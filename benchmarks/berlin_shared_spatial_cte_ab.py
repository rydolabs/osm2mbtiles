#!/usr/bin/env python3
"""Disposable Berlin A/B: share per-tile PostGIS spatial candidates across layers.

The candidate is benchmark-only. The production SQL/exporter are never edited.
First verify all sampled raw MVT bytes and query timings; only if promising,
export the full 16,943 XYZ grid in three same-DB baseline/candidate pairs.
"""
from __future__ import annotations

import argparse
from collections import Counter
from itertools import zip_longest
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import time

from osm2mbtiles import vector
from osm2mbtiles.tiles import xyz_range

BBOX = (13.088, 52.338, 13.761, 52.676)
LO, HI = 12, 16
PBF_SHA = "51d3845df2e4b1334ebec7cf6516e41e71735ce2fc1c0975c52d57f58f1952ad"
PBF_BYTES = 99604748
REQUESTED_XYZ = 16943
NONEMPTY_XYZ = 12047
PGHOST = "/tmp/osm2mbtiles-shared-berlin/socket"
WORKERS = 4
REPEATS = 3


def query_shared_candidates(z: int, *, cached: bool = False,
                            projected: bool = False) -> str:
    """Keep layer branches, predicates, attributes, and MVT concatenation intact.

    Only the relation supplying t changes: tables referenced by multiple
    layers use one MATERIALIZED spatial-candidate CTE per tile. No sorting
    or serializer changes are introduced. Byte equivalence is NOT assumed.
    """
    if cached or not projected:
        raise ValueError("This experiment supports frozen projected SQL only")
    specs = list(vector.layer_specs(z))
    counts = Counter(table for _, table, _, _ in specs)
    shared = {}
    ctes = ["""b AS (
      SELECT ST_TileEnvelope(%s, %s, %s) AS tile,
             ST_TileEnvelope(%s, %s, %s, margin => 64.0 / 4096) AS search
    )"""]
    for _, table, id_col, _ in specs:
        if counts[table] <= 1 or table in shared:
            continue
        # Identifiers come exclusively from the trusted frozen layer plan.
        alias = "shared_" + table
        shared[table] = alias
        ctes.append(
            f"{alias} AS MATERIALIZED ("
            f"SELECT t.{id_col}, t.geom, t.tags FROM {table} t "
            f"CROSS JOIN b WHERE t.geom && b.search)"
        )
    grouped = {}
    for name, table, key, predicate in specs:
        props = vector._properties("t", z, name, key, projected=True)
        source = shared.get(table, table)
        spatial = "" if table in shared else "t.geom && b.search AND "
        branch = (
            f"""SELECT ST_AsMVTGeom(t.geom, b.tile, {vector.EXTENT}, {vector.BUFFER}, true) AS geom,
                          {props} AS properties
                     FROM {source} t
                    WHERE {spatial}({predicate})"""
        )
        grouped.setdefault(name, []).append(branch)
    parts = []
    for name, branches in grouped.items():
        query = " UNION ALL ".join(branches)
        parts.append(
            f"""COALESCE((
          SELECT ST_AsMVT(q, '{name}', {vector.EXTENT}, 'geom')
            FROM ({query}) q WHERE q.geom IS NOT NULL
        ), ''::bytea)"""
        )
    if not parts:
        return "SELECT ''::bytea"
    return "WITH " + ",\n".join(ctes) + "\n SELECT " + " || ".join(parts) + " FROM b"


