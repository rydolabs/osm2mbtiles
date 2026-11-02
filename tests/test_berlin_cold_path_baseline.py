"""Contract tests for independent fresh-DB Berlin initial-build baseline."""
import gzip
import importlib.util
from pathlib import Path
import sqlite3

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location(
    "berlin_cold_path_baseline",ROOT/"benchmarks/berlin_cold_path_baseline.py")
cold=importlib.util.module_from_spec(spec)
spec.loader.exec_module(cold)


def test_cold_baseline_uses_same_pbf_zoom_area_as_warm_reference():
    import json
    ref=json.loads((ROOT/"benchmarks/baselines/berlin-stress-vector-postgis-20261102.json")
                   .read_text())
    assert cold.EXPECTED_PBF_SHA==ref["input"]["sha256"]
    assert cold.EXPECTED_PBF_BYTES==ref["input"]["size_bytes"]
    assert list(cold.BBOX)==ref["input"]["bbox"]
    assert [cold.LO,cold.HI]==ref["input"]["zoom"]
    assert cold.EXPECTED_NONEMPTY==ref["export"]["nonempty_xyz"]
    assert cold.EXPECTED_GZIP_BYTES==ref["export"]["total_gzip_tile_data_bytes"]
    assert cold.vector.tile_count(cold.BBOX,cold.LO,cold.HI)==16943


def test_archive_manifest_hashes_exact_stored_gzip_and_flips_to_xyz(tmp_path):
    p=tmp_path/"cold.mbtiles"
    blob=gzip.compress(b"mvt",compresslevel=6,mtime=0)
    with sqlite3.connect(p) as db:
        db.execute("CREATE TABLE metadata(name text PRIMARY KEY,value text)")
        db.executemany("INSERT INTO metadata VALUES (?,?)",
                       [("name","cold"),("format","pbf")])
        db.execute("CREATE TABLE tiles(zoom_level integer,tile_column integer,"
                   "tile_row integer,tile_data blob, "
                   "PRIMARY KEY(zoom_level,tile_column,tile_row))")
        db.execute("INSERT INTO tiles VALUES (12, 2198, 2752, ?)",(blob,))
    m=cold.archive_manifest(p)
    assert list(m["rows"])==[(12,2198,1343)]
    assert m["compressed_bytes"]==len(blob)
    assert m["metadata"]=={"format":"pbf"}


def test_archive_manifest_fails_closed_on_nongzip(tmp_path):
    p=tmp_path/"invalid.mbtiles"
    with sqlite3.connect(p) as db:
        db.execute("CREATE TABLE metadata(name text PRIMARY KEY,value text)")
        db.execute("INSERT INTO metadata VALUES ('format','pbf')")
        db.execute("CREATE TABLE tiles(zoom_level integer,tile_column integer,"
                   "tile_row integer,tile_data blob)")
        db.execute("INSERT INTO tiles VALUES (12, 1, 2, ?)",(b"not gzip",))
    with pytest.raises(RuntimeError,match="gzip"):
        cold.archive_manifest(p)


def test_independent_db_semantic_multiset_ignores_mvt_feature_order(tmp_path):
    import mapbox_vector_tile

    a={"geometry":{"type":"Point","coordinates":[100,100]},
       "properties":{"name":"a"},"id":1}
    b={"geometry":{"type":"Point","coordinates":[200,200]},
       "properties":{"name":"b"},"id":2}

    def make(path,features):
        encoded=mapbox_vector_tile.encode({"name":"poi","features":features})
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE metadata(name text PRIMARY KEY,value text)")
            db.executemany("INSERT INTO metadata VALUES (?,?)",
                           [("name",path.stem),("format","pbf")])
            db.execute("CREATE TABLE tiles(zoom_level integer,tile_column integer,"
                       "tile_row integer,tile_data blob)")
            db.execute("INSERT INTO tiles VALUES (12, 2198, 2752, ?)",
                       (gzip.compress(encoded,compresslevel=6,mtime=0),))

    left,right=tmp_path/"a.mbtiles",tmp_path/"b.mbtiles"
    make(left,[a,b])
    make(right,[b,a])
    first=cold.archive_manifest(left,verify_decoded=True)
    second=cold.archive_manifest(right,verify_decoded=True)
    assert first["rows"] != second["rows"]
    assert first["decoded"]==second["decoded"]
    assert first["decoded_feature_total"]==2
    assert second["decoded_feature_total"]==2
