#!/usr/bin/env python3
"""Read-only Berlin PostGIS SQL diagnostics and prepared-statement A/B pilot.

Only benchmark-side functions differ. Both modes call exactly the frozen
vector.query_for_zoom(z, projected=True), same source DB, MVT parameters,
gzip arguments and SQLite writer. Strict comparison is compressed-tile bytes.
"""
from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import statistics
import time

from osm2mbtiles import vector

BBOX = (13.088, 52.338, 13.761, 52.676)
LO, HI = 12, 16
FROZEN_SHA = "a29c9b9fd18ee4e85a8e9b08cb21ea51ab004024"
PBF_SHA = "51d3845df2e4b1334ebec7cf6516e41e71735ce2fc1c0975c52d57f58f1952ad"
NAME_PREFIX = "osm2mbtiles_probe_z"
PILOT_BY_ZOOM = {12: 24, 13: 48, 14: 120, 15: 240, 16: 560}
MIN_GAIN_PCT = 5.0
_prepared = set()


def prepared_sql(z: int) -> str:
    """Replace exactly 6 psycopg placeholders with server-side $1..$6."""
    source = vector.query_for_zoom(z, projected=True)
    if source.count("%s") != 6:
        raise RuntimeError("Source SQL placeholder contract changed")
    for idx in range(1, 7):
        source = source.replace("%s", "$"+str(idx), 1)
    if "%s" in source:
        raise RuntimeError("Untranslated PostgreSQL placeholder")
    return source


def init_prepared():
    global _prepared
    vector.init_worker()
    _prepared = set()


def render_prepared(task):
    z, x, y = task
    if z < 3:
        return z,x,y,b""
    name = f"{NAME_PREFIX}{z}"
    with vector._conn.cursor() as cur:
        if z not in _prepared:
            # Same SQL body, same coordinates. Only parse/plan reuse changes.
            cur.execute(
                f"PREPARE {name} (integer,integer,integer,integer,integer,integer) AS "
                + prepared_sql(z))
            _prepared.add(z)
        cur.execute(f"EXECUTE {name} (%s,%s,%s,%s,%s,%s)",
                    (z,x,y,z,x,y))
        raw = cur.fetchone()[0] or b""
    return z,x,y,bytes(raw)


