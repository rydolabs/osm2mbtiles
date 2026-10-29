#!/usr/bin/env python3
"""Tokyo Z11 locality A-B-B-A benchmark and byte-exact output parity report."""
from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ORDER = ("baseline1", "spatial1", "spatial2", "baseline2")
STATS = ("blks_hit", "blks_read", "temp_bytes", "temp_files",
         "blk_read_time", "blk_write_time", "tup_returned", "tup_fetched")


def snapshot(path: Path):
    import psycopg2
    with psycopg2.connect() as db:
        db.autocommit = True
        with db.cursor() as cur:
            cur.execute("SELECT pg_stat_clear_snapshot()")
            cur.execute(
                "SELECT blks_hit,blks_read,temp_bytes,temp_files,"
                "blk_read_time,blk_write_time,tup_returned,tup_fetched "
                "FROM pg_stat_database WHERE datname = current_database()"
            )
            row = cur.fetchone()
    path.write_text(json.dumps(dict(zip(STATS, row))) + "\n")


def explain(work: Path):
    """Capture two sample query plans AFTER all timing runs."""
    import psycopg2
    from osm2mbtiles.vector_batch import batch_query, microbatches
    output = {}
    with psycopg2.connect() as db:
        with db.cursor() as cur:
            for z in (14, 16):
                _, cells = next(microbatches(
                    (139.55, 35.55, 139.95, 35.85), z, z, 2))
                params = [v for xy in cells for v in xy]
                cur.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                    + batch_query(z, len(cells)), params)
                plan = cur.fetchone()[0][0]
                output[str(z)] = {
                    "first_microbatch": cells,
                    "planning_ms": plan.get("Planning Time"),
                    "execution_ms": plan.get("Execution Time"),
                    "top_plan_node": plan["Plan"]["Node Type"],
                    "top_plan_buffers": {
                        k: plan["Plan"].get(k) for k in
                        ("Shared Hit Blocks", "Shared Read Blocks",
                         "Temp Written Blocks", "Temp Read Blocks")
                    },
                    "note": "Only one sample plan; not representative of all tile density.",
                }
    (work / "representative-explain.json").write_text(json.dumps(output, indent=2) + "\n")


def open_mbtiles(path: Path):
    assert path.is_file(), path
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", path
    metadata = dict(conn.execute("SELECT name, value FROM metadata"))
    count = conn.execute("SELECT count(*) FROM tiles").fetchone()[0]
    return conn, metadata, count


def compare_pair(reference: Path, candidate: Path):
    left, left_meta, left_count = open_mbtiles(reference)
    right, right_meta, right_count = open_mbtiles(candidate)
    q = ("SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles "
         "ORDER BY zoom_level,tile_column,tile_row")
    mismatches = []
    different_count = 0
    for a, b in itertools.zip_longest(left.execute(q), right.execute(q)):
        if a != b:
            different_count += 1
            if len(mismatches) < 5:
                mismatches.append({
                    "baseline_coord": list(a[:3]) if a else None,
                    "candidate_coord": list(b[:3]) if b else None,
                    "baseline_payload_sha256": hashlib.sha256(a[3]).hexdigest() if a else None,
                    "candidate_payload_sha256": hashlib.sha256(b[3]).hexdigest() if b else None,
                })
    left.close()
    right.close()
    return {
        "metadata_equal": left_meta == right_meta,
        "reference_tiles": left_count,
        "candidate_tiles": right_count,
        "different_rows": different_count,
        "sample_differences": mismatches,
        "pass": (left_meta == right_meta and left_count == right_count
                 and different_count == 0)
    }


