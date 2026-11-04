#!/usr/bin/env python3
"""Berlin: cache projected JSONB attributes in ORIGINAL source tables, benchmark only.

No separate fast_* tables, new geometry indexes, sort, MVT encoder, gzip, or
SQLite writer. An isolated PostGIS DB is imported once. Build/cache costs are
reported independently; full A/B proceeds only after exact raw MVT sample
byte parity and >=5% median 25-query time improvement.
"""
from __future__ import annotations

import argparse
from collections import Counter
from itertools import zip_longest
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import time

from osm2mbtiles import vector
from osm2mbtiles.tiles import xyz_range

BBOX = (13.088, 52.338, 13.761, 52.676)
LO, HI = 12, 16
PBF_SHA = "51d3845df2e4b1334ebec7cf6516e41e71735ce2fc1c0975c52d57f58f1952ad"
PBF_BYTES = 99604748
EXPECTED_XYZ = 16943
EXPECTED_NONEMPTY = 12047
PGHOST = "/tmp/osm2mbtiles-inline-attr-berlin/socket"
WORKERS = 4
REPEATS = 3
TABLES = vector.CACHE_TABLES


def check_environment(pbf: Path):
    if (os.environ.get("PGHOST") != PGHOST or
            os.environ.get("PGUSER") != "attrprobe" or
            os.environ.get("PGDATABASE") != "gis" or
            os.environ.get("OSM2MBTILES_VECTOR_QUERY_MODE") != "projected"):
        raise RuntimeError("Unsafe database or unfrozen query mode")
    if os.name != "posix":
        raise RuntimeError("Only Linux fork worker tests are supported")
    if vector.tile_count(BBOX, LO, HI) != EXPECTED_XYZ:
        raise RuntimeError("Berlin XYZ grid drift")
    sha = hashlib.sha256()
    with pbf.open("rb") as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
            sha.update(b)
    if sha.hexdigest() != PBF_SHA or pbf.stat().st_size != PBF_BYTES:
        raise RuntimeError("Wrong Berlin PBF SHA/size")


def array_literal(keys):
    return "ARRAY[" + ",".join("'" + k.replace("'", "''") + "'" for k in keys) + "]::text[]"


def build_sql(table):
    if table not in TABLES:
        raise ValueError("Unsafe table name")
    def projection(keys):
        return ("(SELECT COALESCE(jsonb_object_agg(p.key,p.value), '{}'::jsonb) "
                "FROM jsonb_each(t.tags) AS p(key,value) "
                f"WHERE p.key = ANY({array_literal(keys)}))")
    return (f"UPDATE {table} t SET bench_props_low = {projection(vector.LOW_FIELDS)}, "
            f"bench_props_high = {projection(vector.ALL_FIELDS)}")


def candidate_sql(z: int, *, cached=False, projected=False) -> str:
    if cached or not projected:
        raise ValueError("Frozen projected SQL only")
    original = vector.query_for_zoom(z, projected=True)
    candidate = original
    specs = list(vector.layer_specs(z))
    for name, table, key, _ in specs:
        old = vector._properties("t", z, name, key, projected=True)
        new = (vector._properties("t", z, name, key, cached=True)
               .replace("t.props_low", "t.bench_props_low")
               .replace("t.props_high", "t.bench_props_high"))
        # _properties may be shared verbatim across two source tables.
        if old not in candidate and new not in candidate:
            raise RuntimeError(f"Could not find baseline property projection for {name}")
        candidate = candidate.replace(old, new)
    if candidate.count("%s") != original.count("%s") or candidate.count("%s") != 6:
        raise RuntimeError("Changed tile SQL bind parameters")
    if candidate.count("ST_AsMVTGeom(") != original.count("ST_AsMVTGeom("):
        raise RuntimeError("Changed MVT feature branches")
    if candidate.count("ST_AsMVT(q,") != original.count("ST_AsMVT(q,"):
        raise RuntimeError("Changed MVT layer concatenation")
    if candidate.count("jsonb_object_agg") != 0:
        raise RuntimeError("Not all per-feature tag projections replaced")
    return candidate


