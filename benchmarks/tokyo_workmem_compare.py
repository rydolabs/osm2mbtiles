#!/usr/bin/env python3
"""True A-B-B-A same-DB PostgreSQL work_mem test and exact gzip MVT byte parity."""
from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import statistics
import sys
from pathlib import Path

RUNS = ("baseline1", "mem16_1", "mem16_2", "baseline2")
METRICS = ("blks_hit", "blks_read", "temp_bytes", "temp_files",
           "blk_read_time", "blk_write_time", "tup_returned", "tup_fetched")


def snapshot(dst):
    import psycopg2
    with psycopg2.connect() as db:
        db.autocommit = True
        with db.cursor() as cur:
            cur.execute("SELECT pg_stat_clear_snapshot()")
            cur.execute("SELECT " + ",".join(METRICS) +
                        " FROM pg_stat_database WHERE datname=current_database()")
            values = cur.fetchone()
    Path(dst).write_text(json.dumps(dict(zip(METRICS, values))) + "\n")


def profile(work):
    import psycopg2
    from osm2mbtiles.vector_batch import batch_query, microbatches
    rows = {}
    with psycopg2.connect() as db:
        with db.cursor() as cur:
            for z in (12, 14, 16):
                _, cells = next(microbatches((139.55,35.55,139.95,35.85),z,z,2))
                params = [item for xy in cells for item in xy]
                for mode in ("8MB", "16MB"):
                    cur.execute("SELECT set_config('work_mem', %s, false)", (mode,))
                    cur.execute("SHOW work_mem")
                    assert cur.fetchone()[0] == mode
                    q = batch_query(z, len(cells))
                    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + q, params)
                    plan = cur.fetchone()[0][0]
                    nodes = []
                    def walk(node, depth=0):
                        nodes.append({
                            "depth": depth,
                            "type": node.get("Node Type"),
                            "cte": node.get("CTE Name"),
                            "subplan": node.get("Subplan Name"),
                            "time_ms": node.get("Actual Total Time", 0),
                            "loops": node.get("Actual Loops", 0),
                            "temp_written_blocks": node.get("Temp Written Blocks", 0),
                            "temp_read_blocks": node.get("Temp Read Blocks", 0),
                            "shared_read_blocks": node.get("Shared Read Blocks", 0),
                        })
                        for child in node.get("Plans", ()):
                            walk(child, depth + 1)
                    walk(plan["Plan"])
                    rows[f"{z}-{mode}"] = {
                        "zoom": z, "work_mem": mode, "tiles": cells,
                        "planning_ms": plan.get("Planning Time"),
                        "execution_ms": plan.get("Execution Time"),
                        "shared_hit_blocks": plan["Plan"].get("Shared Hit Blocks", 0),
                        "shared_read_blocks": plan["Plan"].get("Shared Read Blocks", 0),
                        "temp_read_blocks": plan["Plan"].get("Temp Read Blocks", 0),
                        "temp_written_blocks": plan["Plan"].get("Temp Written Blocks", 0),
                        "top_temp_nodes": sorted(nodes,key=lambda x:x["temp_written_blocks"], reverse=True)[:12],
                        "top_time_nodes": sorted(nodes,key=lambda x:x["time_ms"]*x["loops"], reverse=True)[:12],
                    }
    (work / "explain-samples.json").write_text(json.dumps(rows, indent=2) + "\n")


