"""Frozen Berlin baseline comparison must reject changed datasets or tile counts."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
BASELINE = json.loads((HERE / "benchmarks/baselines/berlin-stress-vector-postgis-20261102.json").read_text())
spec = importlib.util.spec_from_file_location(
    "compare_berlin_baseline", HERE / "benchmarks/compare_berlin_baseline.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def sample():
    inp = BASELINE["input"]
    export = BASELINE["export"]
    rows = [
        {"profile": "legacy", "repeat": i + 1, "status": "PASS",
         "wall_s": t, "nonempty_tiles": export["nonempty_xyz"],
         "tile_bytes": export["total_gzip_tile_data_bytes"]}
        for i, t in enumerate(export["wall_seconds"])
    ]
    parity = [
        {"candidate": "legacy", "repeat": i + 1,
         "exact_gzip_and_xyz": True, "gzip_mismatches": 0,
         "xyz_equal": True, "metadata_equal": True,
         "reference_tiles": export["nonempty_xyz"],
         "candidate_tiles": export["nonempty_xyz"]}
        for i in range(export["repeats"])
    ]
    return {"region": inp["region"], "pbf_sha256": inp["sha256"],
            "source_pbf_size_bytes": inp["size_bytes"],
            "bbox": inp["bbox"], "zooms": inp["zoom"],
            "planned_xyz": inp["planned_xyz"], "workers": BASELINE["setup"]["tile_workers"],
            "status": "PASS_EXPLORATORY_ONLY", "repeats": export["repeats"],
            "trials": rows, "comparisons": parity,
            "production_sha": BASELINE["evidence"]["production_commit_sha"]}


def test_baseline_exact_self_comparison_is_diagnostic_only():
    r = module.compare(BASELINE, sample())
    assert r["status"] == "COMPARABLE_HISTORICAL_ONLY"
    assert r["baseline_export_median_s"] == 124.938
    assert r["candidate_export_median_s"] == 124.938
    assert r["observed_time_reduction_pct"] == 0
    assert not r["production_adoption_approved"]
    assert not r["cross_revision_same_database_byte_parity_verified"]


@pytest.mark.parametrize("key,bad", [
    ("pbf_sha256", "not-the-same-pbf"),
    ("source_pbf_size_bytes", 1),
    ("region", "monaco"),
    ("bbox", [0, 0, 1, 1]),
    ("zooms", [8, 14]),
    ("planned_xyz", 87),
    ("workers", 2),
])
def test_change_in_workload_must_refuse_comparison(key,bad):
    r = sample()
    r[key] = bad
    with pytest.raises(module.IncomparableBenchmark, match="Cannot compare"):
        module.compare(BASELINE, r)


def test_tile_count_or_tile_bytes_drift_is_not_performance_win():
    for name, bad in [("nonempty_tiles", 12046), ("tile_bytes", 42)]:
        r = sample()
        r["trials"][0][name] = bad
        with pytest.raises(module.IncomparableBenchmark):
            module.compare(BASELINE, r)


def test_failure_or_incomplete_parity_refused():
    r = sample()
    r["comparisons"][0]["gzip_mismatches"] = 1
    with pytest.raises(module.IncomparableBenchmark):
        module.compare(BASELINE, r)
    r = sample()
    r["status"] = "BYTE_PARITY_BLOCKED"
    with pytest.raises(module.IncomparableBenchmark):
        module.compare(BASELINE, r)
    r = sample()
    r["repeats"] = 1
    with pytest.raises(module.IncomparableBenchmark):
        module.compare(BASELINE, r)


def test_historical_5pct_signal_is_not_approval():
    r = sample()
    for t in r["trials"]:
        t["wall_s"] *= 0.90
    summary = module.compare(BASELINE, r)
    assert summary["historical_signal"] == "candidate_faster_historical"
    assert summary["observed_time_reduction_pct"] > 9
    assert not summary["production_adoption_approved"]
