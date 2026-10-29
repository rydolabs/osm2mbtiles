"""Experimental region-batched PostGIS MVT exporter (same profile as vector.py).

Fetch candidate source features once per batch via MATERIALIZED SQL CTEs,
then fan them out to the relevant tiles, applying the same ST_AsMVTGeom and
ST_AsMVT rules as the baseline.  Baseline remains untouched.  This is not
an external geometry encoder or a production incremental-update engine.
"""
from __future__ import annotations

import gzip
import multiprocessing
import re
import os
import sqlite3
import sys
import time
from pathlib import Path

from .tiles import tile_count, xyz_range
from .vector import (
    BUFFER, EXTENT, _properties, create_db, layer_specs, metadata,
)

_conn = None
_queries = {}

# Write batches are independent of 2x2 SQL microbatches and SQLite transactions.
# The previous writer committed after 128 nonempty tiles.
SQLITE_INSERT_BATCH = 256
SQLITE_COMMIT_BATCH = 1024


def batch_query(z: int, count: int, *, projected: bool = True) -> str:
    """One SQL statement returns all XYZ tiles in a rectangular microbatch.

    Inputs: x,y pairs (2 * count bind parameters). CTE materialization is
    intentional: one region-index scan per layer, not one scan per tile.
    """
    if not 1 <= count <= 64:
        raise ValueError("tile microbatch must have 1..64 tiles")
    values = ", ".join(["(%s::integer, %s::integer)"] * count)
    ctes = [
        f"""b AS MATERIALIZED (
              SELECT v.x, v.y,
                     ST_TileEnvelope({z}, v.x, v.y) AS tile,
                     ST_TileEnvelope({z}, v.x, v.y,
                       margin => {BUFFER}.0 / {EXTENT}) AS search
              FROM (VALUES {values}) AS v(x,y)
            )""",
        "region AS MATERIALIZED (SELECT ST_Envelope(ST_Collect(search)) AS geom FROM b)",
    ]
    grouped = {}
    for name, table, key, predicate in layer_specs(z):
        props = _properties("t", z, name, key, projected=projected)
        src = (f"SELECT t.geom, {props} AS properties FROM {table} t "
               f"CROSS JOIN region r WHERE t.geom && r.geom AND ({predicate})")
        grouped.setdefault(name, []).append(src)
    parts = []
    for idx, (name, branches) in enumerate(grouped.items()):
        base = f"l{idx}"
        ctes.append(f"{base}_src AS MATERIALIZED (" + " UNION ALL ".join(branches) + ")")
        ctes.append(
            f"""{base}_feat AS MATERIALIZED (
               SELECT b.x, b.y,
                      ST_AsMVTGeom(s.geom, b.tile, {EXTENT}, {BUFFER}, true) AS geom,
                      s.properties
               FROM {base}_src s JOIN b ON s.geom && b.search
            )"""
        )
        # Only geom and properties enter ST_AsMVT, never batch x/y columns.
        parts.append(
            f"""COALESCE((
               SELECT ST_AsMVT(q, '{name}', {EXTENT}, 'geom')
               FROM (SELECT f.geom, f.properties FROM {base}_feat f
                     WHERE f.x=b.x AND f.y=b.y AND f.geom IS NOT NULL) q
            ), ''::bytea)"""
        )
    payload = " || ".join(parts) if parts else "''::bytea"
    return "WITH " + ",\n".join(ctes) + f"\nSELECT b.x, b.y, {payload} AS pbf FROM b ORDER BY b.y,b.x"


def microbatches(bbox, lo: int, hi: int, side: int = 2):
    if not 1 <= side <= 8:
        raise ValueError("batch side must be 1..8")
    for z in range(lo, hi + 1):
        xmin, xmax, ymin, ymax = xyz_range(bbox, z)
        for y0 in range(ymin, ymax + 1, side):
            for x0 in range(xmin, xmax + 1, side):
                cells = [(x, y)
                         for y in range(y0, min(y0 + side, ymax + 1))
                         for x in range(x0, min(x0 + side, xmax + 1))]
                yield z, cells


def _init_worker():
    global _conn, _queries
    import psycopg2
    _conn = psycopg2.connect(dbname=os.environ.get("PGDATABASE", "gis"))
    _conn.autocommit = True
    _queries = {}