def select_xyz():
    result = []
    for z in range(LO, HI + 1):
        xmin, xmax, ymin, ymax = xyz_range(BBOX, z)
        coords = ((xmin, ymin), (xmax, ymax),
                  ((xmin + xmax) // 2, (ymin + ymax) // 2),
                  (xmin, ymax), (xmax, ymin))
        for x, y in dict.fromkeys(coords):
            result.append((z, x, y))
    return result


def require_disposable(pbf: Path):
    if (os.environ.get("PGHOST") != PGHOST or
            os.environ.get("PGUSER") != "sharedprobe" or
            os.environ.get("PGDATABASE") != "gis"):
        raise RuntimeError("Refusing any non-disposable PostgreSQL instance")
    h = hashlib.sha256()
    with pbf.open("rb") as f:
        for buf in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(buf)
    if pbf.stat().st_size != PBF_BYTES or h.hexdigest() != PBF_SHA:
        raise RuntimeError("Pinned Berlin PBF bytes/SHA changed")
    if vector.tile_count(BBOX, LO, HI) != REQUESTED_XYZ:
        raise RuntimeError("Berlin XYZ grid drift")


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def sample(pbf: Path, out: Path):
    import psycopg2
    require_disposable(pbf)
    samples = select_xyz()
    report = {
        "status": "RUNNING", "scope": "25 representative Berlin XYZ, not full export",
        "pbf_sha256": PBF_SHA, "bbox": BBOX, "zooms": [LO, HI],
        "sql_mode": "projected", "samples": [list(x) for x in samples],
        "repeats": REPEATS, "trials": [], "explain": [],
        "raw_mvt_mismatch_count": 0, "mismatch_examples": [],
        "limitations": [
            "25 fixed corners/centers are not representative of the full XYZ distribution.",
            "Serial SQL timings can differ from a four-process tile export.",
            "Alternating order reduces, but does not eliminate, cache effects."
        ]
    }
    write_json(out, report)
    base = {z: vector.query_for_zoom(z, projected=True) for z in range(LO, HI + 1)}
    alt = {z: query_shared_candidates(z, projected=True) for z in range(LO, HI + 1)}
    with psycopg2.connect(dbname="gis") as conn:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 90000")
            for trial in range(REPEATS):
                timings = {"baseline": [], "candidate": []}
                centers = {"baseline": [], "candidate": []}
                for index, (z, x, y) in enumerate(samples):
                    order = ("baseline", "candidate") if (index + trial) % 2 == 0 \
                        else ("candidate", "baseline")
                    raw = {}
                    for mode in order:
                        query = base[z] if mode == "baseline" else alt[z]
                        t0 = time.perf_counter()
                        cur.execute(query, (z, x, y, z, x, y))
                        raw[mode] = bytes(cur.fetchone()[0] or b"")
                        ms = (time.perf_counter() - t0) * 1000
                        timings[mode].append(ms)
                        if index % 5 == 2:
                            centers[mode].append(ms)
                    if raw["baseline"] != raw["candidate"]:
                        report["raw_mvt_mismatch_count"] += 1
                        if len(report["mismatch_examples"]) < 12:
                            report["mismatch_examples"].append({
                                "xyz": [z, x, y], "trial": trial + 1,
                                "baseline_sha256": hashlib.sha256(raw["baseline"]).hexdigest(),
                                "candidate_sha256": hashlib.sha256(raw["candidate"]).hexdigest()
                            })
                report["trials"].append({
                    "trial": trial + 1,
                    "baseline_total_ms": round(sum(timings["baseline"]), 3),
                    "candidate_total_ms": round(sum(timings["candidate"]), 3),
                    "baseline_center_ms": round(sum(centers["baseline"]), 3),
                    "candidate_center_ms": round(sum(centers["candidate"]), 3)
                })
                write_json(out, report)
                print("BERLIN_SHARED_SAMPLE", json.dumps(report["trials"][-1]), flush=True)
            for z in (12, 14, 16):
                xyz = samples[(z - LO) * 5 + 2]
                for mode, q in (("baseline", base[z]), ("candidate", alt[z])):
                    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + q,
                                (xyz[0], xyz[1], xyz[2], xyz[0], xyz[1], xyz[2]))
                    raw = cur.fetchone()[0]
                    plan = raw[0] if isinstance(raw, list) else json.loads(raw)[0]
                    found = Counter()
                    def walk(node):
                        found[node.get("Node Type", "unknown")] += 1
                        for child in node.get("Plans", []):
                            walk(child)
                    walk(plan["Plan"])
                    report["explain"].append({
                        "mode": mode, "xyz": list(xyz),
                        "planning_ms": round(plan["Planning Time"], 3),
                        "execution_ms": round(plan["Execution Time"], 3),
                        "ctes": found["CTE Scan"],
                        "bitmap_heap_scans": found["Bitmap Heap Scan"],
                        "index_scans": found["Index Scan"]
                    })
            a = statistics.median(t["baseline_total_ms"] for t in report["trials"])
            b = statistics.median(t["candidate_total_ms"] for t in report["trials"])
            report["median_sample_ms"] = {"baseline": round(a, 3),
                                         "candidate": round(b, 3)}
            report["sample_speedup_pct"] = round(100 * (1 - b / a), 3)
            # Do not fund a six-export, multi-minute run if the 25-sample
            # result is slower or any raw MVT byte changes.
            promising = report["raw_mvt_mismatch_count"] == 0 and b <= a * 0.95
            report["status"] = "PROMISING_FOR_FULL_AB" if promising \
                else "STOP_SAMPLE_GATE"
            write_json(out, report)
    print("BERLIN_SHARED_SAMPLE_GATE", json.dumps({
        "status": report["status"], "sample_speedup_pct": report["sample_speedup_pct"],
        "mismatch_count": report["raw_mvt_mismatch_count"],
        "explain": report["explain"]}), flush=True)