def stratified_tasks():
    """Sample all five zooms, not just the dominant z16 workload."""
    tasks=[]
    for z,count in PILOT_BY_ZOOM.items():
        all_cells=list(vector.xyz_tasks(BBOX,z,z))
        stride=max(1,len(all_cells)//count)
        pick=all_cells[::stride][:count]
        if len(set(pick))!=len(pick):
            raise RuntimeError("Pilot generated duplicate XYZ")
        tasks.extend(pick)
    return tasks


def explain_samples():
    """Read-only actual query plans for inner/corner tiles at z12, z14, z16."""
    import psycopg2
    out=[]
    conn=psycopg2.connect()
    conn.autocommit=True
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '120s'")
            cur.execute("SET default_transaction_read_only = on")
            for z in (12,14,16):
                xmin,xmax,ymin,ymax=vector.xyz_range(BBOX,z)
                samples=[("center",(xmin+xmax)//2,(ymin+ymax)//2),
                         ("northwest",xmin,ymin),("southeast",xmax,ymax)]
                for kind,x,y in samples:
                    query=vector.query_for_zoom(z,projected=True)
                    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "+query,
                                (z,x,y,z,x,y))
                    raw=cur.fetchone()[0]
                    plan=raw if isinstance(raw,list) else json.loads(raw)
                    data=plan[0]
                    nodes=[]
                    scans=[]
                    def walk(node):
                        tag=node.get("Node Type","")
                        nodes.append(tag)
                        if any(s in tag for s in ("Scan","Sort","Aggregate")):
                            scans.append({
                                "kind":tag,
                                "relation":node.get("Relation Name"),
                                "index":node.get("Index Name"),
                                "actual_rows":node.get("Actual Rows"),
                                "actual_total_ms":node.get("Actual Total Time"),
                                "temp_written_blocks":node.get("Temp Written Blocks",0)
                            })
                        for sub in node.get("Plans",[]):
                            walk(sub)
                    walk(data["Plan"])
                    out.append({
                        "zoom":z,"sample":kind,"xyz":[z,x,y],
                        "planning_ms":data.get("Planning Time"),
                        "execution_ms":data.get("Execution Time"),
                        "root_temp_read_blocks":data["Plan"].get("Temp Read Blocks",0),
                        "root_temp_written_blocks":data["Plan"].get("Temp Written Blocks",0),
                        "root_shared_hit_blocks":data["Plan"].get("Shared Hit Blocks",0),
                        "root_shared_read_blocks":data["Plan"].get("Shared Read Blocks",0),
                        "node_types":dict(Counter(nodes)),
                        "scans":scans
                    })
                    print("SQL_EXPLAIN",json.dumps({
                        "zoom":z,"sample":kind,
                        "planning_ms":data.get("Planning Time"),
                        "execution_ms":data.get("Execution Time"),
                        "temp_write":data["Plan"].get("Temp Written Blocks",0),
                        "index_scans":sum(1 for n in nodes if "Index" in n),
                        "seq_scans":sum(1 for n in nodes if n=="Seq Scan")}),
                        flush=True)
    finally:
        conn.close()
    return out


def pilot(mode,tasks,workers):
    ctx=multiprocessing.get_context("fork")
    start=time.perf_counter()
    rows={}
    with ctx.Pool(processes=workers,
                  initializer=vector.init_worker if mode=="baseline" else init_prepared) as pool:
        func=vector.render_one if mode=="baseline" else render_prepared
        for z,x,y,mvt in pool.imap_unordered(func,tasks,chunksize=1):
            key=(z,x,y)
            if key in rows:
                raise RuntimeError("Duplicate XYZ from pilot SQL")
            blob=gzip.compress(mvt,compresslevel=6,mtime=0) if mvt else b""
            rows[key]=hashlib.sha256(blob).hexdigest()
    if len(rows)!=len(tasks):
        raise RuntimeError("Incomplete pilot SQL tiles")
    return time.perf_counter()-start,rows


def manifest(path):
    with sqlite3.connect(path) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
            raise RuntimeError("SQLite integrity failed")
        meta=dict(conn.execute("SELECT name,value FROM metadata"))
        meta.pop("name",None)
        rows={}
        for z,x,y,data in conn.execute(
                "SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles"):
            key=(int(z),int(x),(1<<int(z))-1-int(y))
            if key in rows:
                raise RuntimeError("Duplicate persisted XYZ")
            if not data or data[:2]!=b"\x1f\x8b":
                raise RuntimeError("Expected compressed MVT")
            rows[key]=hashlib.sha256(data).hexdigest()
    return meta,rows


def export_prepared(out,workers):
    """Byte-for-byte production export structure, only SQL is server PREPARE."""
    partial=out.with_name(out.name+".partial")
    partial.unlink(missing_ok=True)
    ctx=multiprocessing.get_context("fork")
    started=time.perf_counter()
    total=vector.tile_count(BBOX,LO,HI)
    seen=written=0
    with ctx.Pool(processes=workers,initializer=init_prepared) as pool:
        with sqlite3.connect(partial) as conn:
            vector.create_db(conn,vector.metadata(out.stem,BBOX,LO,HI))
            pending=0
            for z,x,y,pbf in pool.imap_unordered(
                    render_prepared,vector.xyz_tasks(BBOX,LO,HI),chunksize=1):
                seen+=1
                if pbf:
                    conn.execute("INSERT INTO tiles VALUES (?,?,?,?)",
                                 (z,x,(1<<z)-1-y,sqlite3.Binary(
                                      gzip.compress(pbf,compresslevel=6,mtime=0))))
                    pending+=1
                    written+=1
                if pending>=128:
                    conn.commit()
                    pending=0
            if seen!=total:
                raise RuntimeError("Incomplete prepared XYZ output")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
            if conn.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
                raise RuntimeError("Prepared MBTiles integrity check failed")
    os.replace(partial,out)
    return round(time.perf_counter()-started,3),written


def main():
    a=argparse.ArgumentParser()
    a.add_argument("--pbf",type=Path,required=True)
    a.add_argument("--work",type=Path,required=True)
    a.add_argument("--workers",type=int,default=4)
    args=a.parse_args()
    work=args.work.resolve()
    if (os.environ.get("PGHOST")!=str(work/"socket")
            or os.environ.get("PGDATABASE")!="gis"
            or not str(work).startswith("/tmp/osm2mbtiles-sql-")
            or not (work/"pgdata"/"postmaster.pid").is_file()):
        raise RuntimeError("Refusing a non-disposable PostGIS target")
    if os.environ.get("OSM2MBTILES_VECTOR_QUERY_MODE")!="projected":
        raise RuntimeError("Projected SQL mode required")
    if not 1<=args.workers<=8:
        raise ValueError("Unsupported worker count")
    digest=hashlib.sha256()
    with args.pbf.open("rb") as stream:
        for b in iter(lambda:stream.read(8*1024*1024),b""):
            digest.update(b)
    if digest.hexdigest()!=PBF_SHA:
        raise RuntimeError("Berlin PBF SHA256 differs from pinned reference")
    report={"source_sha":FROZEN_SHA,"pbf_sha256":PBF_SHA,
            "bbox":list(BBOX),"zoom":[LO,HI],"tile_workers":args.workers,
            "planned_xyz":vector.tile_count(BBOX,LO,HI),
            "status":"RUNNING","sql_plans":[],"pilot":[],"full":[],
            "adopt_production":False}
    def save():
        (work/"sql-probe.json").write_text(json.dumps(report,indent=2)+"\n")
    save()
    try:
        report["sql_plans"]=explain_samples()
        save()
        tasks=stratified_tasks()
        report["pilot_task_count"]=len(tasks)
        pilot_times={"baseline":[],"prepared":[]}
        for n in range(3):
            per={}
            for mode in (("baseline","prepared") if n%2==0 else
                         ("prepared","baseline")):
                elapsed,digests=pilot(mode,tasks,args.workers)
                pilot_times[mode].append(round(elapsed,3))
                per[mode]=digests
                report["pilot"].append({
                    "iteration":n+1,"profile":mode,
                    "wall_s":round(elapsed,3),"cells":len(digests)})
                print("SQL_PILOT",json.dumps(report["pilot"][-1]),flush=True)
                save()
            delta=[xyz for xyz in per["baseline"]
                   if per["baseline"][xyz]!=per["prepared"].get(xyz)]
            if delta:
                report["status"]="BYTE_PARITY_BLOCKED"
                report["pilot_first_mismatches"]=delta[:12]
                save()
                print("SQL_PROBE_RESULT",json.dumps({
                    "status":report["status"],
                    "pilot_mismatches":len(delta),
                    "plans":len(report["sql_plans"])}),flush=True)
                return
        med={k:round(statistics.median(v),3) for k,v in pilot_times.items()}
        gain=100*(med["baseline"]-med["prepared"])/med["baseline"]
        report["pilot_summary"]={"median_s":med,"speedup_pct":round(gain,2),
                                  "strict_gzip_parity":True}
        if gain<MIN_GAIN_PCT:
            report["status"]="NO_MEANINGFUL_PILOT_GAIN"
            save()
            print("SQL_PROBE_RESULT",json.dumps({
                "status":report["status"],"pilot":report["pilot_summary"],
                "plans":len(report["sql_plans"])}),flush=True)
            return
        # Only a strict, >=5% pilot win warrants expensive full 17k-XYZ A/B.
        full_times={"baseline":[],"prepared":[]}
        for n in range(3):
            signatures={}
            archives=[]
            order=("baseline","prepared") if n%2==0 else ("prepared","baseline")
            for mode in order:
                out=work/f"sql-{mode}-{n+1}.mbtiles"
                if mode=="baseline":
                    before=time.perf_counter()
                    ntiles=vector.export(args.pbf,out,BBOX,LO,HI,args.workers)
                    elapsed=round(time.perf_counter()-before,3)
                else:
                    elapsed,ntiles=export_prepared(out,args.workers)
                meta,rows=manifest(out)
                if ntiles!=12047 or len(rows)!=12047:
                    raise RuntimeError("Berlin nonempty tile count drift")
                signatures[mode]=(meta,rows)
                archives.append(out)
                full_times[mode].append(elapsed)
                report["full"].append({"iteration":n+1,"profile":mode,
                                       "wall_s":elapsed,"nonempty":len(rows)})
                print("SQL_FULL",json.dumps(report["full"][-1]),flush=True)
                save()
            base,other=signatures["baseline"],signatures["prepared"]
            if base!=other:
                report["status"]="FULL_BYTE_PARITY_BLOCKED"
                report["full_first_mismatches"]=[
                    x for x in base[1] if base[1][x]!=other[1].get(x)][:20]
                save()
                return
            for out in archives:
                out.unlink()
        summary={k:round(statistics.median(v),3) for k,v in full_times.items()}
        percent=100*(summary["baseline"]-summary["prepared"])/summary["baseline"]
        report["full_summary"]={"median_s":summary,
            "speedup_pct":round(percent,2),"strict_gzip_parity":True}
        report["status"]="FULL_CANDIDATE_PASS" if percent>=MIN_GAIN_PCT else "FULL_GAIN_BELOW_THRESHOLD"
        save()
        print("SQL_PROBE_RESULT",json.dumps({
            "status":report["status"],"pilot":report["pilot_summary"],
            "full":report["full_summary"]}),flush=True)
    except BaseException as exc:
        report["status"]="ERROR"
        report["error"]=repr(exc)
        save()
        raise


if __name__=="__main__":
    main()
