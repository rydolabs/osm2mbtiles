#!/usr/bin/env python3
"""Conservative experiment for retained PostGIS import flags, unchanged main code.

No production code or default is altered. Run on disposable separate PostgreSQL
databases, then compare independent outputs by full decoded MVT multiset.
Byte equality across distinct DB imports is diagnostic, not an adoption gate.
"""
from __future__ import annotations
import argparse
from collections import Counter
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

KANTO_SHA256 = "1af62f945ecb25a1c52db30967d7d2db1c6aaea5a17f1dbb1d53dbe55691040e"
MAIN_SHA = "4b7f84ce8bf7a8d4227c1e7f557e9a4056c3db7a"
BBOXES = {"kanto": (139.745, 35.675, 139.77, 35.70),
          "monaco": (7.41, 43.72, 7.44, 43.75)}
POIS = {"kanto": (139.750, 35.680), "monaco": (7.425, 43.735)}
LO, HI = 12, 16
POI_NAME = "osm2mbtiles isolated import profile audit hospital"


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as fp:
        for chunk in iter(lambda: fp.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_import_variant(argv, variant):
    """Transform only initial slim import flags; append remains identical."""
    args = [str(x) for x in argv]
    if not args or Path(args[0]).name != "osm2pgsql":
        raise ValueError("expected initial osm2pgsql command")
    flags = ("--slim", "--cache=256", "--number-processes=1",
             "--disable-parallel-indexing")
    if "--append" in args or "--drop" in args or any(args.count(x) != 1 for x in flags):
        raise ValueError("unexpected import command; refuse to change")
    if variant == "baseline":
        return args
    if variant == "cache512":
        args[args.index("--cache=256")] = "--cache=512"
    elif variant == "workers2":
        args[args.index("--number-processes=1")] = "--number-processes=2"
        # Keep --disable-parallel-indexing. Only importer worker count changes.
    else:
        raise ValueError("unknown profile: " + variant)
    return args


def choose_node_id(pbf):
    p = subprocess.Popen(["osmium", "cat", str(pbf), "-f", "opl"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    nmax = 0
    assert p.stdout is not None
    for row in p.stdout:
        if row.startswith(b"n"):
            token = row.split(maxsplit=1)[0][1:]
            if token.isdigit():
                nmax = max(nmax, int(token))
    if p.wait() or nmax < 1:
        raise RuntimeError("unable to derive real OSM max node ID")
    return nmax + 1


def make_diff(path, pbf, region):
    nodeid = choose_node_id(pbf)
    lon, lat = POIS[region]
    payload = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<osmChange version="0.6" generator="import-configuration-audit"><create>'
        f'<node id="{nodeid}" version="1" lat="{lat}" lon="{lon}" '
        'timestamp="2026-10-30T00:00:00Z" changeset="1">'
        '<tag k="amenity" v="hospital"/>'
        f'<tag k="name" v="{POI_NAME}"/>'
        '</node></create></osmChange>'
    ).encode()
    path.write_bytes(gzip.compress(payload, compresslevel=6, mtime=0))
    return {"id": nodeid, "sha256": digest(path)}


def tiles(path):
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", path
        assert dict(db.execute("SELECT name,value FROM metadata"))["format"] == "pbf"
        for z, x, y, blob in db.execute(
            "SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles "
            "ORDER BY zoom_level,tile_column,tile_row"
        ):
            xyz = int(z), int(x), (1 << int(z)) - 1 - int(y)
            yield xyz, bytes(blob)


def poi_visible(archive, lon, lat):
    import mapbox_vector_tile
    z=16
    x=int((lon+180)/360*(1<<z))
    y=int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)*(1<<z)/2)
    with sqlite3.connect(archive) as db:
        row=db.execute("SELECT tile_data FROM tiles WHERE zoom_level=? "
                       "AND tile_column=? AND tile_row=?",
                       (z,x,(1<<z)-1-y)).fetchone()
    if row is None:
        return False
    result=mapbox_vector_tile.decode(gzip.decompress(bytes(row[0])))
    return any(f.get("properties",{}).get("name")==POI_NAME
               for layer in result.values() for f in layer.get("features",[]))


