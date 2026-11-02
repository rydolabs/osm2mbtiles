"""Read-only Berlin SQL profiling helper tests; no PostGIS required."""
import importlib.util
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("berlin_sql_plan_profile",
    ROOT/"benchmarks/berlin_sql_plan_profile.py")
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_frozen_berlin_sample_coverage():
    points=mod.selected_xyz()
    assert len(points)>=15
    assert len(points)==len(set(points))
    assert {p[0] for p in points}==set(mod.ZOOMS)
    for z,x,y in points:
        xmin,xmax,ymin,ymax=mod.xyz_range(mod.BBOX,z)
        assert xmin<=x<=xmax and ymin<=y<=ymax


def test_plan_stats_do_not_double_count_inclusive_buffers():
    tree={"Node Type":"Aggregate","Temp Read Blocks":12,
          "Temp Written Blocks":8,"Shared Hit Blocks":200,
          "Plans":[
            {"Node Type":"Sort","Temp Read Blocks":9,
             "Temp Written Blocks":5,"Sort Space Type":"Disk",
             "Plans":[{"Node Type":"Seq Scan","Relation Name":"vector_lines",
                       "Actual Rows":3,"Actual Loops":2,
                       "Rows Removed by Filter":40,
                       "Shared Hit Blocks":100}]},
            {"Node Type":"Index Scan","Relation Name":"vector_areas",
             "Index Name":"vector_areas_geom_idx","Actual Rows":2,
             "Actual Loops":1,"Shared Hit Blocks":70}]}
    x=mod.plan_stats(tree)
    assert x["nodes"]==4
    assert x["seq_scans"]==1 and x["index_scans"]==1
    assert x["disk_sort_nodes"]==1
    assert x["temp_read_blocks"]==12 and x["temp_written_blocks"]==8
    assert x["shared_hit_blocks"]==200
    assert x["scan_relations"][0]["relation"]=="vector_lines"
    assert x["scan_relations"][1]["index"]=="vector_areas_geom_idx"


def test_rank_by_actual_execution_ms():
    rows=[{"xyz":[12,1,1],"planning_ms":1,"execution_ms":20},
          {"xyz":[12,2,2],"planning_ms":2,"execution_ms":10},
          {"xyz":[16,3,3],"planning_ms":3,"execution_ms":5}]
    result=mod.report_plans(rows)
    assert result["top_slowest_xyz"][0]["xyz"]==[12,1,1]
    assert result["per_zoom"]["12"]["median_execution_ms"]==15
    assert result["total_sample_planning_ms"]==6