def _prepare_batch(cur, z: int, count: int) -> tuple[str, str]:
    """PREPARE once per worker connection/zoom/batch shape.

    Named statements live on the worker's PostgreSQL connection. Parameters
    stay bound at EXECUTE time; never interpolate tile coordinates into SQL.
    """
    key = (z, count)
    cached = _queries.get(key)
    if cached is not None:
        return cached
    sql = batch_query(z, count)
    placeholders = iter(range(1, count * 2 + 1))
    prepared = re.sub(r"%s::integer",
                      lambda _: f"${next(placeholders)}::integer", sql)
    name = f"mvt_z{z}_n{count}"
    cur.execute(
        f"PREPARE {name} ({', '.join(['integer'] * (count * 2))}) AS {prepared}")
    execute = f"EXECUTE {name} ({', '.join(['%s'] * (count * 2))})"
    _queries[key] = (name, execute)
    return _queries[key]


def _gzip_batch_rows(z, rows):
    """Compress MVT in independent SQL workers; preserve byte-for-byte gzip output."""
    return [(z, int(x), int(y),
             gzip.compress(bytes(data), compresslevel=6, mtime=0) if data else b"")
            for x, y, data in rows]


def _render_batch(job):
    z, cells = job
    args = [value for pair in cells for value in pair]
    with _conn.cursor() as cur:
        _, execute = _prepare_batch(cur, z, len(cells))
        cur.execute(execute, args)
        rows = cur.fetchall()
    if {(x, y) for x, y, _ in rows} != set(cells):
        raise RuntimeError("batch SQL returned missing or duplicate tile coordinates")
    return _gzip_batch_rows(z, rows)


def export_batch(pbf, out: Path, bbox, lo: int, hi: int,
                 processes: int, side: int = 2):
    """Export batch MVT with one SQLite writer and atomic file replacement."""
    if not 1 <= processes <= 64:
        raise ValueError("workers must be 1..64")
    partial = out.with_name(out.name + ".partial")
    partial.unlink(missing_ok=True)
    total = tile_count(bbox, lo, hi)
    seen = written = pending = commits = 0
    insert_seconds = commit_seconds = worker_wait_seconds = 0.0
    insert_calls = 0
    rows_to_insert = []
    insert_sql = "INSERT INTO tiles VALUES (?,?,?,?)"
    start = time.monotonic()
    ctx = multiprocessing.get_context("spawn" if sys.platform == "win32" else "fork")
    with ctx.Pool(processes=processes, initializer=_init_worker) as pool:
        with sqlite3.connect(partial) as conn:
            create_db(conn, metadata(out.stem, bbox, lo, hi))
            # One writer owns SQLite. The workers only return gzip-compressed MVT.
            jobs = pool.imap_unordered(
                _render_batch, microbatches(bbox, lo, hi, side), chunksize=1)
            last = time.monotonic()
            for batch in jobs:
                worker_wait_seconds += time.monotonic() - last
                for z, x, y, data in batch:
                    seen += 1
                    if data:
                        rows_to_insert.append(
                            (z, x, (1 << z) - 1 - y, sqlite3.Binary(data)))
                        written += 1
                if len(rows_to_insert) >= SQLITE_INSERT_BATCH:
                    stamp = time.monotonic()
                    conn.executemany(insert_sql, rows_to_insert)
                    insert_seconds += time.monotonic() - stamp
                    insert_calls += 1
                    pending += len(rows_to_insert)
                    rows_to_insert.clear()
                if pending >= SQLITE_COMMIT_BATCH:
                    stamp = time.monotonic()
                    conn.commit()
                    commit_seconds += time.monotonic() - stamp
                    commits += 1
                    pending = 0
                last = time.monotonic()
            if seen != total:
                raise RuntimeError(f"batch export incomplete: {seen}/{total}")
            if rows_to_insert:
                stamp = time.monotonic()
                conn.executemany(insert_sql, rows_to_insert)
                insert_seconds += time.monotonic() - stamp
                insert_calls += 1
                pending += len(rows_to_insert)
            stamp = time.monotonic()
            conn.commit()
            commit_seconds += time.monotonic() - stamp
            commits += 1
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError("batch MBTiles integrity_check: " + result)
    print(f"[BATCH WRITER] insert_calls={insert_calls}, commits={commits}, "
          f"insert_seconds={insert_seconds:.3f}, commit_seconds={commit_seconds:.3f}, "
          f"worker_wait_seconds={worker_wait_seconds:.3f}", flush=True)
    os.replace(partial, out)
    print(f"[BATCH] {written}/{total} nonempty tiles, "
          f"{time.monotonic()-start:.2f}s, {out.stat().st_size} bytes", flush=True)
    return written
