"""Safe opt-in PostgreSQL work_mem override for PostGIS Batch workers."""
import sys
import types

import pytest
from osm2mbtiles import vector_batch as v


class Cursor:
    def __init__(self):
        self.statements = []
    def execute(self, sql, params=None):
        self.statements.append((sql, params))
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


class Connection:
    def __init__(self):
        self.autocommit = None
        self.cursors = []
    def cursor(self):
        c = Cursor()
        self.cursors.append(c)
        return c


@pytest.mark.parametrize("mem,expected", [
    (None, None), ("8MB", "8MB"), ("16MB", "16MB"), ("32MB", "32MB")
])
def test_worker_override_is_session_only_and_off_by_default(monkeypatch, mem, expected):
    instance = Connection()
    monkeypatch.setitem(sys.modules, "psycopg2",
                        types.SimpleNamespace(connect=lambda dbname: instance))
    if mem is None:
        monkeypatch.delenv("OSM2MBTILES_VECTOR_BATCH_WORK_MEM", raising=False)
    else:
        monkeypatch.setenv("OSM2MBTILES_VECTOR_BATCH_WORK_MEM", mem)
    monkeypatch.setattr(v, "_queries", {(14, 4): ("stale", "statement")})
    v._init_worker()
    assert instance.autocommit is True
    assert v._queries == {}
    if expected:
        assert len(instance.cursors) == 1
        assert instance.cursors[0].statements == [
            ("SELECT set_config('work_mem', %s, false)", (expected,))
        ]
    else:
        assert instance.cursors == []


@pytest.mark.parametrize("value", ["-1MB", "1GB", "16MB;DROP TABLE tiles", "1MB", ""])
def test_worker_override_rejects_unknown_and_injection(monkeypatch, value):
    instance = Connection()
    monkeypatch.setitem(sys.modules, "psycopg2",
                        types.SimpleNamespace(connect=lambda dbname: instance))
    monkeypatch.setenv("OSM2MBTILES_VECTOR_BATCH_WORK_MEM", value)
    with pytest.raises(ValueError, match="Unsupported"):
        v._init_worker()
    assert instance.cursors == []
