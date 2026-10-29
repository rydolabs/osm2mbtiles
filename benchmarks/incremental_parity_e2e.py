#!/usr/bin/env python3
"""Real osm2pgsql --append parity proof: changes vs independent full rebuild.

Creates only synthetic, locally generated OSM XML/.osc fixtures. Runs against
two disposable databases initialized by the GitHub Actions workflow; it never
connects to a user's persistent cluster.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from xml.sax.saxutils import escape

import psycopg2

from osm2mbtiles import vector_incremental as inc
from osm2mbtiles.vector import export as full_export
from postgis_batch_parity import compare_strict, content_signature

BBOX = (126.97, 37.48, 127.03, 37.53)
LO, HI = 12, 15


def node(oid, lon, lat, tags=None, version=1):
    tags = tags or {}
    attrs = (f'id="{oid}" lon="{lon}" lat="{lat}" version="{version}" '
             f'timestamp="2026-10-28T00:00:00Z" changeset="{version}"')
    if not tags:
        return f"<node {attrs}/>"
    return "<node " + attrs + ">" + "".join(
        f'<tag k="{escape(k)}" v="{escape(v)}"/>' for k, v in tags.items()
    ) + "</node>"


def way(oid, refs, tags=None, version=1):
    return (f'<way id="{oid}" version="{version}" '
            f'timestamp="2026-10-28T00:00:00Z" changeset="{version}">' +
            "".join(f'<nd ref="{x}"/>' for x in refs) +
            "".join(f'<tag k="{escape(k)}" v="{escape(v)}"/>'
                    for k, v in (tags or {}).items()) + "</way>")


def fixtures(directory: Path):
    a = [
        node(100, 126.986, 37.494, {"amenity": "school", "name": "Old School"}),
        node(101, 126.993, 37.505, {"shop": "bakery", "name": "Removed Shop"}),
        node(102, 127.0, 37.50, {"place": "neighbourhood", "name": "Stable Place"}),
        node(103, 127.005, 37.501, {"amenity": "restaurant", "name": "Old Name"}),
        node(210, 126.981, 37.489), node(211, 126.984, 37.489),
        node(212, 126.984, 37.491), node(213, 126.981, 37.491),
        node(310, 126.995, 37.497), node(311, 127.000, 37.498),
        node(312, 127.007, 37.500),
        node(320, 127.02, 37.515), node(321, 127.025, 37.520),
    ]
    w = [
        way(3000, [210, 211, 212, 213, 210],
            {"building": "yes", "name": "Old Building", "shop": "books"}),
        way(4000, [310, 311, 312], {"highway": "residential", "name": "Removed Road"}),
        way(4001, [320, 321], {"highway": "secondary", "name": "Stable Road"}),
    ]
    b = [
        node(100, 127.015, 37.514, {"amenity": "school", "name": "Moved School"}, 2),
        node(102, 127.0, 37.50, {"place": "neighbourhood", "name": "Stable Place"}),
        node(103, 127.005, 37.501, {"amenity": "restaurant", "name": "New Name"}, 2),
        node(105, 127.008, 37.511, {"amenity": "pharmacy", "name": "Added Pharmacy"}),
        node(210, 126.979, 37.488, version=2),
        node(211, 126.984, 37.489), node(212, 126.984, 37.491),
        node(213, 126.981, 37.491),
        node(310, 126.995, 37.497), node(311, 127.000, 37.498),
        node(312, 127.007, 37.500),
        node(320, 127.02, 37.515), node(321, 127.025, 37.520),
    ]
    wb = [
        way(3000, [210, 211, 212, 213, 210],
            {"building": "retail", "name": "New Building", "shop": "books"}, 2),
        way(4001, [320, 321], {"highway": "secondary", "name": "Stable Road"}),
    ]
    initial = directory / "initial.osm"
    final = directory / "final.osm"
    diff = directory / "change.osc"
    for path, objects in ((initial, a+w), (final, b+wb)):
        path.write_text('<osm version="0.6" generator="osm2mbtiles-ci">\n' +
                        "\n".join(objects) + "\n</osm>\n")
    # Osmium requires GLOBAL type/id ordering even across action blocks:
    # every node before every way, monotonically increasing IDs.
    diff.write_text(
        '<osmChange version="0.6" generator="osm2mbtiles-ci">\n'
        '<modify>\n' +
        node(100, 127.015, 37.514, {"amenity": "school", "name": "Moved School"}, 2) +
        '\n</modify>\n<delete>\n' +
        node(101, 126.993, 37.505, version=2) +
        '\n</delete>\n<modify>\n' +
        node(103, 127.005, 37.501, {"amenity": "restaurant", "name": "New Name"}, 2) +
        '\n</modify>\n<create>\n' +
        node(105, 127.008, 37.511, {"amenity": "pharmacy", "name": "Added Pharmacy"}) +
        '\n</create>\n<modify>\n' +
        node(210, 126.979, 37.488, version=2) +
        '\n' + way(3000, [210, 211, 212, 213, 210],
                      {"building": "retail", "name": "New Building", "shop": "books"}, 2) +
        '\n</modify>\n<delete>\n' +
        way(4000, [310, 311, 312], version=2) +
        '\n</delete>\n</osmChange>\n'
    )
    compressed = directory / "change.osc.gz"
    compressed.write_bytes(gzip.compress(diff.read_bytes(), mtime=0))
    assert gzip.decompress(compressed.read_bytes()) == diff.read_bytes()
    return initial, compressed, final


def sql_scalar(statement):
    with psycopg2.connect() as db:
        with db.cursor() as cur:
            cur.execute(statement)
            return cur.fetchone()[0]


def compare_delta(old, new, dirty):
    changed = {
        tile for tile in old.keys() | new.keys()
        if old.get(tile) != new.get(tile)
    }
    missing_dirty = sorted(changed - dirty)
    return {"actual_changed_tiles": len(changed),
            "expired_tiles": len(dirty),
            "changed_not_expired": missing_dirty[:20],
            "dirty_covers_all_changes": not missing_dirty}


def run(work: Path):
    work.mkdir(parents=True, exist_ok=True)
    initial, diff, final = fixtures(work)
    inc_archive = work / "updated.mbtiles"
    ref_archive = work / "full.mbtiles"
    report = {
        "protocol": "real osm2pgsql flex --slim create + --append .osc.gz vs fresh full OSM import",
        "bbox": BBOX, "zooms": [LO, HI],
        "fixture_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (initial, diff, final)
        },
    }
    evidence = work / "incremental-results.json"
    def save():
        evidence.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    old_pg = os.environ.get("PGDATABASE", "gis")
    assert old_pg == "gis", "Must run against disposable gis DB"
    started = time.perf_counter()
    inc.init(initial, inc_archive, BBOX, LO, HI, processes=2)
    report["init_sec"] = round(time.perf_counter() - started, 3)
    before, _, report["initial_stats"] = content_signature(inc_archive)
    save()
    started = time.perf_counter()
    inc.append(diff, inc_archive, flush=False)
    report["append_sec"] = round(time.perf_counter() - started, 3)
    assert sql_scalar("SELECT phase FROM osm2mbtiles_vector_sync") == "pending"
    with psycopg2.connect() as db, db.cursor() as cur:
        cur.execute("SELECT zoom,x,y FROM vector_expired_tiles")
        expired = {(z,x,y) for z,x,y in cur.fetchall()}
    assert expired, "Expected dirty tiles for added/deleted/moved objects"
    # Simulate a crash/unavailable archive AFTER append, BEFORE flushing.
    # Must not acknowledge queue or try replaying the diff.
    parked = work / "archive-temporarily-missing"
    inc_archive.rename(parked)
    try:
        try:
            inc.flush_tiles(inc_archive)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("Flush must fail if MBTiles archive is missing")
        assert sql_scalar("SELECT count(*) FROM vector_expired_tiles") == len(expired)
        assert sql_scalar("SELECT phase FROM osm2mbtiles_vector_sync") == "pending"
    finally:
        parked.rename(inc_archive)
    started = time.perf_counter()
    report["flush"] = inc.flush_tiles(inc_archive)
    report["flush_sec"] = round(time.perf_counter() - started, 3)
    assert sql_scalar("SELECT count(*) FROM vector_expired_tiles") == 0
    assert sql_scalar("SELECT phase FROM osm2mbtiles_vector_sync") == "idle"
    report["delta"] = compare_delta(before, content_signature(inc_archive)[0], expired)
    assert report["delta"]["dirty_covers_all_changes"], report["delta"]
    assert report["delta"]["actual_changed_tiles"] > 0
    # The same OSC must never be applied twice.
    try:
        inc.append(diff, inc_archive, flush=False)
    except RuntimeError as exc:
        assert "same diff twice" in str(exc)
    else:
        raise AssertionError("duplicate OSM diff was accepted")
    # Repeated flush must not change any decoded tile contents.
    once, _, _ = content_signature(inc_archive)
    replay = inc.flush_tiles(inc_archive)
    twice, _, _ = content_signature(inc_archive)
    assert once == twice and replay["dirty_total"] == 0
    save()
    # Independent full rebuild on fresh reference database.
    subprocess.run(["createdb", "refgis"], check=True)
    subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", "refgis",
                    "-c", "CREATE EXTENSION postgis; CREATE EXTENSION hstore;"], check=True)
    subprocess.run(["osm2pgsql", "--slim", "--drop", "--cache=256",
                    "--number-processes=1", "-O", "flex", "-S",
                    inc.source_style(), "-d", "refgis", final], check=True)
    subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", "refgis",
                    "-f", inc.post_import_sql()], check=True)
    os.environ["PGDATABASE"] = "refgis"
    try:
        started = time.perf_counter()
        full_export(final, ref_archive, BBOX, LO, HI, processes=2)
        report["reference_render_sec"] = round(time.perf_counter()-started, 3)
    finally:
        os.environ["PGDATABASE"] = old_pg
    report["parity"] = compare_strict(ref_archive, inc_archive)
    save()
    print("INCREMENTAL_E2E_RESULT", json.dumps(report, ensure_ascii=False), flush=True)
    assert report["parity"]["equal"], "Incremental MBTiles != independent full rebuild"
    print("INCREMENTAL_FULL_REBUILD_PARITY_PASS", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--work", type=Path, required=True)
    args = p.parse_args()
    run(args.work.resolve())


if __name__ == "__main__":
    main()
