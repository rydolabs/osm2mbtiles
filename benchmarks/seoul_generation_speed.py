#!/usr/bin/env python3
"""Compare measured one-tile runtime under aligned Seoul input/XYZ settings."""
import json
import sqlite3
import statistics
import sys
from pathlib import Path

Z, X, Y = 14, 13970, 6344
TMS_Y = (1 << Z) - 1 - Y


def seconds(path):
    return float(path.read_text(encoding="utf-8").strip())


def check(path):
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", path
        count = db.execute("SELECT count(*) FROM tiles").fetchone()[0]
        matching = db.execute(
            "SELECT count(*) FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (Z, X, TMS_Y),
        ).fetchone()[0]
        meta = dict(db.execute("SELECT name, value FROM metadata"))
    assert (count, matching) == (1, 1), (path, count, matching)
    return {"tiles": count, "target_matches": matching, "bytes": path.stat().st_size,
            "format": meta.get("format")}


def main(work):
    input_sha = (work / "input.sha256").read_text().split()[0]
    import_sec = seconds(work / "osm2mbtiles-import.seconds")
    records = {}
    for name in ("osm2mbtiles", "planetiler", "tilemaker"):
        runs = []
        for run in (1, 2):
            mbtiles = work / f"{name}-r{run}.mbtiles"
            runs.append({
                "repeat": run,
                "elapsed_seconds": seconds(work / f"{name}-r{run}.seconds"),
                **check(mbtiles),
            })
        records[name] = {
            "runs": runs,
            "median_seconds": statistics.median(row["elapsed_seconds"] for row in runs),
            "mean_seconds": statistics.mean(row["elapsed_seconds"] for row in runs),
        }
    output = {
        "input_sha256": input_sha, "xyz": [Z, X, Y],
        "source": "same Seoul OSM PBF on same ubuntu-24.04 GitHub Actions host",
        "repeats": 2, "workers": 4,
        "postgis_import_seconds": import_sec,
        "engine_runtime": records,
        "limits": [
            "Only 1 tile; process startup, PBF parsing and disk effects dominate.",
            "osm2mbtiles timed tile stage reuses PostGIS imported once; import separately reported.",
            "Planetiler and Tilemaker are standalone PBF->MBTiles including input parsing.",
            "PostGIS database setup/extension startup omitted from import measurement.",
            "Different layer profiles and feature counts; not equal output workload.",
            "All inputs, outputs, timing and semantic comparisons run on same CI host.",
            "Tilemaker uses --bbox to produce exactly one tile, unlike prior 512-tile comparison.",
            "Download, Java installation, apt setup excluded.",
        ],
    }
    (work / "same-tile-speed.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Seoul City Hall: 3-engine speed benchmark",
        "",
        f"OSM PBF SHA256: {input_sha}",
        f"Exact XYZ: {Z}/{X}/{Y}, output exactly **one tile** per run, **2 independent runs**.",
        "",
        "| Engine | Run 1 (s) | Run 2 (s) | Median (s) | Timed part |",
        "|---|---:|---:|---:|---|",
    ]
    for name, v in records.items():
        a, b = v["runs"]
        timed = ("tile-only after separate PostGIS import" if name == "osm2mbtiles"
                 else "standalone PBF to MBTiles")
        lines.append(
            f"| {name} | {a['elapsed_seconds']:.3f} | "
            f"{b['elapsed_seconds']:.3f} | {v['median_seconds']:.3f} | {timed} |"
        )
    lines.extend([
        "",
        f"PostGIS import + post-import SQL once: **{import_sec:.3f}s** "
        "(excludes DB init/extension setup).",
        "Import + repeated tile-only is NOT a like-for-like standalone E2E run.",
        "",
        "## Limits",
        *["- " + item for item in output["limits"]],
        "",
    ])
    (work / "speed_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("SEOUL_THREE_ENGINE_SPEED_PASS", json.dumps(
        {k: round(v["median_seconds"], 3) for k, v in records.items()} |
        {"postgis_import": round(import_sec, 3)}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
