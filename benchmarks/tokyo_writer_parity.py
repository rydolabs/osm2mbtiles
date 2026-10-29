#!/usr/bin/env python3
"""Exact old/new Tokyo vector MBTiles content parity and single-run elapsed report."""
from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import sys
from pathlib import Path


def data(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", path
    info = dict(db.execute("SELECT name,value FROM metadata ORDER BY name"))
    tile_count = db.execute("SELECT count(*) FROM tiles").fetchone()[0]
    return db, info, tile_count


def timefile(path):
    seconds, rss = path.read_text().strip().split()
    return {"seconds": float(seconds), "max_rss_kb": int(rss)}


def main(work):
    a = work / "baseline/tokyo.mbtiles"
    b = work / "optimized/tokyo.mbtiles"
    assert a.exists() and b.exists(), "Both baseline and optimized MBTiles must exist"
    old, old_meta, old_count = data(a)
    new, new_meta, new_count = data(b)
    old_tiles = old.execute(
        "SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles "
        "ORDER BY zoom_level,tile_column,tile_row"
    )
    new_tiles = new.execute(
        "SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles "
        "ORDER BY zoom_level,tile_column,tile_row"
    )
    mismatches = []
    mismatches_total = 0
    for left, right in itertools.zip_longest(old_tiles, new_tiles):
        if left != right:
            mismatches_total += 1
            if len(mismatches) < 8:
                mismatches.append({
                    "xyz_tms": list(left[:3] if left else right[:3]),
                    "baseline_sha256": hashlib.sha256(left[3]).hexdigest() if left else None,
                    "optimized_sha256": hashlib.sha256(right[3]).hexdigest() if right else None,
                })
    old.close()
    new.close()
    old_sec = timefile(work / "baseline.time")
    new_sec = timefile(work / "optimized.time")
    result = {
        "baseline_sha": (work / "base-commit.txt").read_text().strip(),
        "optimized_sha": (work / "optimized-commit.txt").read_text().strip(),
        "pbf_sha256": (work / "input.sha256").read_text().split()[0],
        "bbox": [139.55, 35.55, 139.95, 35.85],
        "zooms": [12, 16],
        "workers": 4,
        "batch_side": 2,
        "old_tiles": old_count,
        "new_tiles": new_count,
        "metadata_equal": old_meta == new_meta,
        "tile_payload_mismatch_count": mismatches_total,
        "sample_mismatches": mismatches,
        "old_bytes": a.stat().st_size,
        "new_bytes": b.stat().st_size,
        "old_time": old_sec,
        "new_time": new_sec,
        "delta_seconds": round(new_sec["seconds"] - old_sec["seconds"], 3),
        "speedup_old_over_new": round(old_sec["seconds"] / new_sec["seconds"], 4),
        "valid": old_meta == new_meta and old_count == new_count and mismatches_total == 0,
        "caveat": "One sequential pair only; running baseline first can warm the OS/PostGIS cache for optimized. Timings include only tile export, not PBF download or initial PostGIS import.",
    }
    (work / "parity.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    lines = [
        "# Tokyo z12-16 Batch Writer before / after",
        "",
        "- Kanto PBF SHA256: " + result["pbf_sha256"],
        "- Both runs: same PostgreSQL DB, bbox, zooms, 4 workers, 2x2 SQL batches",
        "- BEFORE: old main SQL, gzip in writer, SQLite COMMIT every 128 nonempty tiles",
        "- AFTER: worker PREPARE, gzip in workers, 256-row executemany, SQLite COMMIT after 1024 nonempty tiles",
        "",
        "| Metric | Before | After |",
        "|---|---:|---:|",
        f"| Tile-only seconds | {old_sec['seconds']:.2f} | {new_sec['seconds']:.2f} |",
        f"| Nonempty tile rows | {old_count:,} | {new_count:,} |",
        f"| MBTiles file MiB | {a.stat().st_size/1048576:.2f} | {b.stat().st_size/1048576:.2f} |",
        f"| Max command RSS MiB | {old_sec['max_rss_kb']/1024:.1f} | {new_sec['max_rss_kb']/1024:.1f} |",
        "",
        f"**Metadata equality:** {old_meta == new_meta}; "
        f"**byte-for-byte tile payload mismatches:** {mismatches_total:,}; "
        f"**PASS:** {result['valid']}",
        "",
        f"**Delta:** {result['delta_seconds']:+.2f} seconds "
        "(negative means optimized ran faster).",
        "",
        "**Limitations:** ONE sequential before/after run, not a statistically stable "
        "benchmark. The second run may benefit from caches warmed by the first. "
        "PostGIS import, PBF download, runner setup excluded.",
        "",
    ]
    if mismatches:
        lines += ["First mismatches: " + json.dumps(mismatches), ""]
    (work / "parity.md").write_text("\n".join(lines))
    print("TOKYO_WRITER_CONTENT_PARITY", "PASS" if result["valid"] else "FAIL")
    print("TOKYO_WRITER_BEFORE_AFTER", json.dumps({
        "old_seconds": old_sec["seconds"], "new_seconds": new_sec["seconds"],
        "old_tiles": old_count, "new_tiles": new_count,
        "mismatches": mismatches_total, "metadata_equal": old_meta == new_meta,
    }))
    if not result["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