def compare(work: Path):
    times, db_deltas, snapshots = {}, {}, {}
    for name in ("before", *ORDER):
        snapshots[name] = json.loads((work / f"{name}-pgstat.json").read_text())
    for previous, current in zip(("before", *ORDER[:-1]), ORDER):
        t, rss = (work / f"{current}.time").read_text().strip().split()
        times[current] = {"elapsed_s": float(t), "max_command_rss_kb": int(rss)}
        db_deltas[current] = {
            k: round(snapshots[current][k] - snapshots[previous][k], 4)
            for k in STATS}
    ref = work / "baseline1/tokyo.mbtiles"
    matches = {
        name: compare_pair(ref, work / name / "tokyo.mbtiles")
        for name in ("spatial1", "spatial2", "baseline2")
    }
    old_times = [times[x]["elapsed_s"] for x in ("baseline1", "baseline2")]
    new_times = [times[x]["elapsed_s"] for x in ("spatial1", "spatial2")]
    old_med, new_med = statistics.median(old_times), statistics.median(new_times)
    valid = all(v["pass"] for v in matches.values())
    report = {
        "status": "PASS" if valid else "FAIL",
        "pbf_sha256": (work / "input.sha256").read_text().split()[0],
        "baseline_source_sha": (work / "base-commit.txt").read_text().strip(),
        "spatial_source_sha": (work / "spatial-commit.txt").read_text().strip(),
        "bbox": [139.55, 35.55, 139.95, 35.85],
        "zoom": [12, 16], "workers": 4, "batch_side": 2,
        "spatial_group_zoom": 11, "spatial_max_microbatches": 32,
        "order": list(ORDER),
        "timings": times,
        "pg_database_stats_deltas": db_deltas,
        "equality": matches,
        "same_metadata_and_all_compressed_tile_bytes": valid,
        "baseline_median_seconds": old_med,
        "spatial_median_seconds": new_med,
        "delta_spatial_minus_baseline_seconds": round(new_med - old_med, 3),
        "change_percent": round((new_med / old_med - 1) * 100, 2),
        "caveats": [
            "ABBA is two observations each in a hosted VM, not five independent cold-cache runs.",
            "pg_stat_database counters are cumulative database-level estimates; stats reporting may lag.",
            "The scheduler changes order and worker affinity only; unchanged SQL batches can have different cache footprints.",
            "Adaptive subdivision uses SQL batch count, not actual OSM geometry density.",
            "Input download, one PostGIS import, and post-run EXPLAIN are excluded from timed vector stages."
        ],
    }
    (work / "spatial-parity.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    text = [
        "# Tokyo 2x2 SQL baseline vs Z11 adaptive locality (A-B-B-A)",
        "",
        "Input PBF SHA256: " + report["pbf_sha256"] + "; "
        "shared PostGIS DB; z12-16; bbox 139.55,35.55,139.95,35.85; 4 workers.",
        "",
        "| Run order | Mode | Export seconds | Command RSS MiB | PG shared hit | PG shared read | PG temp MiB |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for name in ORDER:
        t, pg = times[name], db_deltas[name]
        text.append(
            f"| {name} | {'spatial z11' if name.startswith('spatial') else 'scanline'} "
            f"| {t['elapsed_s']:.2f} | {t['max_command_rss_kb']/1024:.1f} "
            f"| {pg['blks_hit']:,} | {pg['blks_read']:,} | {pg['temp_bytes']/1048576:.1f} |")
    text += [
        "",
        f"**Baseline median:** {old_med:.2f}s; **spatial median:** {new_med:.2f}s; "
        f"**change:** {report['change_percent']:+.2f}% (negative is faster).",
        "",
        "| Exact byte + metadata parity (vs baseline1) | Tile rows | Payload differences | Metadata equal | Status |",
        "|---|---:|---:|---|---|"
    ]
    for name, v in matches.items():
        text.append(
            f"| {name} | {v['candidate_tiles']:,} | {v['different_rows']} | "
            f"{v['metadata_equal']} | {'PASS' if v['pass'] else 'FAIL'} |")
    text.extend([
        "", f"**Overall byte-identical tile content and metadata: {valid}.**",
        "",
        "The SQLite physical MBTiles file SHA/length may vary due to write order. "
        "Equality is checked by every (z,x,TMS-y,tile_data) row and metadata.",
        "",
        "This isolates scheduling; PostGIS SQL, import profile, gzip and SQLite writer are unchanged. "
        "PG stats may include PostgreSQL-background activity.",
        ""
    ])
    (work / "spatial-parity.md").write_text("\n".join(text))
    print("TOKYO_SPATIAL_EXACT_PARITY", "PASS" if valid else "FAIL", flush=True)
    print("TOKYO_SPATIAL_ABBA", json.dumps({
        "baseline_s": old_times, "spatial_s": new_times,
        "median_change_pct": report["change_percent"],
        "exact_equal": valid,
        "different_rows": {k: v["different_rows"] for k, v in matches.items()}
    }), flush=True)
    if not valid:
        raise SystemExit("MBTiles content difference: see spatial-parity.json")


if __name__ == "__main__":
    work = Path(sys.argv[2])
    if sys.argv[1] == "snapshot":
        snapshot(work / f"{sys.argv[3]}-pgstat.json")
    elif sys.argv[1] == "explain":
        explain(work)
    elif sys.argv[1] == "compare":
        compare(work)
    else:
        raise SystemExit("usage: tokyo_spatial_compare.py snapshot|explain|compare WORK [snapshot-name]")
