#!/usr/bin/env python3
"""Read-only EXPLAIN ANALYZE for frozen Berlin projected MVT SQL.

No writes to production schema, no output change. Uses same exact query_for_zoom
as production. EXPLAIN ANALYZE executes SELECTs but returns only JSON plans.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import time

from osm2mbtiles import vector
from osm2mbtiles.tiles import xyz_range

BBOX = (13.088, 52.338, 13.761, 52.676)
ZOOMS = (12, 13, 14, 15, 16)
SOURCE_SHA256 = "51d3845df2e4b1334ebec7cf6516e41e71735ce2fc1c0975c52d57f58f1952ad"
BASELINE_EXPORT_MEDIAN_S = 124.938


def selected_xyz(bbox=BBOX, zooms=ZOOMS):
    """Repeatable 5 tile locations per zoom; deduplicate tiny low-zoom grids."""
    result = []
    for z in zooms:
        xmin,xmax,ymin,ymax = xyz_range(bbox,z)
        candidates = [
            (xmin,ymin), (xmax,ymax), ((xmin+xmax)//2,(ymin+ymax)//2),
            (xmin,ymax), (xmax,ymin),
        ]
        seen = set()
        for x,y in candidates:
            if (x,y) not in seen:
                result.append((z,x,y))
                seen.add((x,y))
    return result


def plan_stats(node):
    """Recursively sum plan-node metrics; note inclusive time must not be summed."""
    found = {"nodes":0,"seq_scans":0,"index_scans":0,
             "bitmap_index_scans":0,"sorts":0,"hashes":0,
             "temp_read_blocks":0,"temp_written_blocks":0,
             "shared_read_blocks":0,"shared_hit_blocks":0,
             "disk_sort_nodes":0,"scan_relations":[]}
    def visit(n):
        kind=n.get("Node Type","")
        found["nodes"]+=1
        if kind=="Seq Scan":
            found["seq_scans"]+=1
        if kind in ("Index Scan","Index Only Scan"):
            found["index_scans"]+=1
        if kind=="Bitmap Index Scan":
            found["bitmap_index_scans"]+=1
        if kind=="Sort":
            found["sorts"]+=1
            if n.get("Sort Space Type")=="Disk":
                found["disk_sort_nodes"]+=1
        if kind=="Hash":
            found["hashes"]+=1
        for src,dest in (("Temp Read Blocks","temp_read_blocks"),
                         ("Temp Written Blocks","temp_written_blocks"),
                         ("Shared Read Blocks","shared_read_blocks"),
                         ("Shared Hit Blocks","shared_hit_blocks")):
            # EXPLAIN BUFFERS values on parent nodes are inclusive; record
            # per-node values but do NOT sum them as total I/O.
            found[dest]=max(found[dest],int(n.get(src,0)))
        if "Relation Name" in n:
            found["scan_relations"].append({
                "relation":n["Relation Name"],"node":kind,
                "actual_rows":n.get("Actual Rows"),
                "actual_loops":n.get("Actual Loops"),
                "rows_removed_by_filter":n.get("Rows Removed by Filter"),
                "index":n.get("Index Name")})
        for child in n.get("Plans",[]):
            visit(child)
    visit(node)
    return found


def report_plans(records):
    grouped={}
    for z in ZOOMS:
        values=[r["execution_ms"] for r in records if r["xyz"][0]==z]
        if values:
            grouped[str(z)]={"n":len(values),"median_execution_ms":round(statistics.median(values),3),
                             "max_execution_ms":round(max(values),3),
                             "sum_execution_ms":round(sum(values),3)}
    return {"per_zoom":grouped,
            "top_slowest_xyz":sorted(records,key=lambda r:r["execution_ms"],reverse=True)[:10],
            "total_sample_execution_ms":round(sum(r["execution_ms"] for r in records),3),
            "total_sample_planning_ms":round(sum(r["planning_ms"] for r in records),3)}


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--pbf",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--statement-timeout-ms",type=int,default=90000)
    args=p.parse_args()
    if args.statement_timeout_ms<1000 or args.statement_timeout_ms>180000:
        p.error("statement timeout must be 1-180 seconds")
    if os.environ.get("PGDATABASE")!="gis" or not os.environ.get("PGHOST","").startswith("/tmp/osm2mbtiles-sql-"):
        p.error("Refusing to profile any non-disposable DB")
    sha=hashlib.sha256(args.pbf.read_bytes()).hexdigest()
    if sha!=SOURCE_SHA256:
        p.error("Frozen Berlin PBF checksum differs")
    import psycopg2
    result={"status":"RUNNING","dataset":"berlin-stress",
            "pbf_sha256":sha,"pbf_size_bytes":args.pbf.stat().st_size,
            "bbox":BBOX,"zooms":ZOOMS,"query_mode":"projected",
            "baseline_export_median_s":BASELINE_EXPORT_MEDIAN_S,
            "source_sql_sha256":hashlib.sha256(
                Path(vector.__file__).read_bytes()).hexdigest(),
            "records":[],"summary":None,
            "limitations":["EXPLAIN ANALYZE includes profiling overhead and does not transfer full MVT to Python.",
                           "Samples do not represent all 16943 cells or prove an optimization.",
                           "BUFFERS values are inclusive at parent plan nodes; never sum across tree.",
                           "Read-only on a fresh disposable Berlin PostGIS database."]}
    def save():
        args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    save()
    try:
        with psycopg2.connect(dbname="gis") as conn:
            conn.set_session(readonly=True,autocommit=True)
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = %s",(args.statement_timeout_ms,))
                for z,x,y in selected_xyz():
                    q=vector.query_for_zoom(z,projected=True)
                    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "+q,
                                (z,x,y,z,x,y))
                    raw=cur.fetchone()[0]
                    plan=raw[0] if isinstance(raw,list) else json.loads(raw)[0]
                    stats=plan_stats(plan["Plan"])
                    record={"xyz":[z,x,y],"planning_ms":round(plan.get("Planning Time",0),3),
                            "execution_ms":round(plan["Execution Time"],3),
                            "plan_stats":stats}
                    result["records"].append(record)
                    save()
                    print("BERLIN_SQL_SAMPLE",json.dumps({
                        "xyz":record["xyz"],"planning_ms":record["planning_ms"],
                        "execution_ms":record["execution_ms"],
                        "seq_scans":stats["seq_scans"],"index_scans":stats["index_scans"],
                        "temp_read_blocks":stats["temp_read_blocks"],
                        "temp_written_blocks":stats["temp_written_blocks"]}),flush=True)
        result["summary"]=report_plans(result["records"])
        result["status"]="PASS_PROFILE_ONLY"
        save()
        print("BERLIN_SQL_PROFILE_COMPLETE",json.dumps({
            "samples":len(result["records"]),"per_zoom":result["summary"]["per_zoom"],
            "top_slowest_xyz":[{"xyz":x["xyz"],"execution_ms":x["execution_ms"]}
                               for x in result["summary"]["top_slowest_xyz"][:5]]}),flush=True)
    except BaseException as exc:
        result["status"]="FAIL"
        result["error"]=str(exc)
        save()
        raise


if __name__=="__main__":
    main()
