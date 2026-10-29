#!/usr/bin/env python3
"""Summarize measured Tokyo vector benchmark outputs; never fabricate absent results."""
from __future__ import annotations
import gzip
import json
import sqlite3
import sys
from pathlib import Path
from osm2mbtiles.tiles import xyz_range, tile_count

BBOX = (139.55, 35.55, 139.95, 35.85)
ZOOMS = range(12, 17)
ENGINES = ("osm2mbtiles", "planetiler", "tilemaker")


def timed(work: Path, name: str):
    path = work / (name + ".time")
    if not path.is_file():
        return None
    v = path.read_text().strip().split()
    if len(v) != 2:
        raise ValueError("Expected elapsed seconds and peak RSS kB: " + str(path))
    return {"elapsed_seconds": float(v[0]), "peak_rss_kb": int(v[1])}


def summarize_db(path: Path):
    with sqlite3.connect(path) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok", (str(path), integrity)
        metadata = dict(db.execute("SELECT name,value FROM metadata"))
        count = db.execute("SELECT count(*) FROM tiles").fetchone()[0]
        by_zoom = db.execute("SELECT zoom_level, count(*) FROM tiles GROUP BY zoom_level").fetchall()
        assert count > 0, "Zero tiles: " + str(path)
        out_of_bounds = 0
        for z, x, row in db.execute("SELECT zoom_level,tile_column,tile_row FROM tiles"):
            if z not in ZOOMS:
                out_of_bounds += 1
                continue
            x0, x1, y0, y1 = xyz_range(BBOX, z)
            y = (1 << z) - 1 - row
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                out_of_bounds += 1
        assert not out_of_bounds, (str(path), out_of_bounds)
        z = 14
        x, y = 14552, 6451
        sample = db.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (z, x, (1 << z) - 1 - y)
        ).fetchone()
        sample_layers = []
        if sample:
            try:
                import mapbox_vector_tile
                data = sample[0]
                if data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                sample_layers = sorted(mapbox_vector_tile.decode(data))
            except Exception as ex:
                sample_layers = ["decoding failed: " + str(ex)]
    return {
        "tiles": count,
        "tiles_by_zoom": {str(z): n for z, n in by_zoom},
        "mbtiles_bytes": path.stat().st_size,
        "metadata_format": metadata.get("format"),
        "out_of_requested_xyz_cells": out_of_bounds,
        "sample_tile_xyz": [14, 14552, 6451],
        "sample_layers": sample_layers,
    }


def main(work: Path):
    report = {
        "status": "PASS",
        "mode": "single-run pilot; NOT a 3+ repeat scientific ranking",
        "area": "Tokyo urban region, Japan; input full Geofabrik Kanto extract",
        "input_url": "https://download.geofabrik.de/asia/japan/kanto-latest.osm.pbf",
        "input_sha256": (work / "input.sha256").read_text().split()[0],
        "source_bytes": (work / "kanto.osm.pbf").stat().st_size,
        "bbox": list(BBOX),
        "zoom": [min(ZOOMS), max(ZOOMS)],
        "candidate_xyz_cells": tile_count(BBOX, min(ZOOMS), max(ZOOMS)),
        "postgis_initialization_and_import": timed(work, "osm2mbtiles-import"),
        "engines": {},
        "fairness_warnings": [
            "Original PBF is the whole Kanto extract. SQL imports full Kanto although tiles cover Tokyo only.",
            "Native profile differences cause varying feature selection; this is NOT equal-workload parity.",
            "Planetiler uses corrected source classification derived from custom Seoul profile; not official OMT.",
            "Tilemaker uses OMT-like profile with shapefile-sourced layers omitted.",
            "One run only; cache and GitHub-hosted VM variation can be substantial.",
            "osm2mbtiles cold time is measured import+DB init and separate tile-write elapsed SUM.",
            "Downloads, package installs and JVM installation are excluded from timing.",
            "Output integrity + bbox/zoom validated; nonempty tile counts may differ.",
        ],
    }
    for name in ENGINES:
        path = work / (name + ".mbtiles")
        if not path.is_file():
            raise RuntimeError("Missing output: " + str(path))
        data = summarize_db(path)
        data["timing"] = timed(work, name)
        report["engines"][name] = data
    initial = report["postgis_initialization_and_import"]
    render = report["engines"]["osm2mbtiles"]["timing"]
    report["osm2mbtiles_cold_component_sum_seconds"] = round(
        initial["elapsed_seconds"] + render["elapsed_seconds"], 3
    )
    (work / "tokyo-benchmark.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    lines = [
        "# Tokyo urban z12-16 real three-engine single-run pilot",
        "",
        "Same Kanto PBF SHA256: " + report["input_sha256"],
        "bbox 139.55,35.55,139.95,35.85, 6,863 candidate XYZ cells.",
        "",
        "| Engine | Timed interval | Elapsed (s) | Max RSS (MiB) | Nonempty tiles | Output (MiB) |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for name in ENGINES:
        data = report["engines"][name]
        t = data["timing"]
        lines.append(
            f"| {name} | {'tile stage after import' if name == 'osm2mbtiles' else 'PBF to MBTiles'} "
            f"| {t['elapsed_seconds']:.2f} | {t['peak_rss_kb']/1024:.1f} "
            f"| {data['tiles']:,} | {data['mbtiles_bytes']/1024/1024:.1f} |"
        )
    lines += [
        "",
        f"**osm2mbtiles PostGIS initialization + import:** {initial['elapsed_seconds']:.2f}s; "
        f"**initialization + import + tile-stage component sum:** "
        f"{report['osm2mbtiles_cold_component_sum_seconds']:.2f}s "
        "(not a single continuous process measurement).",
        "",
        "This is ONE pilot run per engine; do not generalize these timings.",
        "Tile type MVT/PBF only, with non-equivalent output profiles.",
    ]
    (work / "tokyo-benchmark.md").write_text("\n".join(lines) + "\n")
    print("TOKYO_THREE_ENGINE_PILOT_PASS", json.dumps({
        k: {"seconds": v["timing"]["elapsed_seconds"], "tiles": v["tiles"]}
        for k, v in report["engines"].items()
    }), flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