def compare_archives(left: Path, right: Path):
    with sqlite3.connect(left) as a, sqlite3.connect(right) as b:
        if a.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or \
                b.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite integrity_check failure")
        ma = dict(a.execute("SELECT name,value FROM metadata"))
        mb = dict(b.execute("SELECT name,value FROM metadata"))
        ma.pop("name", None)
        mb.pop("name", None)
        if ma != mb:
            raise RuntimeError("MBTiles metadata parity failure (excluding name)")
        sql = ("SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles "
               "ORDER BY zoom_level,tile_column,tile_row")
        count, mismatches, samples = 0, 0, []
        for aa, bb in zip_longest(a.execute(sql), b.execute(sql)):
            count += 1
            if aa is None or bb is None or aa[:3] != bb[:3] or aa[3] != bb[3]:
                mismatches += 1
                if len(samples) < 10:
                    samples.append({"baseline": aa[:3] if aa else None,
                                    "candidate": bb[:3] if bb else None})
        return {"rows": count, "gzip_payload_mismatches": mismatches,
                "examples": samples, "metadata_equal": True}


def full(pbf: Path, out: Path, sample_report: Path):
    require_disposable(pbf)
    gate = json.loads(sample_report.read_text())
    if gate.get("status") != "PROMISING_FOR_FULL_AB":
        raise RuntimeError("Sample gate did not authorize full A/B")
    if os.environ.get("OSM2MBTILES_VECTOR_QUERY_MODE") != "projected":
        raise RuntimeError("Requires frozen projected MVT query mode")
    if os.name != "posix":
        raise RuntimeError("Forked workers required for benchmark-only SQL override")
    report = {"status": "RUNNING", "pbf_sha256": PBF_SHA,
              "bbox": BBOX, "zooms": [LO, HI], "tile_workers": WORKERS,
              "same_db": True, "paired_trials": [], "adopt_production": False,
              "sample_report": str(sample_report),
              "limitations": [
                  "Warm same-DB full export only, not a fresh PostgreSQL import.",
                  "Production code, SQL files, incremental exporter and main unchanged."
              ]}
    write_json(out, report)
    production_query = vector.query_for_zoom
    try:
        for trial in range(1, REPEATS + 1):
            timings = {}
            paths = {}
            order = ("baseline", "candidate") if trial % 2 else \
                ("candidate", "baseline")
            for mode in order:
                target = out.parent / f"berlin-{mode}-trial{trial}.mbtiles"
                if target.exists() or target.with_name(target.name + ".partial").exists():
                    raise RuntimeError("Preexisting archive, refusing overwrite")
                vector.query_for_zoom = (
                    production_query if mode == "baseline" else query_shared_candidates
                )
                t0 = time.perf_counter()
                written = vector.export(pbf, target, BBOX, LO, HI, WORKERS)
                timings[mode] = round(time.perf_counter() - t0, 3)
                paths[mode] = target
                if written != NONEMPTY_XYZ:
                    raise RuntimeError(f"Unexpected {mode} nonempty count: {written}")
            parity = compare_archives(paths["baseline"], paths["candidate"])
            item = {"trial": trial, "run_order": list(order),
                    "wall_seconds": timings, "parity": parity}
            report["paired_trials"].append(item)
            write_json(out, report)
            print("BERLIN_SHARED_FULL_PAIR", json.dumps(item), flush=True)
            if parity["rows"] != NONEMPTY_XYZ or parity["gzip_payload_mismatches"]:
                raise RuntimeError("Entire Berlin gzip/XYZ bytes do not match")
            for f in paths.values():
                f.unlink()
        a = statistics.median(p["wall_seconds"]["baseline"] for p in report["paired_trials"])
        b = statistics.median(p["wall_seconds"]["candidate"] for p in report["paired_trials"])
        report["medians_s"] = {"baseline": round(a, 3), "candidate": round(b, 3)}
        report["warm_export_speedup_pct"] = round(100 * (1 - b / a), 3)
        report["status"] = ("PASS_EXPLORATORY_GAIN" if b <= a * 0.95
                            else "PASS_BYTE_PARITY_NO_MEANINGFUL_GAIN")
        write_json(out, report)
        print("BERLIN_SHARED_FULL_DONE", json.dumps({
            "status": report["status"], "medians_s": report["medians_s"],
            "warm_export_speedup_pct": report["warm_export_speedup_pct"]
        }), flush=True)
    finally:
        vector.query_for_zoom = production_query


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("sample", "full"), required=True)
    parser.add_argument("--pbf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-report", type=Path)
    args = parser.parse_args()
    if args.phase == "sample":
        sample(args.pbf, args.output)
    else:
        if args.sample_report is None:
            parser.error("--sample-report is required for full")
        full(args.pbf, args.output, args.sample_report)


if __name__ == "__main__":
    main()
