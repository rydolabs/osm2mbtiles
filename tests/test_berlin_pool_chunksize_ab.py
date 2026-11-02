"""Benchmark-only task dispatch tests, without PostGIS or a PBF."""
import multiprocessing.pool

import pytest

from benchmarks.berlin_pool_chunksize_ab import (
    BBOX, CHUNK_SIZE, EXPECTED_XYZ, override_chunk_size,
)
from osm2mbtiles import vector


def test_berlin_task_count_frozen():
    assert CHUNK_SIZE == 4
    assert vector.tile_count(BBOX, 12, 16) == EXPECTED_XYZ == 16943


def test_only_original_vector_render_gets_modified_chunksize(monkeypatch):
    observed = []
    def fake_imap(pool, fn, iterable, chunksize=1):
        observed.append((fn, chunksize))
        return list(iterable)
    monkeypatch.setattr(multiprocessing.pool.Pool, "imap_unordered", fake_imap)
    with override_chunk_size() as calls:
        result = multiprocessing.pool.Pool.imap_unordered(
            None, vector.render_one, [(12, 1, 2)], chunksize=1)
        assert result == [(12, 1, 2)]
        multiprocessing.pool.Pool.imap_unordered(
            None, object(), [(9, 8)], chunksize=1)
        assert len(calls) == 1
    assert observed[0] == (vector.render_one, CHUNK_SIZE)
    assert observed[1][1] == 1
    assert multiprocessing.pool.Pool.imap_unordered is fake_imap


def test_fails_closed_if_no_render_was_intercepted(monkeypatch):
    monkeypatch.setattr(multiprocessing.pool.Pool, "imap_unordered",
                        lambda *a, **kw: [])
    with pytest.raises(RuntimeError, match="Expected exactly 1"):
        with override_chunk_size():
            pass
