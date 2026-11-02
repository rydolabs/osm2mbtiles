"""Pure, dependency-free checks for the isolated persistent Batch probe."""
import importlib.util
from pathlib import Path
import sqlite3

SRC = Path(__file__).resolve().parents[1] / "benchmarks" / "persistent_batch_probe.py"
spec = importlib.util.spec_from_file_location("persistent_batch_probe", SRC)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def archive(path, tiles):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tiles("
                   "zoom_level integer,tile_column integer,tile_row integer,"
                   "tile_data blob,PRIMARY KEY(zoom_level,tile_column,tile_row))")
        db.executemany("INSERT INTO tiles VALUES(?,?,?,?)", tiles)


def test_byte_compare_exact_and_changed_payload(tmp_path):
    a, b = tmp_path/"a.mbtiles", tmp_path/"b.mbtiles"
    archive(a, [(12, 1, 4094, b"same"), (12, 2, 4094, b"old")])
    archive(b, [(12, 1, 4094, b"same"), (12, 2, 4094, b"new")])
    result = audit.byte_compare(a, b)
    assert result["compared_tiles"] == 2
    assert result["gzip_mismatches"] == 1
    assert result["xyz_mismatches"] == 0
    assert result["samples"][0]["xyz"] == (12, 2, 1)
    assert audit.byte_compare(a, a)["byte_equal"]


def test_byte_compare_missing_coordinates(tmp_path):
    a, b = tmp_path/"a.mbtiles", tmp_path/"b.mbtiles"
    archive(a, [(12, 1, 4094, b"same")])
    archive(b, [(12, 1, 4094, b"same"), (12, 2, 4094, b"new")])
    result = audit.byte_compare(a, b)
    assert result["xyz_mismatches"] == 1
    assert not result["byte_equal"]


def test_explain_summary_is_read_only_aggregation():
    class Cursor:
        def execute(self, query, params):
            assert query.startswith("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT ")
            assert params == [1, 2]
        def fetchone(self):
            return ([{"Execution Time":3.2, "Planning Time":0.4,
                      "Plan": {"Node Type":"Nested Loop",
                               "Shared Hit Blocks":5, "Shared Read Blocks":2,
                               "Plans":[{"Node Type":"Index Scan",
                                         "Relation Name":"vector_points"}]}}],)
    s = audit.explain_summary(Cursor(), "SELECT 1", [1, 2])
    assert s["shared_hit_blocks"] == 5
    assert s["node_types"] == [("Nested Loop", 1), ("Index Scan", 1)]
    assert s["relations"] == [("vector_points", 1)]
