#!/usr/bin/env python3
"""Real sequential OSM diffs + crash-safe flush + independent rebuild proof.

Runs only against disposable GitHub Actions PostGIS, never user DBs.
OSM changes are synthetic; archive generation and osm2pgsql append are real.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import psycopg2
from osm2mbtiles import vector_incremental as inc
from osm2mbtiles.vector import export as full_export
from incremental_parity_e2e import fixtures, node, BBOX, LO, HI
from postgis_batch_parity import content_signature


def scalar(sql: str):
    with psycopg2.connect() as db, db.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def main():
    work = Path(os.environ["RELEASE_WORK"])
    work.mkdir(parents=True, exist_ok=True)
    assert os.environ.get("PGDATABASE") == "gis", "Refuse non-disposable database"
    initial, diff1, final = fixtures(work)
    archive = work / "sequential-updated.mbtiles"
    final_archive = work / "independent-final.mbtiles"
    inc.init(initial, archive, BBOX, LO, HI, processes=2)
    baseline, _, _ = content_signature(archive)

    # First diff: node create/delete/move/tag-change and line/polygon edits.
    inc.append(diff1, archive, flush=False)
    assert scalar("SELECT phase FROM osm2mbtiles_vector_sync") == "pending"
    try:
        inc.append(diff1, archive, flush=False)
    except RuntimeError as exc:
        assert "Pending" in str(exc)
    else:
        raise AssertionError("Allowed a new diff while prior flush pending")
    first = inc.flush_tiles(archive)
    assert first["dirty_in_archive"] > 0
    after_one, _, _ = content_signature(archive)
    assert after_one != baseline

    # Second independent diff creates a new POI. Prove the recorded pending
    # queue survives a lost MBTiles file and can be drained after recovery.
    cafe = node(106, 127.011, 37.513,
                {"amenity": "post_office", "name": "Release Second Diff"})
    second_xml = (
        '<osmChange version="0.6" generator="release-multidiff">'
        '<create>' + cafe + '</create></osmChange>\n'
    )
    diff2 = work / "second.osc.gz"
    diff2.write_bytes(gzip.compress(second_xml.encode(), mtime=0))
    inc.append(diff2, archive, flush=False)
    assert scalar("SELECT phase FROM osm2mbtiles_vector_sync") == "pending"
    expired_before = scalar("SELECT count(*) FROM vector_expired_tiles")
    assert expired_before > 0
    parked = work / "parked.mbtiles"
    archive.rename(parked)
    try:
        try:
            inc.flush_tiles(archive)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("Missing archive flush unexpectedly succeeded")
        assert scalar("SELECT phase FROM osm2mbtiles_vector_sync") == "pending"
        assert scalar("SELECT count(*) FROM vector_expired_tiles") == expired_before
    finally:
        parked.rename(archive)
    second = inc.flush_tiles(archive)
    assert second["dirty_in_archive"] > 0
    after_two, _, _ = content_signature(archive)
    assert after_two != after_one
    try:
        inc.append(diff2, archive, flush=False)
    except RuntimeError as exc:
        assert "same diff twice" in str(exc)
    else:
        raise AssertionError("Duplicate second diff was accepted")

    # Third distinct diff deletes the POI added in diff2. Final state should
    # exactly match the independent full import of the first diff's final OSM.
    third_xml = (
        '<osmChange version="0.6" generator="release-multidiff">'
        '<delete>' + node(106, 127.011, 37.513, version=2)
        + '</delete></osmChange>\n'
    )
    diff3 = work / "third.osc.gz"
    diff3.write_bytes(gzip.compress(third_xml.encode(), mtime=0))
    inc.append(diff3, archive)
    after_three, _, updated_stats = content_signature(archive)
    assert after_three == after_one, (
        "The create+delete lifecycle failed to restore the decoded MVT content"
    )
    assert scalar("SELECT phase FROM osm2mbtiles_vector_sync") == "idle"
    assert scalar("SELECT count(*) FROM vector_expired_tiles") == 0

    # Recreating the exact initial + all three OSM changes in fresh PostGIS
    # must match the resulting updated MVT by decoded feature/geometry data.
    subprocess.run(["createdb", "refgis"], check=True)
    subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", "refgis",
                    "-c", "CREATE EXTENSION postgis; CREATE EXTENSION hstore;"], check=True)
    subprocess.run([
        "osm2pgsql", "--slim", "--drop", "--cache=256",
        "--number-processes=1", "-O", "flex", "-S",
        str(inc.source_style()), "-d", "refgis", str(final)
    ], check=True)
    subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", "refgis",
                    "-f", str(inc.post_import_sql())], check=True)
    os.environ["PGDATABASE"] = "refgis"
    try:
        full_export(final, final_archive, BBOX, LO, HI, processes=2)
    finally:
        os.environ["PGDATABASE"] = "gis"
    reference, _, reference_stats = content_signature(final_archive)
    assert after_three == reference, (
        "Three sequential OSC diffs differ from an independent final OSM import"
    )
    with sqlite3.connect(archive) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    report = {
        "diff_count": 3,
        "initial_osm_sha256": hashlib.sha256(initial.read_bytes()).hexdigest(),
        "diff_sha256": [hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (diff1, diff2, diff3)],
        "different_after_first": after_one != baseline,
        "different_after_second": after_two != after_one,
        "third_reverses_second": after_three == after_one,
        "independent_full_rebuild_semantic_equal": after_three == reference,
        "archive_loss_kept_pending_queue": True,
        "duplicate_diff_rejected": True,
        "initial_dirty_in_archive": first["dirty_in_archive"],
        "second_dirty_in_archive": second["dirty_in_archive"],
        "final_stats": updated_stats,
        "reference_stats": reference_stats,
    }
    path = work / "release-sequential-diffs.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n")
    print("RELEASE_THREE_REAL_OSC_DIFFS_FULL_REBUILD_PASS",json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