def xyz_samples():
    rows = []
    for z in range(LO, HI + 1):
        xmin, xmax, ymin, ymax = xyz_range(BBOX, z)
        coords = ((xmin, ymin), (xmax, ymax),
                  ((xmin+xmax)//2, (ymin+ymax)//2),
                  (xmin, ymax), (xmax, ymin))
        rows.extend((z, x, y) for x, y in dict.fromkeys(coords))
    return rows


def save(path: Path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")


def build_cache(cur, report, out):
    # All changes remain in the disposable DB; source geometries and their
    # index definitions are never modified. Include UPDATE + VACUUM overhead.
    result = {"tables": [], "total_seconds": 0}
    sizes_before = {}
    for table in TABLES:
        cur.execute("SELECT pg_total_relation_size(%s::regclass)", (table,))
        sizes_before[table] = cur.fetchone()[0]
    start = time.perf_counter()
    for table in TABLES:
        stamp = time.perf_counter()
        cur.execute(f"ALTER TABLE {table} ADD COLUMN bench_props_low jsonb")
        cur.execute(f"ALTER TABLE {table} ADD COLUMN bench_props_high jsonb")
        cur.execute(build_sql(table))
        rows = cur.rowcount
        cur.execute(f"VACUUM (ANALYZE) {table}")
        cur.execute("SELECT pg_total_relation_size(%s::regclass)", (table,))
        size_after = cur.fetchone()[0]
        item = {"table": table, "rows": rows,
                "seconds": round(time.perf_counter()-stamp, 3),
                "relation_bytes_before": sizes_before[table],
                "relation_bytes_after": size_after,
                "relation_growth_bytes": size_after-sizes_before[table]}
        result["tables"].append(item)
        report["cache_build"] = result
        save(out, report)
        print("INLINE_ATTR_CACHE_TABLE", json.dumps(item), flush=True)
    result["total_seconds"] = round(time.perf_counter()-start, 3)
    report["cache_build"] = result
    save(out, report)
    return result


def sample_queries(cur, report, out):
    original = {z: vector.query_for_zoom(z, projected=True) for z in range(LO,HI+1)}
    cached = {z: candidate_sql(z, projected=True) for z in range(LO,HI+1)}
    xyz = xyz_samples()
    report["sample"] = {"n_xyz": len(xyz), "trials": [],
                        "raw_mvt_byte_mismatches": 0,
                        "mismatch_examples": [], "explain": []}
    cur.execute("SET statement_timeout = 90000")
    for trial in range(REPEATS):
        elapsed = {"baseline": [], "candidate": []}
        for i, (z,x,y) in enumerate(xyz):
            order = ("baseline","candidate") if (i+trial)%2==0 else ("candidate","baseline")
            payloads = {}
            for mode in order:
                t0 = time.perf_counter()
                cur.execute(original[z] if mode=="baseline" else cached[z],
                            (z,x,y,z,x,y))
                payloads[mode] = bytes(cur.fetchone()[0] or b"")
                elapsed[mode].append((time.perf_counter()-t0)*1000)
            if payloads["baseline"] != payloads["candidate"]:
                report["sample"]["raw_mvt_byte_mismatches"] += 1
                if len(report["sample"]["mismatch_examples"]) < 10:
                    report["sample"]["mismatch_examples"].append(
                        {"xyz": [z,x,y], "trial": trial+1,
                         "baseline_sha256": hashlib.sha256(payloads["baseline"]).hexdigest(),
                         "candidate_sha256": hashlib.sha256(payloads["candidate"]).hexdigest()})
        one = {"trial": trial+1,
               "baseline_total_ms": round(sum(elapsed["baseline"]), 3),
               "candidate_total_ms": round(sum(elapsed["candidate"]), 3)}
        report["sample"]["trials"].append(one)
        save(out, report)
        print("INLINE_ATTR_SAMPLE_TRIAL", json.dumps(one), flush=True)
    for z in (12,14,16):
        x,y = [(x,y) for zz,x,y in xyz if zz == z][2]
        for mode, query in (("baseline",original[z]),("candidate",cached[z])):
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "+query,
                        (z,x,y,z,x,y))
            raw = cur.fetchone()[0]
            plan = raw[0] if isinstance(raw,list) else json.loads(raw)[0]
            report["sample"]["explain"].append(
                {"xyz":[z,x,y], "mode":mode,
                 "planning_ms":round(plan["Planning Time"],3),
                 "execution_ms":round(plan["Execution Time"],3),
                 "shared_hit_blocks":plan["Plan"].get("Shared Hit Blocks",0),
                 "shared_read_blocks":plan["Plan"].get("Shared Read Blocks",0)})
    baseline=statistics.median(x["baseline_total_ms"] for x in report["sample"]["trials"])
    candidate=statistics.median(x["candidate_total_ms"] for x in report["sample"]["trials"])
    report["sample"]["median_ms"]={"baseline":round(baseline,3),"candidate":round(candidate,3)}
    report["sample"]["speedup_pct"]=round((1-candidate/baseline)*100,3)
    report["status"] = "PROMISING_FULL_AB" if (
        report["sample"]["raw_mvt_byte_mismatches"]==0 and candidate<=baseline*.95
    ) else "STOP_SAMPLE_GATE"
    save(out, report)
    print("INLINE_ATTR_SAMPLE_GATE", json.dumps({
        "status": report["status"],
        "raw_mvt_byte_mismatches": report["sample"]["raw_mvt_byte_mismatches"],
        "median_ms": report["sample"]["median_ms"],
        "sample_speedup_pct": report["sample"]["speedup_pct"]
    }), flush=True)


def compare_archives(left: Path, right: Path):
    with sqlite3.connect(left) as a, sqlite3.connect(right) as b:
        if a.execute("PRAGMA integrity_check").fetchone()[0]!="ok" or \
           b.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
            raise RuntimeError("SQLite integrity_check failed")
        am = dict(a.execute("SELECT name,value FROM metadata"))
        bm = dict(b.execute("SELECT name,value FROM metadata"))
        am.pop("name",None);bm.pop("name",None)
        if am!=bm:
            raise RuntimeError("MVT MBTiles metadata differs")
        q = ("SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles "
             "ORDER BY zoom_level,tile_column,tile_row")
        count=bad=ab=bb=0
        for x,y in zip_longest(a.execute(q),b.execute(q)):
            count+=1
            if x is not None:ab+=len(x[3])
            if y is not None:bb+=len(y[3])
            if x is None or y is None or x[:3]!=y[:3] or x[3]!=y[3]:
                bad+=1
        return {"rows":count,"gzip_payload_mismatches":bad,
                "gzip_bytes_baseline":ab,"gzip_bytes_candidate":bb,
                "metadata_equal_except_name":True}


def paired_full(pbf: Path,report,out):
    baseline_query = vector.query_for_zoom
    report["full_ab"]={"pairs":[],"export_workers":WORKERS,"paired_repeats":REPEATS}
    for trial in range(1,REPEATS+1):
        order = ("baseline","candidate") if trial%2 else ("candidate","baseline")
        paths = {}
        times={}
        try:
            for mode in order:
                archive=out.parent/f"berlin-{trial}-{mode}.mbtiles"
                if archive.exists() or archive.with_name(archive.name+".partial").exists():
                    raise RuntimeError("Existing archive; refusing overwrite")
                vector.query_for_zoom = baseline_query if mode=="baseline" else candidate_sql
                t0=time.perf_counter()
                written=vector.export(pbf,archive,BBOX,LO,HI,WORKERS)
                times[mode]=round(time.perf_counter()-t0,3)
                paths[mode]=archive
                if written!=EXPECTED_NONEMPTY:
                    raise RuntimeError("Unexpected number of nonempty tiles")
            parity=compare_archives(paths["baseline"],paths["candidate"])
            pair={"trial":trial,"order":list(order),"wall_seconds":times,"parity":parity}
            report["full_ab"]["pairs"].append(pair)
            save(out,report)
            print("INLINE_ATTR_FULL_PAIR",json.dumps(pair),flush=True)
            if parity["rows"]!=EXPECTED_NONEMPTY or parity["gzip_payload_mismatches"]:
                raise RuntimeError("Complete Berlin gzip or XYZ byte parity FAIL")
        finally:
            vector.query_for_zoom=baseline_query
            for file in paths.values():
                file.unlink(missing_ok=True)
    a=statistics.median(x["wall_seconds"]["baseline"] for x in report["full_ab"]["pairs"])
    b=statistics.median(x["wall_seconds"]["candidate"] for x in report["full_ab"]["pairs"])
    savings=a-b
    build=report["cache_build"]["total_seconds"]
    report["full_ab"]["medians_s"]={"baseline":round(a,3),"candidate":round(b,3)}
    report["full_ab"]["warm_speedup_pct"]=round((1-b/a)*100,3)
    report["full_ab"]["candidate_net_savings_after_build_s"]=round(savings-build,3)
    report["full_ab"]["candidate_build_break_even_export_count"]=(
        round(build/savings,2) if savings>0 else None)
    report["status"] = ("PROMISING_WARM_AND_NET" if
        b<=a*.95 and build<savings and
        all(p["wall_seconds"]["candidate"]<p["wall_seconds"]["baseline"]
            for p in report["full_ab"]["pairs"])
        else "NO_END_TO_END_GAIN_PROVEN")
    save(out,report)
    print("INLINE_ATTR_FULL_DONE",json.dumps({
        "status":report["status"],"cache_build_seconds":build,
        **report["full_ab"]}),flush=True)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--pbf",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    check_environment(args.pbf)
    report={"status":"RUNNING","pbf_sha256":PBF_SHA,"pbf_bytes":PBF_BYTES,
            "bbox":BBOX,"zooms":[LO,HI],"requested_xyz":EXPECTED_XYZ,
            "nonempty_expected":EXPECTED_NONEMPTY,"query_mode":"projected",
            "production_unchanged":True,"adopt_production":False,
            "scope":"disposable PostGIS DB; cache build + same-DB warm exporter A/B",
            "limitations":["Warm archive A/B runs after in-place disposable cache construction.",
                           "A positive warm result alone does not prove fresh-DB end-to-end gain.",
                           "Fresh independent PostGIS run A/B needed before production adoption."]}
    save(args.output,report)
    try:
        import psycopg2
        # Psycopg2 connection context managers open a transaction even when
        # autocommit is set. VACUUM must run outside that block.
        conn=psycopg2.connect(dbname="gis")
        try:
            conn.autocommit=True
            with conn.cursor() as cur:
                pre_cache={}
                cur.execute("SET statement_timeout = 90000")
                for z,x,y in xyz_samples():
                    cur.execute(vector.query_for_zoom(z,projected=True),
                                (z,x,y,z,x,y))
                    pre_cache[(z,x,y)]=hashlib.sha256(
                        bytes(cur.fetchone()[0] or b"")).hexdigest()
                cur.execute("RESET statement_timeout")
                build_cache(cur,report,args.output)
                report["precache_byte_gate"]={"xyz_checked":len(pre_cache),
                    "baseline_raw_mvt_mismatches":0,"mismatch_xyz":[]}
                cur.execute("SET statement_timeout = 90000")
                for (z,x,y),before in pre_cache.items():
                    cur.execute(vector.query_for_zoom(z,projected=True),
                                (z,x,y,z,x,y))
                    after=hashlib.sha256(bytes(cur.fetchone()[0] or b"")).hexdigest()
                    if before!=after:
                        report["precache_byte_gate"]["baseline_raw_mvt_mismatches"]+=1
                        report["precache_byte_gate"]["mismatch_xyz"].append([z,x,y])
                save(args.output,report)
                print("INLINE_ATTR_PRECACHE_GATE",
                      json.dumps(report["precache_byte_gate"]),flush=True)
                if report["precache_byte_gate"]["baseline_raw_mvt_mismatches"]:
                    report["status"]="STOP_PRECACHE_BYTE_GATE"
                    save(args.output,report)
                else:
                    sample_queries(cur,report,args.output)
        finally:
            conn.close()
        if report["status"] == "PROMISING_FULL_AB":
            paired_full(args.pbf,report,args.output)
        else:
            print("INLINE_ATTR_SKIP_FULL",report["status"],flush=True)
    except BaseException as e:
        report["status"]="FAIL"
        report["error"]=repr(e)
        save(args.output,report)
        raise
    save(args.output,report)
    print("INLINE_ATTR_FINAL",json.dumps({"status":report["status"],
        "cache_build":report.get("cache_build",{}).get("total_seconds"),
        "sample":report.get("sample",{}).get("median_ms"),
        "full":report.get("full_ab",{}).get("medians_s")}),flush=True)


if __name__=="__main__":
    main()
