#!/usr/bin/env python3
"""Strict paired Berlin exporter A/B: only multiprocessing imap chunksize changes.

For each mode use the *unchanged* production osm2mbtiles.vector.export,
original SQL (projected), gzip settings, SQLite writer and tile task stream.
A temporary in-process monkeypatch changes only pool task dispatch
chunksize=1 -> 4 when the pool consumes vector.render_one.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
from itertools import zip_longest
import json
import multiprocessing.pool
import os
from pathlib import Path
import sqlite3
import statistics
import time

from osm2mbtiles import vector

BBOX = (13.088, 52.338, 13.761, 52.676)
LO, HI = 12, 16
WORKERS = 4
TRIALS = 3
CHUNK_SIZE = 4
EXPECTED_PBF_SHA = "51d3845df2e4b1334ebec7cf6516e41e71735ce2fc1c0975c52d57f58f1952ad"
EXPECTED_PBF_BYTES = 99604748
EXPECTED_XYZ = 16943
EXPECTED_NONEMPTY = 12047
EXPECTED_HOST = "/tmp/osm2mbtiles-chunk-berlin/socket"


def require_disposable(pbf: Path):
    if (os.environ.get("PGHOST") != EXPECTED_HOST
            or os.environ.get("PGUSER") != "chunkprobe"
            or os.environ.get("PGDATABASE") != "gis"):
        raise RuntimeError("Refusing to run against non-disposable PostGIS")
    if os.environ.get("OSM2MBTILES_VECTOR_QUERY_MODE") != "projected":
        raise RuntimeError("Requires frozen projected query mode")
    if os.name != "posix":
        raise RuntimeError("This benchmark requires Linux fork-mode workers")
    if vector.tile_count(BBOX, LO, HI) != EXPECTED_XYZ:
        raise RuntimeError("Unexpected Berlin tile grid")
    sha = hashlib.sha256()
    with pbf.open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            sha.update(block)
    if sha.hexdigest() != EXPECTED_PBF_SHA or pbf.stat().st_size != EXPECTED_PBF_BYTES:
        raise RuntimeError("Berlin PBF differs from frozen reference")


@contextmanager
def override_chunk_size():
    """Intercept the exact legacy render loop; fail if it stops being used."""
    original = multiprocessing.pool.Pool.imap_unordered
    intercepted = []
    def only_legacy_render(pool, fn, tasks, chunksize=1):
        if fn is vector.render_one:
            if chunksize != 1:
                raise RuntimeError("Legacy exporter chunksize changed")
            intercepted.append(True)
            return original(pool, fn, tasks, chunksize=CHUNK_SIZE)
        return original(pool, fn, tasks, chunksize=chunksize)
    multiprocessing.pool.Pool.imap_unordered = only_legacy_render
    try:
        yield intercepted
    finally:
        multiprocessing.pool.Pool.imap_unordered = original
        if len(intercepted) != 1:
            raise RuntimeError(f"Expected exactly 1 dispatch override, got {len(intercepted)}")


def compare_archives(a_path: Path, b_path: Path):
    with sqlite3.connect(a_path) as a, sqlite3.connect(b_path) as b:
        if (a.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or b.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
            raise RuntimeError("SQLite integrity check failed")
        am = dict(a.execute("SELECT name, value FROM metadata"))
        bm = dict(b.execute("SELECT name, value FROM metadata"))
        am.pop("name", None)
        bm.pop("name", None)
        if am != bm:
            raise RuntimeError("Metadata differs beyond output filename")
        query = ("SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles "
                 "ORDER BY zoom_level,tile_column,tile_row")
        rows, mismatches, examples = 0, 0, []
        compressed_a = compressed_b = 0
        for aa, bb in zip_longest(a.execute(query), b.execute(query)):
            rows += 1
            if aa is not None:
                compressed_a += len(aa[3])
            if bb is not None:
                compressed_b += len(bb[3])
            if aa is None or bb is None or aa[:3] != bb[:3] or aa[3] != bb[3]:
                mismatches += 1
                if len(examples) < 10:
                    examples.append([aa[:3] if aa else None,
                                     bb[:3] if bb else None])
        return {"rows": rows, "gzip_payload_mismatches": mismatches,
                "gzip_bytes_baseline": compressed_a,
                "gzip_bytes_candidate": compressed_b,
                "metadata_equal_except_name": True, "examples": examples}


def write_report(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pbf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require_disposable(args.pbf)
    report = {"status": "RUNNING", "dataset": "berlin-stress",
              "pbf_sha256": EXPECTED_PBF_SHA, "bbox": BBOX, "zooms": [LO, HI],
              "requested_xyz": EXPECTED_XYZ, "expected_nonempty_xyz": EXPECTED_NONEMPTY,
              "query_mode": "projected", "tile_workers": WORKERS,
              "baseline_chunksize": 1, "candidate_chunksize": CHUNK_SIZE,
              "trials": TRIALS, "same_postgis_db": True,
              "pairs": [], "adopt_production": False,
              "limitations": [
                  "Warm tile export only; no fresh-DB or PBF import duration included.",
                  "Runner time is not comparable to other workflows' historic durations.",
                  "Only a benchmark-scoped task-dispatch override; production unchanged."
              ]}
    write_report(args.output, report)
    try:
        for trial in range(1, TRIALS + 1):
            order = ("baseline", "candidate") if trial % 2 else \
                ("candidate", "baseline")
            timing = {}
            paths = {}
            for mode in order:
                archive = args.output.parent / f"berlin-{trial}-{mode}.mbtiles"
                partial = archive.with_name(archive.name + ".partial")
                if archive.exists() or partial.exists():
                    raise RuntimeError("Refusing to overwrite previous archive")
                t = time.perf_counter()
                if mode == "candidate":
                    with override_chunk_size():
                        written = vector.export(args.pbf, archive, BBOX, LO, HI, WORKERS)
                else:
                    written = vector.export(args.pbf, archive, BBOX, LO, HI, WORKERS)
                timing[mode] = round(time.perf_counter() - t, 3)
                if written != EXPECTED_NONEMPTY:
                    raise RuntimeError(f"Unexpected nonempty tiles {mode}: {written}")
                paths[mode] = archive
            parity = compare_archives(paths["baseline"], paths["candidate"])
            item = {"trial": trial, "order": list(order),
                    "wall_seconds": timing, "parity": parity}
            report["pairs"].append(item)
            write_report(args.output, report)
            print("BERLIN_CHUNKSIZE_PAIR", json.dumps(item), flush=True)
            if parity["rows"] != EXPECTED_NONEMPTY or parity["gzip_payload_mismatches"]:
                raise RuntimeError("Complete Berlin gzip/XYZ bytes differ: reject candidate")
            for archive in paths.values():
                archive.unlink()
        a = statistics.median(item["wall_seconds"]["baseline"] for item in report["pairs"])
        b = statistics.median(item["wall_seconds"]["candidate"] for item in report["pairs"])
        report["medians_s"] = {"baseline": round(a, 3), "candidate": round(b, 3)}
        report["speedup_pct"] = round(100 * (1 - b / a), 3)
        all_gain = all(item["wall_seconds"]["candidate"] <
                       item["wall_seconds"]["baseline"] for item in report["pairs"])
        report["all_pairs_faster"] = all_gain
        report["status"] = ("PROMISING_EXPLORATORY_GAIN" if b <= a * 0.95 and all_gain
                            else "NO_MEANINGFUL_GAIN")
        write_report(args.output, report)
        print("BERLIN_CHUNKSIZE_DONE", json.dumps({
            "status": report["status"], "medians_s": report["medians_s"],
            "speedup_pct": report["speedup_pct"],
            "all_pairs_faster": all_gain}), flush=True)
    except BaseException as exc:
        report["status"] = "FAIL"
        report["error"] = repr(exc)
        write_report(args.output, report)
        raise


if __name__ == "__main__":
    main()