def profile_run(args):
    from osm2mbtiles import vector_incremental as inc
    pbf, work = args.pbf.resolve(), args.work.resolve()
    if os.environ.get("PGDATABASE") != "gis":
        raise ValueError("benchmark requires disposable gis database")
    work.mkdir(parents=True,exist_ok=True)
    if args.region == "kanto" and digest(pbf) != KANTO_SHA256:
        raise RuntimeError("Kanto PBF checksum changed; refuse noncomparable input")
    os.environ["OSM2MBTILES_VECTOR_QUERY_MODE"] = "projected"
    mode=args.profile
    prefix=work / mode
    archive=prefix.with_suffix(".mbtiles")
    initial=work / (mode+"-initial.mbtiles")
    logfile=work / (mode+"-osm2pgsql-time.txt")
    output={"profile":mode,"region":args.region, "main_sha":MAIN_SHA,
            "input_sha256":digest(pbf), "bbox":BBOXES[args.region],
            "phases":{},"status":"RUNNING"}
    report=work/(mode+"-result.json")
    def save():
        report.write_text(json.dumps(output,indent=2)+"\n",encoding="utf-8")
    save()
    original_run=inc._run
    original_export=inc.full_export
    def cmd_run(cmd):
        words=[str(x) for x in cmd]
        if Path(words[0]).name=="osm2pgsql":
            if "--append" in words:
                raise RuntimeError("initial-only import wrapper used for append")
            words=safe_import_variant(words,mode)
            output["osm2pgsql_args"]=[x if not x.endswith(".osm.pbf") else "<PBF>"
                                      for x in words]
            stamp=time.perf_counter()
            # Capture importer RSS only; PostgreSQL server memory is separate.
            subprocess.run(["/usr/bin/time","-v","-o",str(logfile),*words],check=True)
            output["phases"]["osm2pgsql_import_s"]=round(time.perf_counter()-stamp,3)
        else:
            stamp=time.perf_counter()
            original_run(cmd)
            output["phases"]["derived_sql_s"]=round(time.perf_counter()-stamp,3)
        save()
    def export_wrap(*arg,**kw):
        stamp=time.perf_counter()
        result=original_export(*arg,**kw)
        output["phases"]["initial_tiles_s"]=round(time.perf_counter()-stamp,3)
        save()
        return result
    inc._run=cmd_run
    inc.full_export=export_wrap
    t=time.perf_counter()
    try:
        inc.init(pbf,archive,BBOXES[args.region],LO,HI,processes=4)
    finally:
        inc._run=original_run
        inc.full_export=original_export
    output["phases"]["init_total_s"]=round(time.perf_counter()-t,3)
    shutil.copyfile(archive,initial)
    before=dict(tiles(archive))
    dirty_path=work/"synthetic.osc.gz"
    with inc._connect() as db:
        with db.cursor() as c:
            c.execute("SELECT phase,lo,hi FROM osm2mbtiles_vector_sync WHERE id=1")
            row=c.fetchone()
    if row!=("idle",LO,HI):
        raise RuntimeError("persistent DB state invalid after init")
    start=time.perf_counter()
    inc.append(dirty_path,archive,flush=False)
    output["phases"]["append_s"]=round(time.perf_counter()-start,3)
    with inc._connect() as db:
        with db.cursor() as c:
            c.execute("SELECT zoom,x,y FROM vector_expired_tiles")
            expired={tuple(map(int, r)) for r in c.fetchall()}
    start=time.perf_counter()
    result=inc.flush_tiles(archive)
    output["phases"]["flush_s"]=round(time.perf_counter()-start,3)
    after=dict(tiles(archive))
    changed={xyz for xyz in before.keys()|after.keys() if before.get(xyz)!=after.get(xyz)}
    output["expired_xyz"]=len(expired)
    output["changed_xyz"]=len(changed)
    output["changed_outside_expiry"]=sorted(changed-expired)[:8]
    output["delta_valid"]=(changed <= expired and len(changed)>0
                           and result.get("dirty_total")==len(expired))
    lon,lat=POIS[args.region]
    output["new_poi_visible"]=poi_visible(archive,lon,lat)
    second=inc.flush_tiles(archive)
    output["empty_flush_byte_idempotent"]=(second["dirty_total"]==0 and
                                          dict(tiles(archive))==after)
    try:
        inc.append(dirty_path,archive,flush=False)
    except RuntimeError as err:
        output["replay_rejected"]="same diff twice" in str(err)
    else:
        output["replay_rejected"]=False
    with inc._connect() as db:
        with db.cursor() as c:
            c.execute("SELECT phase FROM osm2mbtiles_vector_sync WHERE id=1")
            output["final_phase"]=c.fetchone()[0]
    output["initial_file_sha256"]=digest(initial)
    output["updated_file_sha256"]=digest(archive)
    output["initial_tiles_count"]=len(before)
    output["updated_tiles_count"]=len(after)
    output["status"]="PASS" if all((
        output["delta_valid"],output["new_poi_visible"],
        output["empty_flush_byte_idempotent"],output["replay_rejected"],
        output["final_phase"]=="idle"
    )) else "INCREMENTAL_FAIL"
    save()
    print("RETAINED_IMPORT_PROFILE",json.dumps(output),flush=True)
    if output["status"]!="PASS":
        raise RuntimeError("original production incremental pipeline safety failure")