def inspect(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", path
    metadata = dict(db.execute("SELECT name, value FROM metadata"))
    n = db.execute("SELECT count(*) FROM tiles").fetchone()[0]
    return db, metadata, n


def parity(reference, candidate):
    ref, ref_meta, ref_count = inspect(reference)
    test, test_meta, test_count = inspect(candidate)
    select = ("SELECT zoom_level,tile_column,tile_row,tile_data "
              "FROM tiles ORDER BY zoom_level,tile_column,tile_row")
    bad = []
    num_bad = 0
    for old, new in itertools.zip_longest(ref.execute(select), test.execute(select)):
        if old != new:
            num_bad += 1
            if len(bad) < 8:
                bad.append({
                    "reference_xyz_tms": list(old[:3]) if old else None,
                    "candidate_xyz_tms": list(new[:3]) if new else None,
                    "reference_payload_sha": hashlib.sha256(old[3]).hexdigest() if old else None,
                    "candidate_payload_sha": hashlib.sha256(new[3]).hexdigest() if new else None,
                })
    ref.close()
    test.close()
    return {
        "ok": num_bad == 0 and ref_meta == test_meta and ref_count == test_count,
        "metadata_equal": ref_meta == test_meta,
        "original_rows": ref_count, "candidate_rows": test_count,
        "mismatching_rows": num_bad, "first_mismatches": bad,
    }


def report(work):
    measures, stats = {}, {}
    snaps = {name: json.loads((work / (name + "-pgstat.json")).read_text())
             for name in ("before", *RUNS)}
    for prior, current in zip(("before", *RUNS[:-1]), RUNS):
        sec, max_kb = (work / (current + ".time")).read_text().split()
        measures[current] = {"elapsed_seconds": float(sec), "max_rss_kb": int(max_kb)}
        stats[current] = {k: snaps[current][k] - snaps[prior][k] for k in METRICS}
    reference = work / "baseline1/tokyo.mbtiles"
    comparisons = {
        run: parity(reference, work / run / "tokyo.mbtiles")
        for run in RUNS[1:]
    }
    valid = all(v["ok"] for v in comparisons.values())
    a_times = [measures[k]["elapsed_seconds"] for k in ("baseline1", "baseline2")]
    b_times = [measures[k]["elapsed_seconds"] for k in ("mem16_1", "mem16_2")]
    a_median, b_median = statistics.median(a_times), statistics.median(b_times)
    a_temp = statistics.median(stats[k]["temp_bytes"] for k in ("baseline1","baseline2"))
    b_temp = statistics.median(stats[k]["temp_bytes"] for k in ("mem16_1","mem16_2"))
    data = {
        "byte_exact_and_metadata": valid,
        "source_sha256": (work / "input.sha256").read_text().split()[0],
        "baseline_commit": (work / "baseline-commit.txt").read_text().strip(),
        "candidate_commit": (work / "candidate-commit.txt").read_text().strip(),
        "requested_xyz_tiles": 6863, "workers": 4,
        "bbox": [139.55,35.55,139.95,35.85], "minzoom": 12, "maxzoom": 16,
        "modes": {"baseline": "work_mem 8MB (original main)",
                  "mem16": "work_mem 16MB (unchanged PostGIS SQL)"},
        "order": list(RUNS),
        "times": measures, "db_stat_deltas": stats, "parity": comparisons,
        "baseline_median_seconds": a_median, "mem16_median_seconds": b_median,
        "delta_seconds": round(b_median - a_median, 3),
        "delta_percent": round((b_median / a_median - 1) * 100, 2),
        "baseline_median_temp_mib": round(a_temp / 1048576, 2),
        "mem16_median_temp_mib": round(b_temp / 1048576, 2),
        "caveats": [
            "Only two executions per method and one hosted runner: small differences may be noise.",
            "A-B-B-A balances but does not remove cache warming bias.",
            "pg_stat_database stats can lag and include other connections/background activity.",
            "EXPLAIN uses only one representative SQL batch per zoom, taken AFTER timed exports.",
            "All four runs share identical imported PostGIS data; import/download excluded from times.",
            "Physical SQLite file SHA can vary with write order; every compressed tile and metadata field is tested instead."
        ],
    }
    (work / "cte-parity.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    lines = [
        "# Tokyo PostGIS feature work_mem A–B–B–A: exact parity",
        "",
        "- Single Kanto PBF source SHA256: " + data["source_sha256"],
        "- Same imported PostGIS DB, 4 workers, 2x2 batches, z12–16, bbox 139.55,35.55,139.95,35.85.",
        "- A: materialized feature work_mem (original); B: NOT MATERIALIZED feature work_mem.",
        "",
        "| Run | work_mem | Vector seconds | PostgreSQL temp MiB | Shared blocks read |",
        "|---|---|---:|---:|---:|",
    ]
    for name in RUNS:
        s = measures[name]["elapsed_seconds"]
        t = stats[name]["temp_bytes"] / 1048576
        n = stats[name]["blks_read"]
        lines.append(f"| {name} | {'16MB' if name.startswith('mem16') else '8MB'} | {s:.2f} | {t:,.1f} | {n:,} |")
    lines.extend([
        "",
        f"**A median {a_median:.2f}s; B median {b_median:.2f}s; time delta {data['delta_percent']:+.2f}%** (negative means B faster).",
        f"**PostgreSQL temp written:** A median {data['baseline_median_temp_mib']:.1f} MiB; B median {data['mem16_median_temp_mib']:.1f} MiB.",
        "",
        "| Output vs baseline1 | Tiles | Different compressed bytes/rows | Metadata same | Status |",
        "|---|---:|---:|---|---|",
    ])
    for name, p in comparisons.items():
        lines.append(f"| {name} | {p['candidate_rows']:,} | {p['mismatching_rows']} | {p['metadata_equal']} | {'PASS' if p['ok'] else 'FAIL'} |")
    lines += [
        "",
        "**Overall exact compressed tile content, coordinates, metadata: " + ("PASS" if valid else "FAIL") + "**",
        "",
        "The SQL is identical in every run; only the per-connection work_mem setting changes. "
        "Actual stable performance improvements require independent replication.",
        ""
    ]
    (work / "cte-parity.md").write_text("\n".join(lines))
    print("TOKYO_work_mem_PARITY", "PASS" if valid else "FAIL", flush=True)
    print("TOKYO_work_mem_TIMINGS", json.dumps({
        "old": a_times, "new": b_times, "median_delta_percent": data["delta_percent"],
        "baseline_temp_mib": data["baseline_median_temp_mib"],
        "mem16_temp_mib": data["mem16_median_temp_mib"],
        "mismatches": {k: v["mismatching_rows"] for k,v in comparisons.items()}
    }), flush=True)
    if not valid:
        raise SystemExit("FAIL: output changed; do not merge experimental SQL optimization")


if __name__ == "__main__":
    work = Path(sys.argv[2])
    if sys.argv[1] == "snapshot":
        snapshot(work / (sys.argv[3] + "-pgstat.json"))
    elif sys.argv[1] == "profile":
        profile(work)
    elif sys.argv[1] == "report":
        report(work)
    else:
        raise SystemExit("Usage: tokyo_cte_compare.py snapshot|profile|report WORK [RUN]")
