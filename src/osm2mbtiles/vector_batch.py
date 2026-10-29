"""Experimental region-batched PostGIS MVT exporter (same profile as vector.py).

Fetch candidate source features once per batch via MATERIALIZED SQL CTEs,
then fan them out to the relevant tiles, applying the same ST_AsMVTGeom and
ST_AsMVT rules as the baseline.  Baseline remains untouched.  This is not
an external geometry encoder or a production incremental-update engine.
"""
from __future__ import annotations

import gzip
import multiprocessing
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


def _render_batch(job):
    z, cells = job
    sql = _queries.setdefault((z, len(cells)), batch_query(z, len(cells)))
    args = [value for pair in cells for value in pair]
    with _conn.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    if {(x, y) for x, y, _ in rows} != set(cells):
        raise RuntimeError("batch SQL returned missing or duplicate tile coordinates")
    return [(z, int(x), int(y), bytes(data or b"")) for x, y, data in rows]


def export_batch(pbf, out: Path, bbox, lo: int, hi: int,
                 processes: int, side: int = 2):
    """Export batch MVT with one SQLite writer and atomic file replacement."""
    if not 1 <= processes <= 64:
        raise ValueError("workers must be 1..64")
    partial = out.with_name(out.name + ".partial")
    partial.unlink(missing_ok=True)
    total = tile_count(bbox, lo, hi)
    seen = written = pending = 0
    start = time.monotonic()
    ctx = multiprocessing.get_context("spawn" if sys.platform == "win32" else "fork")
    with ctx.Pool(processes=processes, initializer=_init_worker) as pool:
        with sqlite3.connect(partial) as conn:
            create_db(conn, metadata(out.stem, bbox, lo, hi))
            for batch in pool.imap_unordered(
                    _render_batch, microbatches(bbox, lo, hi, side), chunksize=1):
                for z, x, y, data in batch:
                    seen += 1
                    if data:
                        conn.execute(
                            "INSERT INTO tiles VALUES (?,?,?,?)",
                            (z, x, (1 << z)-1-y,
                             sqlite3.Binary(gzip.compress(data, compresslevel=6, mtime=0))))
                        written += 1
                        pending += 1
                if pending >= 128:
                    conn.commit()
                    pending = 0
            if seen != total:
                raise RuntimeError(f"batch export incomplete: {seen}/{total}")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError("batch MBTiles integrity_check: " + result)
    os.replace(partial, out)
    print(f"[BATCH] {written}/{total} nonempty tiles, "
          f"{time.monotonic()-start:.2f}s, {out.stat().st_size} bytes", flush=True)
    return written