def compare(args):
    from postgis_batch_parity import compare_strict
    work=args.work.resolve()
    candidate=args.candidate
    a=json.loads((work/"baseline-result.json").read_text())
    b=json.loads((work/(candidate+"-result.json")).read_text())
    if a["status"]!="PASS" or b["status"]!="PASS":
        raise RuntimeError("Both imports and incremental checks must pass")
    initial=compare_strict(work/"baseline-initial.mbtiles",
                           work/(candidate+"-initial.mbtiles"))
    updated=compare_strict(work/"baseline.mbtiles",work/(candidate+".mbtiles"))
    a_sec=a["phases"]["osm2pgsql_import_s"]
    b_sec=b["phases"]["osm2pgsql_import_s"]
    report={"candidate":candidate, "baseline":a,"variant":b,
            "initial_semantic_equal":initial["equal"],
            "updated_semantic_equal":updated["equal"],
            "initial_differing_tiles":initial["different_tiles"],
            "updated_differing_tiles":updated["different_tiles"],
            "import_seconds_ratio":round(b_sec/a_sec,4),
            "import_seconds_delta_percent":round(100*(b_sec-a_sec)/a_sec,2),
            "comparison_type":"one sequential cold-import pair on one GH runner",
            "decision":"EVIDENCE_ONLY_NO_PRODUCTION_CHANGE"}
    if not initial["equal"] or not updated["equal"]:
        report["decision"]="BLOCKED_DIFFERENT_FEATURES"
    (work/"paired-results.json").write_text(json.dumps(report,indent=2)+"\n")
    lines=[
        "# Retained PostGIS import tuning — paired probe",
        "",
        f"Region: **{args.region}**. Candidate: **{candidate}**.",
        f"Production baseline pinned: \`{MAIN_SHA}\`.",
        "Full source imported into retained \`--slim\` PostgreSQL database per mode;",
        f"tile outputs limited to bbox \`{BBOXES[args.region]}\` z12–16.",
        "",
        "| Phase | Unchanged safe baseline | Single changed flag |",
        "|---|---:|---:|",
    ]
    for key in ("osm2pgsql_import_s","derived_sql_s","initial_tiles_s",
                "init_total_s","append_s","flush_s"):
        lines.append(f"| {key} | {a['phases'].get(key)} s | {b['phases'].get(key)} s |")
    lines+=["",
        f"Importer relative change: **{report['import_seconds_delta_percent']}%** "
        "(negative is faster). **Only one A→B pair; runner order, caches and "
        "resource variations are uncontrolled.**",
        f"Independent DB decoded parity initial: **{initial['equal']}**, "
        f"post-OSC: **{updated['equal']}**.",
        f"Incremental safety baseline/candidate: **{a['status']}/{b['status']}**.",
        f"Both modes retained --slim middle, same post-import.sql, same initial tile "
        "export, same append command, same dirty flush. Alternative flag applies "
        "**only** to initial osm2pgsql import.",
        "No claim of cross-DB byte-level reproducibility or repeatable speedup.",
        "An importer max RSS file is attached, but **excludes PostgreSQL backend "
        "memory**; no WSL memory safety conclusion can be drawn.",
        f"Decision: **{report['decision']}**.",
    ]
    (work/"paired-results.md").write_text("\n".join(lines)+"\n")
    print("RETAINED_IMPORT_PAIR_RESULT",json.dumps({
        "candidate":candidate,"decision":report["decision"],
        "import_delta_percent":report["import_seconds_delta_percent"],
        "initial_semantic_equal":initial["equal"],
        "updated_semantic_equal":updated["equal"]}),flush=True)
    if report["decision"]!="EVIDENCE_ONLY_NO_PRODUCTION_CHANGE":
        raise RuntimeError("independent import profile changed decoded output")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--action",choices=["diff","run","compare"],required=True)
    ap.add_argument("--region",choices=BBOXES,required=True)
    ap.add_argument("--pbf",type=Path)
    ap.add_argument("--work",required=True,type=Path)
    ap.add_argument("--profile",choices=["baseline","cache512","workers2"])
    ap.add_argument("--candidate",choices=["cache512","workers2"])
    a=ap.parse_args()
    if a.action=="diff":
        if not a.pbf:
            ap.error("--pbf required")
        a.work.mkdir(parents=True,exist_ok=True)
        result=make_diff(a.work/"synthetic.osc.gz",a.pbf,a.region)
        print("SAFE_OSC_DIFF",json.dumps(result))
    elif a.action=="run":
        if not a.pbf or not a.profile:
            ap.error("--pbf and --profile required")
        profile_run(a)
    else:
        if not a.candidate:
            ap.error("--candidate required")
        compare(a)


if __name__=="__main__":
    main()
