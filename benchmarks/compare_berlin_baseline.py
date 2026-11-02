#!/usr/bin/env python3
"""Compare future Berlin-stress vector exporter runs with frozen 2026 baseline.

This is a *historical* regression signal, not proof that different GitHub
runners are comparable, and not a substitute for same-DB, cross-version gzip
byte identity when the MVT SQL/output implementation changes.

Usage:
  python benchmarks/compare_berlin_baseline.py --result path/to/export-io-profile.json
  python benchmarks/compare_berlin_baseline.py --result ... --profile worker-gzip-batch \
      --output path/to/berlin-comparison.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics

HERE = Path(__file__).resolve().parent
DEFAULT_BASELINE = HERE / "baselines" / "berlin-stress-vector-postgis-20261102.json"


class IncomparableBenchmark(ValueError):
    """The candidate is not the exact frozen Berlin workload."""


def _require(value: bool, message: str):
    if not value:
        raise IncomparableBenchmark(message)


def compare(baseline: dict, result: dict, profile: str = "legacy") -> dict:
    """Fail closed on scope/input/tile mismatches; report speed only as observed."""
    _require(baseline.get("schema_version") == 1, "Unsupported baseline schema")
    _require(baseline.get("measurement_scope") == "warm_vector_tile_export_only",
             "Baseline is not a warm tile-export benchmark")
    inp = baseline["input"]
    config = baseline["setup"]
    recorded = baseline["export"]
    for key, expected in (
        ("region", inp["region"]),
        ("pbf_sha256", inp["sha256"]),
        ("source_pbf_size_bytes", inp["size_bytes"]),
        ("bbox", inp["bbox"]),
        ("zooms", inp["zoom"]),
        ("planned_xyz", inp["planned_xyz"]),
        ("workers", config["tile_workers"]),
    ):
        _require(result.get(key) == expected,
                 f"Cannot compare: {key} does not match frozen Berlin reference")
    _require(result.get("status") == "PASS_EXPLORATORY_ONLY",
             "Candidate run did not finish all experiment checks")
    _require(result.get("repeats", 0) >= 3,
             "Need at least three repetitions per candidate profile")
    rows = [r for r in result.get("trials", []) if r.get("profile") == profile]
    _require(len(rows) == result["repeats"],
             "Missing or duplicate candidate profile trials")
    _require({r.get("repeat") for r in rows} == set(range(1, result["repeats"] + 1)),
             "Missing or duplicate repetition indices")
    _require(all(r.get("status") == "PASS" and r.get("wall_s", 0) > 0 for r in rows),
             "Failed candidate trial or invalid duration")
    _require(all(r.get("nonempty_tiles") == recorded["nonempty_xyz"] for r in rows),
             "Tile count changed; do not claim performance equivalence")
    _require(all(r.get("tile_bytes") == recorded["total_gzip_tile_data_bytes"] for r in rows),
             "Compressed payload size changed; byte parity must be investigated")
    parity = [r for r in result.get("comparisons", []) if r.get("candidate") == profile]
    _require(len(parity) == result["repeats"] and all(
        r.get("exact_gzip_and_xyz") is True and r.get("gzip_mismatches") == 0
        and r.get("xyz_equal") is True and r.get("metadata_equal") is True
        and r.get("reference_tiles") == recorded["nonempty_xyz"]
        and r.get("candidate_tiles") == recorded["nonempty_xyz"]
        for r in parity), "Current-run same-DB byte/XYZ parity is not proven")
    samples = sorted((r["wall_s"] for r in rows))
    median = round(statistics.median(samples), 3)
    previous = recorded["median_seconds"]
    reduction = round((previous - median) / previous * 100, 2)
    # Historical fixed-number differences can be caused by environment drift.
    signal = ("candidate_faster_historical" if reduction >= 5.0 else
              "candidate_slower_historical" if reduction <= -5.0 else
              "not_a_meaningful_historical_difference")
    return {
        "status": "COMPARABLE_HISTORICAL_ONLY",
        "benchmark_id": baseline["benchmark_id"],
        "profile": profile,
        "baseline_run_url": baseline["evidence"]["workflow_run_url"],
        "baseline_source_commit": baseline["evidence"]["production_commit_sha"],
        "candidate_source_commit_reported": result.get("production_sha"),
        "pbf_sha256": inp["sha256"],
        "nonempty_xyz": recorded["nonempty_xyz"],
        "baseline_export_median_s": previous,
        "candidate_export_samples_s": [r["wall_s"] for r in rows],
        "candidate_export_median_s": median,
        "observed_time_reduction_pct": reduction,
        "historical_signal": signal,
        "within_candidate_run_gzip_parity": True,
        "cross_revision_same_database_byte_parity_verified": False,
        "same_runner_ab_verified": False,
        "production_adoption_approved": False,
        "caveat": "Historical hosted-runner timings cannot establish a speedup."
                  " For production changes run pinned baseline and candidate on"
                  " the same machine/DB, check every gzip/XYZ payload, and"
                  " record cold full PBF-to-MBTiles time separately.",
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    p.add_argument("--result", type=Path, required=True)
    p.add_argument("--profile", default="legacy")
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    trial = json.loads(args.result.read_text(encoding="utf-8"))
    try:
        summary = compare(baseline, trial, args.profile)
    except IncomparableBenchmark as exc:
        p.error(str(exc))
    formatted = json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(formatted, encoding="utf-8")
    print(formatted, end="")


if __name__ == "__main__":
    main()
