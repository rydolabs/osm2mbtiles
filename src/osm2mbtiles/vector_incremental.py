"""Persistent PostGIS vector-only OSM diff -> dirty MVT tile -> MBTiles pipeline.

This is intentionally separate from the old import-once (--slim --drop) path.
It assumes an isolated, application-owned PostGIS database and a single writer.
The created DB retains osm2pgsql middle tables so --append can resolve ways.
No raster/OSM Carto update is claimed.
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

from .tiles import xyz_range, validate_bbox
from .vector import export as full_export, query_for_zoom

EXPIRE_TABLE = "vector_expired_tiles"
STATE_TABLE = "osm2mbtiles_vector_sync"
LOCK_A, LOCK_B = 209104, 1129
GEOM_PATTERN = re.compile(
    r"(\{ column = 'geom', type = '[^']+', projection = 3857, not_null = true)( \},)"
)


def source_style():
    return Path(__file__).resolve().parents[2] / "vector" / "vector-flex.lua"


def post_import_sql():
    return source_style().with_name("post-import.sql")


def style_digest():
    h = hashlib.sha256()
    for path in (source_style(), post_import_sql(), Path(__file__),
                 Path(__file__).with_name('vector.py')):
        h.update(path.read_bytes())
    return h.hexdigest()


def configured_style(lo: int, hi: int):
    """Preserve every existing flex callback; only attach expire to four geoms."""
    if not 0 <= lo <= hi <= 22:
        raise ValueError("zoom range must be 0..22")
    base = source_style().read_text(encoding="utf-8")
    declaration = ("local vector_expire = osm2pgsql.define_expire_output("
                   "{ table = '" + EXPIRE_TABLE + "', minzoom = " + str(lo) +
                   ", maxzoom = " + str(hi) + " })\n")
    changed, count = GEOM_PATTERN.subn(
        r"\1, expire = { { output = vector_expire, buffer = 64.0 / 4096 } }\2", base)
    if count != 4:
        raise ValueError("Unknown vector flex schema: require exactly four geometry columns")
    return declaration + changed


def _run(cmd):
    subprocess.run([str(x) for x in cmd], check=True)


def _connect():
    import psycopg2
    return psycopg2.connect(dbname=os.environ.get("PGDATABASE", "gis"))


@contextlib.contextmanager
def _exclusive(db):
    with db.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s,%s)", (LOCK_A, LOCK_B))
        if not cur.fetchone()[0]:
            raise RuntimeError("another vector incremental operation owns this PostGIS DB")
    try:
        yield
    finally:
        # Unlock even if an SQL statement has failed and the transaction aborted.
        db.rollback()
        with db.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s,%s)", (LOCK_A, LOCK_B))
        db.commit()


def _load(db):
    with db.cursor() as cur:
        cur.execute("SELECT to_regclass('public." + STATE_TABLE + "')")
        if cur.fetchone()[0] is None:
            raise RuntimeError("DB is not initialized for vector incremental updates")
        cur.execute("SELECT bbox, lo, hi, profile, phase, diff_sha FROM " + STATE_TABLE + " WHERE id=1")
        row = cur.fetchone()
    if not row:
        raise RuntimeError("Missing vector incremental state")
    return dict(zip(("bbox", "lo", "hi", "profile", "phase", "diff_sha"), row))


def _update_phase(db, phase, diff_sha=None):
    with db.cursor() as cur:
        cur.execute(
            "UPDATE " + STATE_TABLE + " SET phase=%s, diff_sha=%s WHERE id=1",
            (phase, diff_sha))
    db.commit()


def _scope(state, output: Path):
    if state["profile"] != style_digest():
        raise RuntimeError("Vector source/profile changed: full reimport is required")
    if not output.is_file():
        raise FileNotFoundError("Expected existing MBTiles archive: " + str(output))
    bbox = tuple(json.loads(state["bbox"]))
    return bbox, int(state["lo"]), int(state["hi"])


def init(pbf: Path, output: Path, bbox, lo: int, hi: int, processes: int = 2):
    """Fresh DB only. Retain slim middle; generate complete baseline MBTiles once."""
    if not pbf.is_file():
        raise FileNotFoundError(pbf)
    bbox = validate_bbox(tuple(bbox))
    output = output.resolve()
    if output.exists():
        raise RuntimeError("Refusing to replace existing MBTiles: " + str(output))
    with _connect() as db:
        with _exclusive(db):
            with db.cursor() as cur:
                cur.execute("SELECT to_regclass('public.vector_points')")
                if cur.fetchone()[0]:
                    raise RuntimeError("Refusing to overwrite existing vector tables")
                cur.execute("SELECT to_regclass('public." + STATE_TABLE + "')")
                if cur.fetchone()[0]:
                    raise RuntimeError("Existing incremental state found")
                cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
                cur.execute("CREATE EXTENSION IF NOT EXISTS hstore")
            db.commit()
            # Save the exact style for append; unlike --drop, slim middle is retained.
            style = output.with_name(output.name + ".update-flex.lua")
            if style.exists():
                raise RuntimeError("Refusing to overwrite existing flex style")
            style.write_text(configured_style(lo, hi), encoding="utf-8")
            _run(["osm2pgsql", "--slim", "--cache=256",
                  "--number-processes=1", "--disable-parallel-indexing",
                  "-O", "flex", "-S", style, "-d",
                  os.environ.get("PGDATABASE", "gis"), pbf])
            _run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-f", post_import_sql()])
            # Existing exporter writes to a partial archive then atomically replaces.
            full_export(pbf, output, bbox, lo, hi, processes)
            with db.cursor() as cur:
                cur.execute(
                    "CREATE TABLE " + STATE_TABLE + " ("
                    "id integer PRIMARY KEY CHECK(id=1), bbox text NOT NULL, "
                    "lo integer NOT NULL, hi integer NOT NULL, "
                    "profile text NOT NULL, phase text NOT NULL, diff_sha text)")
                cur.execute(
                    "INSERT INTO " + STATE_TABLE +
                    " (id,bbox,lo,hi,profile,phase) VALUES (1,%s,%s,%s,%s,'idle')",
                    (json.dumps(bbox), lo, hi, style_digest()))
            db.commit()
    print("VECTOR_INCREMENTAL_INIT_OK", output, flush=True)


def append(diff: Path, output: Path, *, flush: bool = True):
    """Apply one OSM diff at most once. On ambiguous failures stop, never replay."""
    if not diff.is_file():
        raise FileNotFoundError(diff)
    checksum = hashlib.sha256()
    with diff.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            checksum.update(block)
    sha = checksum.hexdigest()
    with _connect() as db:
        with _exclusive(db):
            state = _load(db)
            _scope(state, output)
            if state["phase"] != "idle":
                raise RuntimeError("Pending or uncertain update; use flush or operator recovery")
            if state["diff_sha"] == sha:
                raise RuntimeError("Refusing to apply the same diff twice")
            with db.cursor() as cur:
                cur.execute("SELECT count(*) FROM " + EXPIRE_TABLE)
                if cur.fetchone()[0]:
                    raise RuntimeError("Unflushed expire table; use flush before next append")
            style = output.with_name(output.name + ".update-flex.lua")
            if not style.is_file() or style.read_text() != configured_style(state["lo"], state["hi"]):
                raise RuntimeError("Missing or changed configured update style; no diff was applied")
            # Durable marker written BEFORE invoking an external importer.
            _update_phase(db, "applying", sha)
            try:
                _run(["osm2pgsql", "--append", "--slim", "--cache=256",
                      "--number-processes=1", "-O", "flex", "-S", style,
                      "-d", os.environ.get("PGDATABASE", "gis"), diff])
                # Derived polygon POI/housenumber labels must reflect current areas.
                # Full refresh is correct but deliberately not yet fast on huge DBs.
                _run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-f", post_import_sql()])
            except BaseException:
                # Applying may have partially committed. Automatic retry is unsafe.
                raise
            _update_phase(db, "pending", sha)
    if flush:
        return flush_tiles(output)
    return {"applied": True, "flush_pending": True}


def flush_tiles(output: Path):
    """Stream PostGIS expire records; one atomic SQLite transaction, then clear.

    A crash after the SQLite commit but before queue deletion is idempotently
    recoverable. Tile buffers and dirty rows never accumulate in Python RAM.
    """
    output = output.resolve()
    with _connect() as db:
        with _exclusive(db):
            state = _load(db)
            bbox, lo, hi = _scope(state, output)
            if state["phase"] not in ("pending", "idle"):
                raise RuntimeError("OSM diff application uncertain; refusing partial repair")
            changed = deleted = total = selected = 0
            # Snapshot the resulting OSM/derived tables while building one
            # archive transaction. The source must not change during this flush.
            with _connect() as reader:
                reader.set_session(isolation_level="REPEATABLE READ", readonly=True)
                sql_by_zoom = {}
                with sqlite3.connect(output) as archive:
                    if archive.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("Existing MBTiles failed SQLite integrity_check")
                    meta = dict(archive.execute("SELECT name,value FROM metadata"))
                    if meta.get("format") != "pbf":
                        raise RuntimeError("Not a vector MBTiles file")
                    archive.execute("PRAGMA synchronous=FULL")
                    archive.execute("BEGIN IMMEDIATE")
                    with db.cursor(name="vector_dirty_cursor") as expired:
                        expired.itersize = 256
                        expired.execute(
                            "SELECT zoom,x,y FROM " + EXPIRE_TABLE + " ORDER BY zoom,y,x")
                        with reader.cursor() as query:
                            for z, x, y in expired:
                                total += 1
                                if not lo <= z <= hi:
                                    continue
                                xmin, xmax, ymin, ymax = xyz_range(bbox, z)
                                if not (xmin <= x <= xmax and ymin <= y <= ymax):
                                    continue
                                selected += 1
                                if z < 3:
                                    data = b""
                                else:
                                    sql = sql_by_zoom.setdefault(z, query_for_zoom(z))
                                    query.execute(sql, (z, x, y, z, x, y))
                                    data = bytes(query.fetchone()[0] or b"")
                                tms_y = (1 << z) - 1 - y
                                if data:
                                    archive.execute(
                                        "INSERT INTO tiles VALUES(?,?,?,?) "
                                        "ON CONFLICT(zoom_level,tile_column,tile_row) "
                                        "DO UPDATE SET tile_data=excluded.tile_data",
                                        (z, x, tms_y, sqlite3.Binary(
                                            gzip.compress(data, compresslevel=6, mtime=0))))
                                    changed += 1
                                else:
                                    archive.execute(
                                        "DELETE FROM tiles WHERE zoom_level=? "
                                        "AND tile_column=? AND tile_row=?",
                                        (z, x, tms_y))
                                    deleted += 1
            # If this fails, the complete list survives for another flush.
            with db.cursor() as cur:
                cur.execute("DELETE FROM " + EXPIRE_TABLE)
                cur.execute("UPDATE " + STATE_TABLE + " SET phase='idle' WHERE id=1")
            db.commit()
            result = {"dirty_total": total, "dirty_in_archive": selected,
                      "written": changed, "deleted_or_empty": deleted, "remaining": 0}
            print("VECTOR_INCREMENTAL_FLUSH_OK", json.dumps(result), flush=True)
            return result


def main(argv=None):
    p = argparse.ArgumentParser(prog="osm2mbtiles.vector_incremental")
    sub = p.add_subparsers(dest="action", required=True)
    create = sub.add_parser("init", help="initial import, retaining slim middle data")
    create.add_argument("--pbf", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--bbox", required=True, help="west,south,east,north")
    create.add_argument("--zoom", default="12-15")
    create.add_argument("--processes", type=int, default=2)
    apply = sub.add_parser("append", help="apply one .osc diff and optionally flush")
    apply.add_argument("--diff", required=True, type=Path)
    apply.add_argument("--output", required=True, type=Path)
    apply.add_argument("--no-flush", action="store_true")
    drain = sub.add_parser("flush", help="retry pending tile writes (no diff reapply)")
    drain.add_argument("--output", required=True, type=Path)
    args = p.parse_args(argv)
    if args.action == "init":
        lo, hi = map(int, args.zoom.split("-", 1))
        return init(args.pbf, args.output,
                    tuple(map(float, args.bbox.split(","))), lo, hi, args.processes)
    if args.action == "append":
        return append(args.diff, args.output, flush=not args.no_flush)
    return flush_tiles(args.output)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print("VECTOR_INCREMENTAL_ERROR", str(exc), file=sys.stderr)
        sys.exit(2)
