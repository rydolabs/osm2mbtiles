#!/usr/bin/env python3
"""Disposable Berlin PostGIS partial-GiST index A/B, no production schema change.

Create partial GiST indexes for the exact existing SQL predicates, then
re-explain identical frozen XYZ samples. This is only a candidate filter:
a full 3+ paired export + gzip parity is still required before adoption.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from berlin_sql_plan_profile import selected_xyz, plan_stats
from osm2mbtiles import vector

INDEXES = (
    ("probe_lines_road_gist", "vector_lines",
     "tags ? 'highway' AND tags->>'highway' NOT IN ('footway','cycleway','path','steps','pedestrian','bridleway')"),
    ("probe_lines_pathway_gist", "vector_lines",
     "tags->>'highway' IN ('footway','cycleway','path','steps','pedestrian','bridleway')"),
    ("probe_areas_building_gist", "vector_areas",
     "tags ? 'building' AND tags->>'building' <> 'no'"),
    ("probe_points_poi_gist", "vector_points",
     "tags ?| ARRAY['amenity','shop','tourism','office','craft','historic','leisure']"),
    ("probe_areas_landuse_gist", "vector_areas", "tags ? 'landuse'"),
)


def compare(before, after):
    old={tuple(r["xyz"]):r for r in before["records"]}
    new={tuple(r["xyz"]):r for r in after}
    if old.keys()!=new.keys():
        raise ValueError("XYZ mismatch")
    rows=[]
    for xyz in sorted(old):
        a,b=old[xyz],new[xyz]
        rows.append({"xyz":xyz,"baseline_ms":a["execution_ms"],
                     "partial_index_ms":b["execution_ms"],
                     "ratio":round(b["execution_ms"]/max(a["execution_ms"],0.001),3),
                     "new_index_names":sorted(set(x["index"] for x in b["plan_stats"]["scan_relations"]
                                                   if x.get("index") and x["index"].startswith("probe_")))})
    import statistics
    center=[r for r in rows if tuple(r["xyz"]) in {
        (z,x,y) for z,x,y in selected_xyz()[2::5]}]
    return {"rows":rows,"median_baseline_ms":round(statistics.median(r["baseline_ms"] for r in rows),3),
            "median_candidate_ms":round(statistics.median(r["partial_index_ms"] for r in rows),3),
            "sum_baseline_ms":round(sum(r["baseline_ms"] for r in rows),3),
            "sum_candidate_ms":round(sum(r["partial_index_ms"] for r in rows),3),
            "candidate_index_used_anywhere":any(r["new_index_names"] for r in rows),
            "center_tile_samples":center,
            "not_a_full_export_speedup":True}


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--baseline",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    original=json.loads(args.baseline.read_text())
    if original.get("status")!="PASS_PROFILE_ONLY":
        p.error("Baseline EXPLAIN did not pass")
    if len(original.get("records",[]))!=len(selected_xyz()):
        p.error("Unexpected baseline XYZ samples")
    import psycopg2
    result={"status":"RUNNING","index_build_seconds":None,
            "indexes":[{"name":name,"table":table,"predicate":predicate}
                       for name,table,predicate in INDEXES],
            "records":[],"comparison":None,
            "limitations":["Partial GiST indexes are built only on disposable DB.",
                           "Index construction overhead is excluded from tile query times.",
                           "A/B EXPLAIN order is fixed and subject to warm-cache bias.",
                           "This cannot approve a production change without complete export and byte parity."]}
    def save():args.output.write_text(json.dumps(result,indent=2)+"\n")
    save()
    try:
        with psycopg2.connect(dbname="gis") as conn:
            conn.autocommit=True
            with conn.cursor() as cur:
                start=time.perf_counter()
                for name,table,predicate in INDEXES:
                    cur.execute(f"CREATE INDEX {name} ON {table} USING GIST (geom) WHERE {predicate}")
                    cur.execute(f"ANALYZE {table}")
                result["index_build_seconds"]=round(time.perf_counter()-start,3)
                save()
                cur.execute("SET statement_timeout = 90000")
                for z,x,y in selected_xyz():
                    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "+
                                vector.query_for_zoom(z,projected=True),
                                (z,x,y,z,x,y))
                    raw=cur.fetchone()[0]
                    plan=raw[0] if isinstance(raw,list) else json.loads(raw)[0]
                    result["records"].append({
                        "xyz":[z,x,y],"planning_ms":round(plan["Planning Time"],3),
                        "execution_ms":round(plan["Execution Time"],3),
                        "plan_stats":plan_stats(plan["Plan"])})
                    save()
        result["comparison"]=compare(original,result["records"])
        result["status"]="PASS_EXPLORATORY_ONLY"
        save()
        print("BERLIN_PARTIAL_INDEX_AB",json.dumps({
            "build_s":result["index_build_seconds"],
            "sum_baseline_ms":result["comparison"]["sum_baseline_ms"],
            "sum_candidate_ms":result["comparison"]["sum_candidate_ms"],
            "median_baseline_ms":result["comparison"]["median_baseline_ms"],
            "median_candidate_ms":result["comparison"]["median_candidate_ms"],
            "used":result["comparison"]["candidate_index_used_anywhere"],
            "center_tiles":result["comparison"]["center_tile_samples"]}),flush=True)
    except BaseException as exc:
        result["status"]="FAIL"
        result["error"]=str(exc)
        save()
        raise


if __name__=="__main__":
    main()
