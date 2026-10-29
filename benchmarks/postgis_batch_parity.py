#!/usr/bin/env python3
"""CI parity gate and measured PostGIS batch-vs-baseline-vs-Planetiler benchmark.

The PostGIS variants share ONE imported database per run. Planetiler uses the
same source PBF, bounding box and zooms, but a different YAML layer profile.
Never imply full content equivalence across Planetiler and PostGIS.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import subprocess
import time

import mapbox_vector_tile

from osm2mbtiles.vector import export as export_baseline
from osm2mbtiles.vector_batch import export_batch

BBOX = (126.95, 37.47, 127.13, 37.62)
LO, HI = 12, 15


def content_signature(path: Path):
    """Per-tile multiset: id + type + geometry + properties + multiplicity."""
    result = {}
    layers = collections.Counter()
    features = 0
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", path
        metadata = dict(db.execute("SELECT name,value FROM metadata"))
        assert metadata["format"] == "pbf", path
        for z, x, y, blob in db.execute(
                "SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles"):
            tile = (z, x, (1 << z)-1-y)
            assert tile not in result, ("duplicate tile", tile)
            assert blob[:2] == b"\x1f\x8b", ("not gzipped", tile)
            decoded = mapbox_vector_tile.decode(gzip.decompress(blob))
            counts = collections.Counter()
            for name, layer in decoded.items():
                for feat in layer["features"]:
                    key = json.dumps(
                        [name, feat.get("id"), feat.get("type"),
                         feat["geometry"], feat["properties"]],
                        sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                    counts[key] += 1
                    layers[name] += 1
                    features += 1
            result[tile] = counts
    return result, metadata, {
        "tiles": len(result), "features": features,
        "layers": dict(sorted(layers.items())), "file_bytes": path.stat().st_size,
    }


def compare_strict(baseline: Path, batch: Path):
    before, meta_a, stats_a = content_signature(baseline)
    after, meta_b, stats_b = content_signature(batch)
    # Name tracks output filename, not the style. All other metadata must match.
    meta_a.pop("name", None)
    meta_b.pop("name", None)
    errors = []
    for tile in sorted(before.keys() | after.keys()):
        x = before.get(tile, collections.Counter())
        y = after.get(tile, collections.Counter())
        if x != y:
            errors.append({
                "xyz": tile, "missing_features": sum((x-y).values()),
                "extra_features": sum((y-x).values()),
            })
    return {
        "equal": not errors and meta_a == meta_b,
        "metadata_equal_except_name": meta_a == meta_b,
        "different_tiles": len(errors), "first_mismatches": errors[:12],
        "baseline_stats": stats_a, "batch_stats": stats_b,
        "baseline_file_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
        "batch_file_sha256": hashlib.sha256(batch.read_bytes()).hexdigest(),
        "comparison": "decoded layer, feature ID, geometry type, coordinates, "
                      "all properties, multiplicity and XYZ key; ignores feature order",
    }


def planetiler_stats(path: Path):
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        meta = dict(db.execute("SELECT name,value FROM metadata"))
        assert meta["format"] == "pbf"
        layers = collections.Counter()
        ntiles = 0
        for (blob,) in db.execute("SELECT tile_data FROM tiles"):
            ntiles += 1
            decoded = mapbox_vector_tile.decode(
                gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob)
            for name, layer in decoded.items():
                layers[name] += len(layer["features"])
        assert ntiles, "Planetiler empty output"
    return {"tiles": ntiles, "features": sum(layers.values()),
            "layers": dict(sorted(layers.items())), "file_bytes": path.stat().st_size}


def report_md(report):
    lines = [
        "# PostGIS regional SQL batch: strict parity and Planetiler timing",
        "",
        "Input SHA-256: `" + report["input_sha256"] + "`",
        "",
        "BBOX: `" + str(BBOX) + "`, zoom: 12–15, 4 PostGIS workers, batch side: 2.",
        "",
        "|Run|Baseline tile only (s)|Batch tile only (s)|Strict semantic parity|Planetiler E2E (s)|",
        "|---:|---:|---:|---|---:|",
    ]
    for r in report["runs"]:
        lines.append(f"|{r['repeat']}|{r['baseline_sec']:.3f}|{r['batch_sec']:.3f}|"
                     f"{'PASS' if r['parity']['equal'] else 'FAIL'}|{r['planetiler_sec']:.3f}|")
    lines.extend([
        "",
        "**Median measured times (s):** baseline tile-only "
        f"{statistics.median(r['baseline_sec'] for r in report['runs']):.3f}; "
        "batch tile-only "
        f"{statistics.median(r['batch_sec'] for r in report['runs']):.3f}; "
        "Planetiler PBF-to-MBTiles E2E "
        f"{statistics.median(r['planetiler_sec'] for r in report['runs']):.3f}.",
        "",
        f"Shared PostGIS import (one-time, separate from tile runs): {report['import_sec']:.3f}s.",
        "",
        "**Scope:** Both PostGIS outputs are compared at every XYZ tile by the multiset "
        "of layer, decoded feature ID, geometry/type, full properties and duplicate count. "
        "Metadata is checked excluding the archive filename. gzip/SQLite file hashes "
        "need not match (feature order/SQLite physical layout may differ).",
        "",
        "**Planetiler limitation:** same input PBF, bbox, zoom and runner, but custom "
        "Planetiler YAML and PostGIS SQL use DIFFERENT feature/layer rules; "
        "the Planetiler time is an observational speed reference, NOT equal-output throughput. "
        "PostGIS tile-only must not be compared as E2E to Planetiler E2E. "
        "PostGIS import + tiles is also not a like-for-like comparison because "
        "PostGIS creates reusable tables.",
        "",
        "The batch SQL is an experimental optimization; accept it only if parity "
        "passes and the measured throughput justifies its DB/memory cost.",
    ])
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--project", type=Path, required=True)
    p.add_argument("--planetiler", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--import-sec", type=float, required=True)
    p.add_argument("--repeats", type=int, default=2)
    a = p.parse_args()
    assert a.repeats >= 2
    a.work.mkdir(parents=True, exist_ok=True)
    pbf = a.input.resolve()
    jar = a.planetiler.resolve()
    schema = (a.project / "benchmarks/seoul-planetiler.yml").resolve()
    assert pbf.is_file() and jar.is_file() and schema.is_file()
    # Use original baseline SQL mode. No existing production code is modified.
    os.environ["OSM2MBTILES_VECTOR_QUERY_MODE"] = "baseline"
    report = {
        "input_sha256": hashlib.sha256(pbf.read_bytes()).hexdigest(),
        "input_bytes": pbf.stat().st_size,
        "import_sec": a.import_sec,
        "protocol": {
            "bbox": BBOX, "zoom": [LO, HI], "workers": 4,
            "batch_side": 2, "repeats": a.repeats,
            "postgis_mode": "baseline, fresh independent vector import once",
            "planetiler_version": "v0.10.2",
            "planetiler_schema": str(schema.name),
        },
        "runs": [],
    }
    outfile = a.work / "parity-results.json"
    for repeat in range(1, a.repeats + 1):
        first, second = ("baseline", "batch") if repeat % 2 else ("batch", "baseline")
        times = {}
        paths = {}
        for mode in (first, second):
            out = a.work / f"r{repeat}-{mode}.mbtiles"
            paths[mode] = out
            start = time.perf_counter()
            if mode == "baseline":
                export_baseline(pbf, out, BBOX, LO, HI, 4)
            else:
                export_batch(pbf, out, BBOX, LO, HI, 4, side=2)
            times[mode] = round(time.perf_counter()-start, 3)
            print("TILE_RUN", repeat, mode, times[mode], flush=True)
        parity = compare_strict(paths["baseline"], paths["batch"])
        print("STRICT_PARITY", repeat, json.dumps({
            "equal": parity["equal"], "different_tiles": parity["different_tiles"],
            "baseline_stats": parity["baseline_stats"],
            "batch_stats": parity["batch_stats"],
            "first_mismatches": parity["first_mismatches"]
        }), flush=True)
        # Persist the comparison before asserting, so failed CI has evidence.
        sample = {"repeat": repeat, "baseline_sec": times["baseline"],
                  "batch_sec": times["batch"], "parity": parity,
                  "planetiler_sec": None, "planetiler_stats": None}
        report["runs"].append(sample)
        outfile.write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n")
        if not parity["equal"]:
            raise AssertionError(f"Semantic baseline parity failed in repeat {repeat}")
        for path in paths.values():
            path.unlink()
        tmp = a.work / f"r{repeat}-planetiler-temp"
        tmp.mkdir(exist_ok=True)
        (tmp / "seoul.osm.pbf").symlink_to(pbf)
        planet_path = a.work / f"r{repeat}-planetiler.mbtiles"
        cmd = [
            "java", "-Xmx3g", "-jar", str(jar), "generate-custom",
            "--schema=" + str(schema), "--output=" + str(planet_path),
            "--bounds=" + ",".join(map(str, BBOX)),
            "--minzoom=" + str(LO), "--maxzoom=" + str(HI),
            "--threads=4", "--tmpdir=" + str(tmp/"intermediate"), "--force",
        ]
        start = time.perf_counter()
        with (a.work / f"r{repeat}-planetiler.log").open("w") as logfile:
            subprocess.run(cmd, cwd=tmp, check=True, stdout=logfile,
                           stderr=subprocess.STDOUT, timeout=300)
        sample["planetiler_sec"] = round(time.perf_counter()-start, 3)
        sample["planetiler_stats"] = planetiler_stats(planet_path)
        planet_path.unlink()
        shutil.rmtree(tmp)
        outfile.write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n")
        print("PLANETILER_E2E", repeat, sample["planetiler_sec"], flush=True)
    (a.work / "summary.md").write_text(report_md(report))
    print("STRICT_PARITY_AND_BENCHMARK_PASS", outfile, flush=True)


if __name__ == "__main__":
    main()
