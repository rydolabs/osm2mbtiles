"""Pure proof that the new MBTiles I/O harness checks actual stored bytes."""
import gzip
import importlib.util
from pathlib import Path
import sqlite3

p = Path(__file__).resolve().parents[1] / "benchmarks/vector_export_io_profile.py"
spec = importlib.util.spec_from_file_location("vector_export_io_profile", p)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def create_archive(path, compressed):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE metadata (name text primary key, value text)")
        db.executemany("INSERT INTO metadata VALUES (?,?)",
                       [("name",path.stem),("format","pbf"),("version","1.3")])
        db.execute("CREATE TABLE tiles (zoom_level integer, tile_column integer, "
                   "tile_row integer,tile_data blob,"
                   "PRIMARY KEY(zoom_level,tile_column,tile_row))")
        db.execute("INSERT INTO tiles VALUES (12,300,1000,?)", (compressed,))


def test_same_bytes_across_different_archive_filenames(tmp_path):
    compressed=gzip.compress(b"same-tile",compresslevel=6,mtime=0)
    a,b=tmp_path/"a.mbtiles",tmp_path/"b.mbtiles"
    create_archive(a,compressed)
    create_archive(b,compressed)
    result=probe.compare_manifests(probe.full_manifest(a),probe.full_manifest(b))
    assert result["exact_gzip_and_xyz"] and result["gzip_mismatches"]==0
    assert result["reference_tiles"]==1


def test_gzip_payload_difference_is_strictly_rejected(tmp_path):
    a,b=tmp_path/"a.mbtiles",tmp_path/"b.mbtiles"
    create_archive(a,gzip.compress(b"same",mtime=0,compresslevel=6))
    create_archive(b,gzip.compress(b"other",mtime=0,compresslevel=6))
    r=probe.compare_manifests(probe.full_manifest(a),probe.full_manifest(b))
    assert not r["exact_gzip_and_xyz"] and r["gzip_mismatches"]==1
    assert r["mismatch_examples"]==[(12,300,3095)]


def test_render_compressed_delegates_to_original_sql_and_uses_same_gzip(monkeypatch):
    task=(12,300,3095)
    calls=[]
    def source(arg):
        calls.append(arg)
        return *arg,b"mock MVT payload"
    monkeypatch.setattr(probe.vector,"render_one",source)
    got=probe.render_compressed(task)
    assert calls==[task] and got[:3]==task
    assert got[3]==gzip.compress(b"mock MVT payload",compresslevel=6,mtime=0)
    assert got[4]>=0 and got[5]>=0


def test_berlin_stress_replaces_tiny_monaco_performance_scope():
    assert "monaco" not in probe.BBOX
    assert "berlin-stress" in probe.BBOX
    assert probe.vector.tile_count(probe.BBOX["berlin-stress"],
                                   probe.ZLO, probe.ZHI) >= 10000
