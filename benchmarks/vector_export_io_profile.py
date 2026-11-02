#!/usr/bin/env python3
"""Initial vector MBTiles I/O profiling on the SAME frozen PostGIS database.

Runs unchanged production vector.export(), then two benchmark-only candidates
which use the IDENTICAL vector.render_one SQL/zoom plan, feature ordering and
gzip arguments; the only changes are which process compresses and how SQLite
INSERTs are batched. Refuse adoption on ANY compressed tile payload mismatch.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import time

from osm2mbtiles import vector

BBOX = {
    "monaco": (7.41, 43.72, 7.44, 43.75),
    "kanto": (139.55, 35.55, 139.95, 35.85),
}
PROFILES = ("legacy", "worker-gzip", "worker-gzip-batch")
ZLO, ZHI = 12, 16
PRODUCTION_SHA = "a29c9b9fd18ee4e85a8e9b08cb21ea51ab004024"


def hash_file(file):
    h = hashlib.sha256()
    with open(file, "rb") as f:
        for chunk in iter(lambda: f.read(8*1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def render_compressed(task):
    """Exactly the production render_one SQL. Move only gzip to its worker."""
    start = time.perf_counter()
    z,x,y,raw = vector.render_one(task)
    sql_s = time.perf_counter() - start
    start = time.perf_counter()
    packed = gzip.compress(raw, compresslevel=6, mtime=0) if raw else b""
    gzip_s = time.perf_counter() - start
    return z,x,y,packed,sql_s,gzip_s


def full_manifest(path):
    """Checks every stored gzip blob, XYZ membership, metadata and SQLite integrity."""
    with sqlite3.connect(path) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite MBTiles integrity failed")
        meta = dict(db.execute("SELECT name,value FROM metadata"))
        if meta.get("format") != "pbf":
            raise RuntimeError("Unexpected vector format")
        meta.pop("name", None)  # different output stems are expected
        rows = {}
        total_bytes = 0
        for z,x,tms,blob in db.execute(
                "SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles"):
            if not blob or not bytes(blob).startswith(b"\x1f\x8b"):
                raise RuntimeError("Non-gzip tile output")
            key = (int(z),int(x),(1 << int(z)) - 1 - int(tms))
            if key in rows:
                raise RuntimeError("Duplicate XYZ")
            rows[key] = hashlib.sha256(blob).hexdigest()
            total_bytes += len(blob)
        if not rows:
            raise RuntimeError("Empty MBTiles result")
        return {"metadata":meta, "rows":rows, "tiles":len(rows),
                "tile_bytes":total_bytes}


def compare_manifests(base, candidate):
    coords = set(base["rows"]) | set(candidate["rows"])
    mismatches = [xyz for xyz in coords
                  if base["rows"].get(xyz) != candidate["rows"].get(xyz)]
    return {
        "exact_gzip_and_xyz": not mismatches and base["metadata"] == candidate["metadata"],
        "metadata_equal": base["metadata"] == candidate["metadata"],
        "xyz_equal": set(base["rows"]) == set(candidate["rows"]),
        "gzip_mismatches":len(mismatches),
        "mismatch_examples":sorted(mismatches)[:15],
        "reference_tiles":base["tiles"],"candidate_tiles":candidate["tiles"],
    }


def export_candidate(out, bbox, workers, mode):
    """No modified SQL or MVT encoder; sqlite writer remains single-threaded."""
    assert mode in ("worker-gzip","worker-gzip-batch")
    partial = out.with_name(out.name+".partial")
    partial.unlink(missing_ok=True)
    total = vector.tile_count(bbox, ZLO, ZHI)
    metrics = {"worker_wait_s":0.0,"sqlite_write_s":0.0,
               "worker_sql_s_sum":0.0,"worker_gzip_s_sum":0.0,
               "tiles_expected":total, "tiles_seen":0, "tiles_written":0,
               "commit_count":0, "sql_insert_calls":0}
    started = time.perf_counter()
    ctx = multiprocessing.get_context("spawn" if sys.platform == "win32" else "fork")
    with ctx.Pool(processes=workers,initializer=vector.init_worker) as pool:
        with sqlite3.connect(partial) as db:
            vector.create_db(db,vector.metadata(out.stem,bbox,ZLO,ZHI))
            jobs = iter(pool.imap_unordered(render_compressed,
                       vector.xyz_tasks(bbox,ZLO,ZHI),chunksize=1))
            rows = []
            pending = 0

            def write_batch():
                nonlocal pending
                if not rows:
                    return
                t0 = time.perf_counter()
                db.executemany("INSERT INTO tiles VALUES (?,?,?,?)", rows)
                metrics["sqlite_write_s"] += time.perf_counter()-t0
                metrics["sql_insert_calls"] += 1
                pending += len(rows)
                rows.clear()
                if pending >= 1024:
                    t1 = time.perf_counter()
                    db.commit()
                    metrics["sqlite_write_s"] += time.perf_counter()-t1
                    metrics["commit_count"] += 1
                    pending = 0

            while True:
                t0 = time.perf_counter()
                try:
                    z,x,y,packed,sql_s,gzip_s = next(jobs)
                except StopIteration:
                    break
                metrics["worker_wait_s"] += time.perf_counter()-t0
                metrics["worker_sql_s_sum"] += sql_s
                metrics["worker_gzip_s_sum"] += gzip_s
                metrics["tiles_seen"] += 1
                if packed:
                    metrics["tiles_written"] += 1
                    entry=(z,x,(1<<z)-1-y,sqlite3.Binary(packed))
                    if mode=="worker-gzip-batch":
                        rows.append(entry)
                        if len(rows)>=256:
                            write_batch()
                    else:
                        t1=time.perf_counter()
                        db.execute("INSERT INTO tiles VALUES (?,?,?,?)",entry)
                        metrics["sqlite_write_s"] += time.perf_counter()-t1
                        metrics["sql_insert_calls"] += 1
                        pending += 1
                        if pending>=128:
                            t2=time.perf_counter()
                            db.commit()
                            metrics["sqlite_write_s"] += time.perf_counter()-t2
                            metrics["commit_count"] += 1
                            pending=0
                if metrics["tiles_seen"]%2000==0:
                    print("EXPORT_IO_PROGRESS",json.dumps({
                        "mode":mode,"seen":metrics["tiles_seen"],
                        "total":total,"elapsed_s":round(time.perf_counter()-started,3)}),
                        flush=True)
            write_batch()
            if metrics["tiles_seen"] != total:
                raise RuntimeError("Incomplete XYZ enumeration")
            t1=time.perf_counter()
            db.commit()
            metrics["sqlite_write_s"] += time.perf_counter()-t1
            metrics["commit_count"] += 1
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("PRAGMA journal_mode=DELETE")
            if db.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
                raise RuntimeError("SQLite integrity failed before publish")
    os.replace(partial,out)
    metrics["wall_s"]=round(time.perf_counter()-started,3)
    return metrics


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--region",choices=BBOX,required=True)
    p.add_argument("--pbf",type=Path,required=True)
    p.add_argument("--work",type=Path,required=True)
    p.add_argument("--repeats",type=int,default=3)
    p.add_argument("--workers",type=int,default=4)
    args=p.parse_args()
    if args.repeats not in (1,2,3) or not 1<=args.workers<=8:
        raise ValueError("Repeat range 1..3, worker count 1..8")
    if os.environ.get("PGDATABASE")!="gis" or not str(
            os.environ.get("PGHOST","")).startswith("/tmp/osm2mbtiles-io-"):
        raise RuntimeError("Only a disposable local PostGIS database is allowed")
    if os.environ.get("OSM2MBTILES_VECTOR_QUERY_MODE")!="projected":
        raise RuntimeError("Must use identical projected SQL for each profile")
    work=args.work.resolve()
    if not (work/"pgdata"/"postmaster.pid").is_file():
        raise RuntimeError("Disposable PostgreSQL server is missing")
    work.mkdir(parents=True,exist_ok=True)
    report={"status":"RUNNING","production_sha":PRODUCTION_SHA,
            "pbf_sha256":hash_file(args.pbf),
            "region":args.region,"bbox":BBOX[args.region],
            "zooms":[ZLO,ZHI],"workers":args.workers,"repeats":args.repeats,
            "profile_order":[],"trials":[],"comparisons":[],
            "adopt_production":False,
            "notes":["same DB and full PBF source across profiles",
                     "strict gzip and XYZ parity required",
                     "candidate uses identical SELECT, MVT encoder, gzip level and mtime",
                     "SQLite writer single-process in all profiles",
                     "only tile-export timing, not import included"]}
    output=work/"export-io-profile.json"
    def save():output.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n")
    save()
    # One all-region PostGIS import was performed in this job before profiling.
    for repeat in range(args.repeats):
        order=list(PROFILES[repeat%3:]+PROFILES[:repeat%3])
        if repeat%2:
            order.reverse()
        report["profile_order"].append(order)
        manifests={}
        paths=[]
        for name in order:
            archive=work/f"io-{name}-{repeat}.mbtiles"
            archive.unlink(missing_ok=True)
            entry={"profile":name,"repeat":repeat+1,"status":"RUNNING"}
            report["trials"].append(entry)
            save()
            try:
                started=time.perf_counter()
                if name=="legacy":
                    n=vector.export(args.pbf,archive,BBOX[args.region],ZLO,ZHI,args.workers)
                    entry["metrics"]={"tiles_written":n}
                else:
                    entry["metrics"]=export_candidate(archive,BBOX[args.region],args.workers,name)
                entry["wall_s"]=round(time.perf_counter()-started,3)
                sig=full_manifest(archive)
                manifests[name]=sig
                paths.append(archive)
                entry["nonempty_tiles"]=sig["tiles"]
                entry["tile_bytes"]=sig["tile_bytes"]
                entry["status"]="PASS"
                save()
                print("EXPORT_IO_TRIAL",json.dumps({
                    "profile":name,"repeat":repeat+1,"wall_s":entry["wall_s"],
                    "nonempty_tiles":sig["tiles"],
                    "writer_s":entry["metrics"].get("sqlite_write_s"),
                    "sql_worker_s_sum":entry["metrics"].get("worker_sql_s_sum"),
                    "gzip_worker_s_sum":entry["metrics"].get("worker_gzip_s_sum")}),
                    flush=True)
            except BaseException as ex:
                entry["status"]="FAIL"
                entry["error"]=str(ex)
                report["status"]="FAIL"
                save()
                raise
        baseline=manifests["legacy"]
        for name in PROFILES:
            cmp=compare_manifests(baseline,manifests[name])
            report["comparisons"].append({"repeat":repeat+1,"candidate":name,**cmp})
            save()
            if not cmp["exact_gzip_and_xyz"]:
                report["status"]="BYTE_PARITY_BLOCKED"
                save()
                raise RuntimeError("Strict same-DB compressed MVT byte parity FAILED for "+name)
        for path in paths:
            path.unlink()
    samples={}
    for name in PROFILES:
        times=[t["wall_s"] for t in report["trials"] if t["profile"]==name]
        samples[name]={"wall_s":times,"median_s":round(statistics.median(times),3),
                       "range_s":[min(times),max(times)]}
    base=samples["legacy"]["median_s"]
    for name in PROFILES:
        samples[name]["speedup_pct_vs_legacy"]=round(100*(base-samples[name]["median_s"])/base,2)
    report["timings"]=samples
    report["status"]="PASS_EXPLORATORY_ONLY"
    save()
    print("EXPORT_IO_COMPLETED",json.dumps({"status":report["status"],
          "timings":samples,"all_bytes_equal":True}),flush=True)


if __name__=="__main__":
    main()
