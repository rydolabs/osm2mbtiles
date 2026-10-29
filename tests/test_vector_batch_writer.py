"""Regression tests for the tuned region-batch writer, without real PostGIS."""
import gzip
import sqlite3

from osm2mbtiles import vector_batch as v


def test_gzip_in_worker_matches_original_writer_bytes():
    raw = b"\x1a\x05hello" * 100
    rows = [(100, 200, memoryview(raw)), (101, 200, b"")]
    result = v._gzip_batch_rows(13, rows)
    assert result == [
        (13, 100, 200, gzip.compress(raw, compresslevel=6, mtime=0)),
        (13, 101, 200, b""),
    ]
    assert gzip.decompress(result[0][3]) == raw


def test_postgresql_prepare_cached_once_per_worker(monkeypatch):
    class Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append((sql, params))

    cur = Cursor()
    monkeypatch.setattr(v, "_queries", {})
    generated = []
    original = v.batch_query

    def build(z, count):
        generated.append((z, count))
        return original(z, count)

    monkeypatch.setattr(v, "batch_query", build)
    name, command = v._prepare_batch(cur, 14, 4)
    assert name == "mvt_z14_n4"
    assert command == "EXECUTE mvt_z14_n4 (%s, %s, %s, %s, %s, %s, %s, %s)"
    assert generated == [(14, 4)]
    prepared_sql = cur.statements[0][0]
    assert prepared_sql.startswith("PREPARE mvt_z14_n4 (integer, integer")
    assert "$1::integer" in prepared_sql and "$8::integer" in prepared_sql
    assert "%s::integer" not in prepared_sql
    assert "ST_AsMVTGeom" in prepared_sql
    assert v._prepare_batch(cur, 14, 4) == (name, command)
    assert generated == [(14, 4)]
    assert len(cur.statements) == 1


def test_writer_content_and_commit_count_on_1025_tiles(tmp_path, monkeypatch):
    # Fake parallel work only. SQLite schema/commit/checkpoint/rename are real.
    total = 1025
    raw = b"\x1a\x05hello" * 30
    packed = gzip.compress(raw, compresslevel=6, mtime=0)
    jobs = list(range(total))

    class FakePool:
        def __init__(self, processes, initializer):
            self.processes = processes

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def imap_unordered(self, fn, tasks, chunksize=1):
            for x in tasks:
                yield [(14, 500 + x, 600, packed)]

    class FakeCtx:
        Pool = FakePool

    class LoggedConn:
        def __init__(self, real):
            self.real = real
            self.tiled_inserts = []
            self.commits = 0

        def execute(self, *args, **kwargs):
            return self.real.execute(*args, **kwargs)

        def executescript(self, *args, **kwargs):
            return self.real.executescript(*args, **kwargs)

        def executemany(self, sql, rows):
            if "INTO tiles" in sql:
                self.tiled_inserts.append(len(rows))
            return self.real.executemany(sql, rows)

        def commit(self):
            self.commits += 1
            return self.real.commit()

        def __enter__(self):
            self.real.__enter__()
            return self

        def __exit__(self, *args):
            return self.real.__exit__(*args)

    connections = []
    original_connect = sqlite3.connect

    def traced_connect(path):
        wrapped = LoggedConn(original_connect(path))
        connections.append(wrapped)
        return wrapped

    monkeypatch.setattr(v.multiprocessing, "get_context", lambda *_: FakeCtx())
    monkeypatch.setattr(v, "microbatches", lambda *_a, **_kw: iter(jobs))
    monkeypatch.setattr(v, "tile_count", lambda *_a: total)
    monkeypatch.setattr(v.sqlite3, "connect", traced_connect)

    out = tmp_path / "tokyo.mbtiles"
    written = v.export_batch(tmp_path / "src.osm.pbf", out,
                             (139.55, 35.55, 139.95, 35.85), 14, 14, 4)
    assert written == total
    assert not (tmp_path / "tokyo.mbtiles.partial").exists()
    conn = connections[0]
    assert len(conn.tiled_inserts) == 5
    assert conn.tiled_inserts == [256, 256, 256, 256, 1]
    # 1 metadata commit, 1 for 1024 tiles, 1 final commit.
    assert conn.commits == 3
    with original_connect(out) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        data = list(db.execute("SELECT tile_data FROM tiles ORDER BY tile_column"))
        assert len(data) == total
        assert all(blob == packed for (blob,) in data)


def test_render_batch_rejects_extra_duplicate_sql_rows(monkeypatch):
    """A set-equality check alone used to accept duplicates of requested XYZ."""
    import pytest

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params):
            assert params == [10, 20, 11, 20]

        def fetchall(self):
            # Every requested coordinate exists, yet one row is duplicated.
            return [(10, 20, b"a"), (11, 20, b"b"), (10, 20, b"a")]

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(v, "_conn", FakeConnection())
    monkeypatch.setattr(v, "_prepare_batch", lambda *args: ("stmt", "EXECUTE stmt"))
    with pytest.raises(RuntimeError, match="duplicate"):
        v._render_batch((14, [(10, 20), (11, 20)]))
