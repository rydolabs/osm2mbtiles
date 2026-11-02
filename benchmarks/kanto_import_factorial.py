#!/usr/bin/env python3
"""Benchmark-only PostGIS import: one-factor profiles, memory and full MVT parity.

No production files or style changes. Reuses vector_incremental.init unmodified
and intercepts only its initial osm2pgsql argv in this standalone process.
"""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import time

BASE_SHA = "a29c9b9fd18ee4e85a8e9b08cb21ea51ab004024"
PROFILES = {"safe": (1, 256), "cache384": (1, 384),
            "cache512": (1, 512), "cache1024": (1, 1024),
            "workers2": (2, 256), "both": (2, 384)}
REGIONS = {"monaco": ((7.41, 43.72, 7.44, 43.75), (7.425, 43.735)),
           "kanto": ((139.745, 35.675, 139.77, 35.70), (139.750, 35.680))}
ZOOMS = (12, 16)
TABLES = ("vector_points", "vector_lines", "vector_areas",
          "vector_relations", "vector_label_points")


def digest_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(8*1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def tile_xy(lon, lat, z):
    return int((lon + 180)/360*(1 << z)), int(
        (1-math.asinh(math.tan(math.radians(lat)))/math.pi)*(1 << z)/2)


def signature(archive):
    """Full XYZ/decoded feature multiset, ignoring feature order but not duplicates."""
    import mapbox_vector_tile
    result = {"tiles": {}, "metadata": {}, "features": 0}
    with sqlite3.connect(archive) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        result["metadata"] = dict(db.execute("SELECT name,value FROM metadata"))
        result["metadata"].pop("name", None)  # output filename
        assert result["metadata"]["format"] == "pbf"
        for z, x, tms, data in db.execute(
            "SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles ORDER BY 1,2,3"
        ):
            key = f"{z}/{x}/{(1<<z)-1-tms}"
            assert data[:2] == b"\x1f\x8b" and key not in result["tiles"]
            items = []
            for name, layer in mapbox_vector_tile.decode(gzip.decompress(data)).items():
                for feature in layer.get("features", []):
                    items.append(json.dumps(
                        (name, feature.get("id"), feature.get("type"),
                         feature.get("geometry"), feature.get("properties")),
                        ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            items.sort()
            h = hashlib.sha256()
            for item in items:
                h.update(item.encode())
                h.update(b"\n")
            result["tiles"][key] = {"n": len(items), "features_sha": h.hexdigest(),
                                     "gzip_sha": hashlib.sha256(data).hexdigest()}
            result["features"] += len(items)
    if not result["tiles"]:
        raise RuntimeError("No non-empty MVT tiles")
    return result


def compare(a, b):
    xyz = set(a["tiles"]) | set(b["tiles"])
    decoded = [key for key in xyz if (
        a["tiles"].get(key, {}).get("n"), a["tiles"].get(key, {}).get("features_sha")
    ) != (
        b["tiles"].get(key, {}).get("n"), b["tiles"].get(key, {}).get("features_sha")
    )]
    raw = [key for key in xyz if a["tiles"].get(key, {}).get("gzip_sha")
           != b["tiles"].get(key, {}).get("gzip_sha")]
    return {"semantic_equal": a["metadata"] == b["metadata"] and not decoded,
            "xyz_equal": set(a["tiles"]) == set(b["tiles"]),
            "decoded_mismatched": len(decoded), "gzip_mismatched": len(raw),
            "decoded_samples": sorted(decoded)[:10], "gzip_samples": sorted(raw)[:10]}


def raw_sha(archive):
    with sqlite3.connect(archive) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return {f"{z}/{x}/{(1<<z)-1-y}": hashlib.sha256(data).hexdigest()
                for z,x,y,data in db.execute(
                    "SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles")}


def oom_events():
    try:
        pairs = [line.split() for line in Path("/sys/fs/cgroup/memory.events").read_text().splitlines()]
        return int(dict(pairs).get("oom_kill", "0"))
    except (OSError, ValueError):
        return None


def process_memory(root_pid, importer_pid):
    """Process RSS is an upper-bound sum (shared pages counted more than once)."""
    parent = {}
    for d in os.scandir("/proc"):
        if d.name.isdigit():
            try:
                stat = Path(d.path, "stat").read_text()
                parent[int(d.name)] = int(stat[stat.rfind(")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                continue
    def tree(root):
        found = {root} if root in parent else set()
        while True:
            nxt = found | {pid for pid, ppid in parent.items() if ppid in found}
            if nxt == found:
                break
            found = nxt
        return found
    def rss(pids):
        kb = 0
        for pid in pids:
            try:
                for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        kb += int(line.split()[1])
                        break
            except (OSError, ValueError):
                pass
        return round(kb/1024, 1)
    pg, importer = tree(root_pid), tree(importer_pid)
    try:
        cg = round(int(Path("/sys/fs/cgroup/memory.current").read_text())/1048576, 1)
    except (OSError, ValueError):
        cg = None
    return {"postgres_rss_sum_mb": rss(pg), "importer_rss_sum_mb": rss(importer),
            "cgroup_memory_mb": cg, "pg_processes": len(pg),
            "importer_processes": len(importer)}


def measured_command(argv, log, pg_pid):
    started = time.perf_counter()
    start_oom = oom_events()
    with Path(log).open("a") as output:
        output.write("$ " + " ".join(map(str, argv)) + "\n")
        output.flush()
        p = subprocess.Popen([str(x) for x in argv], stdout=output, stderr=subprocess.STDOUT)
        max_values = {x: 0 for x in (
            "postgres_rss_sum_mb", "importer_rss_sum_mb",
            "pg_processes", "importer_processes")}
        max_values["cgroup_memory_mb"] = None
        while p.poll() is None:
            sample = process_memory(pg_pid, p.pid)
            for k, v in sample.items():
                if v is not None:
                    max_values[k] = max(max_values[k] or 0, v)
            time.sleep(0.5)
        code = p.wait()
    end_oom = oom_events()
    if code:
        raise subprocess.CalledProcessError(code, argv)
    if start_oom is not None and end_oom != start_oom:
        raise RuntimeError("Memory cgroup OOM-kill count increased")
    max_values["oom_kill_delta"] = (end_oom-start_oom) if end_oom is not None and start_oom is not None else None
    return round(time.perf_counter()-started, 3), max_values


def table_counts(dbname):
    import psycopg2
    with psycopg2.connect(dbname=dbname) as db, db.cursor() as cur:
        counts = {}
        for name in TABLES:
            cur.execute("SELECT to_regclass(%s)", ("public."+name,))
            if cur.fetchone()[0] is None:
                raise RuntimeError("Missing source table " + name)
            cur.execute(f'SELECT count(*) FROM "{name}"')
            counts[name] = cur.fetchone()[0]
        cur.execute("SELECT to_regclass('public.planet_osm_nodes')")
        if cur.fetchone()[0] is None:
            raise RuntimeError("osm2pgsql slim middle data not retained")
        return counts


def synthetic_osc(pbf, target, lon, lat, name):
    p = subprocess.Popen(["osmium", "cat", str(pbf), "-f", "opl"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    highest = 0
    assert p.stdout is not None
    for line in p.stdout:
        if line.startswith(b"n"):
            token = line.split(maxsplit=1)[0][1:]
            if token.isdigit():
                highest = max(highest, int(token))
    if p.wait() != 0 or highest == 0:
        raise RuntimeError("OSM node ID scan failed")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<osmChange version="0.6" generator="osm2mbtiles-import-audit">'
           '<create>'
           f'<node id="{highest+1}" version="1" lat="{lat}" lon="{lon}" '
           'timestamp="2026-11-02T00:00:00Z" changeset="1">'
           '<tag k="amenity" v="hospital"/>'
           f'<tag k="name" v="{name}"/>'
           '</node></create></osmChange>').encode()
    target.write_bytes(gzip.compress(xml, mtime=0))


def validate_osc(inc, archive, original_pbf, work, location):
    import mapbox_vector_tile
    lon, lat = location
    osc = work/"onefactor-change.osc.gz"
    name = "osm2mbtiles onefactor synthetic hospital"
    synthetic_osc(original_pbf, osc, lon, lat, name)
    before = raw_sha(archive)
    start = time.perf_counter()
    assert inc.append(osc, archive, flush=False)["flush_pending"]
    append_sec = round(time.perf_counter()-start, 3)
    with inc._connect() as db, db.cursor() as cur:
        cur.execute("SELECT zoom,x,y FROM vector_expired_tiles")
        dirty = {f"{z}/{x}/{y}" for z,x,y in cur.fetchall()}
    start = time.perf_counter()
    flush = inc.flush_tiles(archive)
    flush_sec = round(time.perf_counter()-start, 3)
    after = raw_sha(archive)
    changed = {k for k in set(before)|set(after) if before.get(k)!=after.get(k)}
    if not dirty or not changed or not changed <= dirty:
        raise RuntimeError("Modified an unexpired gzip tile")
    z = 16
    x, y = tile_xy(lon, lat, z)
    with sqlite3.connect(archive) as db:
        row = db.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                         (z,x,(1<<z)-1-y)).fetchone()
    if not row:
        raise RuntimeError("New POI z16 tile missing")
    layers = mapbox_vector_tile.decode(gzip.decompress(row[0]))
    if not any(f["properties"].get("name")==name for layer in layers.values()
               for f in layer["features"]):
        raise RuntimeError("New POI not found after append")
    empty = inc.flush_tiles(archive)
    if empty["dirty_total"] != 0 or raw_sha(archive) != after:
        raise RuntimeError("Empty flush changed archive")
    try:
        inc.append(osc, archive, flush=False)
    except RuntimeError as exc:
        if "same diff twice" not in str(exc):
            raise
    else:
        raise RuntimeError("Duplicate OSC diff was accepted")
    return {"append_s": append_sec, "flush_s": flush_sec,
            "dirty_xyz": len(dirty), "changed_gzip_xyz": len(changed),
            "changed_only_expired": True, "visible_z16_poi": True,
            "empty_flush_idempotent": True, "duplicate_rejected": True,
            "flush": flush, "diff_sha256": digest_file(osc),
            "updated": signature(archive), "osc_path": str(osc)}


def audit_main():
    from osm2mbtiles import vector_incremental as inc
    p = argparse.ArgumentParser()
    p.add_argument("--pbf", type=Path, required=True)
    p.add_argument("--region", choices=REGIONS, required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--profiles", default="safe,cache384,workers2,both")
    p.add_argument("--verify-profile", default="both",
                   help="Profile to exercise through OSC append and independent final rebuild")
    p.add_argument("--independent-rebuild", action="store_true")
    args = p.parse_args()
    profiles = args.profiles.split(",")
    if args.repeats not in (1,2,3) or "safe" not in profiles or (
        any(x not in PROFILES for x in profiles) or len(profiles)!=len(set(profiles))):
        raise RuntimeError("Invalid experiment scope")
    if args.independent_rebuild and args.verify_profile not in profiles:
        raise RuntimeError("Independent OSC validation requires selected verify profile")
    work = args.work.resolve()
    if not str(os.environ.get("PGHOST","")).startswith("/tmp/osm2mbtiles-onefactor-") or (
        os.environ.get("PGDATABASE") != "gis"):
        raise RuntimeError("Refusing non-disposable postgres")
    postmaster = work/"pgdata"/"postmaster.pid"
    if not postmaster.exists():
        raise RuntimeError("Disposable PG server not running")
    server_pid = int(postmaster.read_text().splitlines()[0])
    bbox, location = REGIONS[args.region]
    pbf = args.pbf.resolve()
    output = work/"onefactor-results.json"
    report = {"status":"RUNNING", "source_commit": BASE_SHA, "pbf_sha256":digest_file(pbf),
              "selected_profiles": profiles, "osc_verification_profile": args.verify_profile,
              "region":args.region, "bbox":bbox, "zooms":ZOOMS, "trial_results":[],
              "execution_order":[], "adopt_production":False,
              "notes":["same runner and PBF for all trials; no production code modified",
                       "cgroup includes page cache; summed per-process RSS double counts shared pages",
                       "cross-DB gzip differences are diagnostic, semantic parity is required"]}
    def save():
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n")
    save()
    original_run, original_export = inc._run, inc.full_export
    ref_signature, ref_counts = None, None
    chosen_signature = None
    selected_archive = None
    for rep in range(args.repeats):
        shift=rep%len(profiles)
        order=profiles[shift:]+profiles[:shift]
        if rep%2:
            order=list(reversed(order))
        report["execution_order"].append(order)
        for profile in order:
            processes,cache=PROFILES[profile]
            dbname=f"gis_probe_{profile}_{rep+1}"
            subprocess.run(["createdb",dbname],check=True)
            os.environ["PGDATABASE"]=dbname
            archive=work/f"{dbname}.mbtiles"
            log=work/f"{dbname}.log"
            trial={"profile":profile,"repeat":rep+1,"import_workers":processes,
                   "cache_mb":cache,"postgis_parallel_indexing":False,
                   "phases":{},"status":"RUNNING"}
            report["trial_results"].append(trial)
            save()
            def hook(cmd):
                argv=[str(x) for x in cmd]
                tool=Path(argv[0]).name
                if tool=="osm2pgsql":
                    if "--cache=256" not in argv or "--number-processes=1" not in argv or "--append" in argv:
                        raise RuntimeError("Unexpected stable importer flags")
                    argv=[f"--cache={cache}" if x=="--cache=256" else
                          f"--number-processes={processes}" if x=="--number-processes=1" else x
                          for x in argv]
                    kind="import"
                    trial["import_argv"]=argv
                elif tool=="psql":
                    kind="derived_sql"
                else:
                    raise RuntimeError("Unexpected executable "+tool)
                elapsed, peaks=measured_command(argv,log,server_pid)
                trial["phases"][kind]={"seconds":elapsed,"peaks":peaks}
                save()
            def export_hook(*a,**kw):
                start=time.perf_counter()
                result=original_export(*a,**kw)
                trial["phases"]["legacy_export_s"]=round(time.perf_counter()-start,3)
                save()
                return result
            inc._run,inc.full_export=hook,export_hook
            try:
                start=time.perf_counter()
                inc.init(pbf,archive,bbox,*ZOOMS,processes=4)
                trial["init_total_s"]=round(time.perf_counter()-start,3)
                inc._run,inc.full_export=original_run,original_export
                trial["counts"]=table_counts(dbname)
                trial["archive"]=signature(archive)
                if ref_signature is None:
                    ref_signature,ref_counts=trial["archive"],trial["counts"]
                trial["comparison"]=compare(ref_signature,trial["archive"])
                if trial["counts"]!=ref_counts or not trial["comparison"]["semantic_equal"]:
                    raise RuntimeError("Mismatch across source tables or all decoded tile features")
                if trial["comparison"]["gzip_mismatched"]:
                    raise RuntimeError("Initial MVT gzip byte parity mismatch vs first fresh DB")
                trial["status"]="PASS"
                save()
                print("IMPORT_ONEFACTOR_TRIAL",json.dumps({
                    "profile":profile,"rep":rep+1,
                    "import_s":trial["phases"]["import"]["seconds"],
                    "post_s":trial["phases"]["derived_sql"]["seconds"],
                    "peak_cgroup_mb":trial["phases"]["import"]["peaks"]["cgroup_memory_mb"],
                    "tile_count":len(trial["archive"]["tiles"]),
                    "features":trial["archive"]["features"],
                    "gzip_mismatch_vs_first":trial["comparison"]["gzip_mismatched"]}),flush=True)
                if args.independent_rebuild and profile==args.verify_profile and rep==args.repeats-1:
                    selected_archive=archive
                    report["osc"]=validate_osc(inc,archive,pbf,work,location)
                    save()
            except BaseException as exc:
                trial["status"]="FAIL"
                trial["error"]=str(exc)
                report["status"]="FAIL"
                save()
                raise
            finally:
                inc._run,inc.full_export=original_run,original_export
                os.environ["PGDATABASE"]="gis"
                subprocess.run(["dropdb","--if-exists",dbname],check=True)
                if archive!=selected_archive:
                    archive.unlink(missing_ok=True)
                    archive.with_name(archive.name+".update-flex.lua").unlink(missing_ok=True)
    if args.independent_rebuild:
        if selected_archive is None:
            raise RuntimeError("Candidate diff was not verified")
        osc=work/"onefactor-change.osc.gz"
        final_pbf=work/"independent-final.osm.pbf"
        start=time.perf_counter()
        subprocess.run(["osmium","apply-changes",str(pbf),str(osc),"-o",str(final_pbf)],check=True)
        report["osc"]["pbf_reconstruction_s"]=round(time.perf_counter()-start,3)
        report["osc"]["independent_final_pbf_sha256"]=digest_file(final_pbf)
        dbname="gis_probe_independent_final"
        subprocess.run(["createdb",dbname],check=True)
        os.environ["PGDATABASE"]=dbname
        rebuilt=work/"independent-final.mbtiles"
        try:
            start=time.perf_counter()
            inc.init(final_pbf,rebuilt,bbox,*ZOOMS,processes=4)
            report["osc"]["independent_rebuild_s"]=round(time.perf_counter()-start,3)
            result=compare(report["osc"]["updated"],signature(rebuilt))
            report["osc"]["independent_rebuild_parity"]=result
            if not result["semantic_equal"]:
                raise RuntimeError("Append+flush differs semantically from independently rebuilt PBF")
        finally:
            os.environ["PGDATABASE"]="gis"
            subprocess.run(["dropdb","--if-exists",dbname],check=True)
            rebuilt.unlink(missing_ok=True)
            rebuilt.with_name(rebuilt.name+".update-flex.lua").unlink(missing_ok=True)
            final_pbf.unlink(missing_ok=True)
        selected_archive.unlink(missing_ok=True)
        selected_archive.with_name(selected_archive.name+".update-flex.lua").unlink(missing_ok=True)
    summaries={}
    for profile in profiles:
        rows=[r for r in report["trial_results"] if r["profile"]==profile]
        times=[r["phases"]["import"]["seconds"]+r["phases"]["derived_sql"]["seconds"]
               for r in rows]
        summaries[profile]={"all_import_plus_derived_s":times,
                            "median_s":statistics.median(times),
                            "max_sampled_cgroup_mb":max(
                                (r["phases"]["import"]["peaks"]["cgroup_memory_mb"]
                                 for r in rows if r["phases"]["import"]["peaks"]["cgroup_memory_mb"] is not None),
                                default=None),
                            "max_importer_rss_mb":max(
                                r["phases"]["import"]["peaks"]["importer_rss_sum_mb"] for r in rows),
                            "max_postgres_rss_sum_mb":max(
                                r["phases"]["import"]["peaks"]["postgres_rss_sum_mb"] for r in rows)}
    safe=summaries["safe"]["median_s"]
    for p,stats in summaries.items():
        stats["observed_speedup_pct_vs_safe"]=round(100*(safe-stats["median_s"])/safe,2)
    report["timings"]=summaries
    report["status"]="PASS_EXPERIMENT_NOT_APPROVED"
    save()
    print("IMPORT_ONEFACTOR_DONE",json.dumps({
        "status":report["status"],"timings":summaries,
        "independent_full_rebuild":report.get("osc",{}).get(
            "independent_rebuild_parity",{}).get("semantic_equal")}),flush=True)


if __name__=="__main__":
    audit_main()
