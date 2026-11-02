"""A/B partial index profile never claims whole-export speedups."""
import importlib.util
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("berlin_partial_index_ab",
    ROOT/"benchmarks/berlin_partial_index_ab.py")
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_sample_comparison_tracks_index_usage_and_no_adoption():
    base={"records":[{"xyz":[12,1,1],"execution_ms":10}]}
    candidate=[{"xyz":[12,1,1],"execution_ms":5,
                "plan_stats":{"scan_relations":[
                    {"relation":"vector_lines","index":"probe_lines_road_gist"}]}}]
    r=mod.compare(base,candidate)
    assert r["candidate_index_used_anywhere"]
    assert r["sum_baseline_ms"]==10
    assert r["sum_candidate_ms"]==5
    assert r["not_a_full_export_speedup"]


def test_mismatched_tiles_refused():
    import pytest
    with pytest.raises(ValueError,match="XYZ mismatch"):
        mod.compare({"records":[{"xyz":[12,1,1],"execution_ms":1}]},[])
