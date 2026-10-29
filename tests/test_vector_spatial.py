"""Z11 adaptive locality scheduler tests: EXACT unchanged SQL microbatch membership."""
import collections
import itertools

import pytest

from osm2mbtiles import vector_batch as v
from osm2mbtiles.cli import arguments, convert
from osm2mbtiles.runtime import PipelineError


@pytest.mark.parametrize("bbox,zooms", [
    ((139.55, 35.55, 139.95, 35.85), (12, 16)),  # actual Tokyo pilot
    ((127.015, 37.485, 127.075, 37.535), (9, 15)),
    ((7.41, 43.72, 7.44, 43.75), (3, 16)),
    ((-0.18, 51.49, 0.02, 51.56), (12, 17)),
])
@pytest.mark.parametrize("side", [1, 2, 3, 4, 8])
@pytest.mark.parametrize("max_batches", [8, 32])
def test_sharding_preserves_every_original_sql_microbatch(bbox, zooms, side, max_batches):
    lo, hi = zooms
    expected = list(v.microbatches(bbox, lo, hi, side))
    shards = list(v.spatial_shards(
        bbox, lo, hi, side=side, group_zoom=11, max_batches=max_batches))
    actual = list(itertools.chain.from_iterable(shards))
    def key(job):
        z, cells = job
        return (z, tuple(cells))
    assert collections.Counter(map(key, actual)) == collections.Counter(map(key, expected))
    assert all(1 <= len(shard) <= max_batches for shard in shards)
    assert sum(len(cells) for _, cells in actual) == v.tile_count(bbox, lo, hi)


def test_locality_shard_contains_one_parent_quadtree_region():
    bbox = (139.55, 35.55, 139.95, 35.85)
    shards = list(v.spatial_shards(bbox, 12, 16, 2, group_zoom=11, max_batches=32))
    assert len(shards) > 4
    for shard in shards:
        z = shard[0][0]
        assert all(job[0] == z for job in shard)
        parents = {(cells[0][0] >> (z - 11), cells[0][1] >> (z - 11))
                   for _, cells in shard}
        assert len(parents) == 1


def test_shard_invokes_same_render_batch_without_modifying_sql_arguments(monkeypatch):
    calls = []
    def renderer(job):
        calls.append(job)
        z, cells = job
        return [(z, x, y, b"some data") for x, y in cells]
    monkeypatch.setattr(v, "_render_batch", renderer)
    jobs = [(14, [(100, 200), (101, 200)]), (14, [(102, 200)])]
    result = v._render_spatial_shard(jobs)
    assert calls == jobs
    assert result == [(14, 100, 200, b"some data"),
                      (14, 101, 200, b"some data"),
                      (14, 102, 200, b"some data")]


@pytest.mark.parametrize("kwargs", [
    {"group_zoom": -1}, {"group_zoom": 23},
    {"max_batches": 0}, {"max_batches": 257},
])
def test_invalid_spatial_scheduler_settings(kwargs):
    with pytest.raises(ValueError):
        list(v.spatial_shards((139.55, 35.55, 139.95, 35.85), 12, 16, **kwargs))


def test_scheduler_cli_is_opt_in_and_batch_only(monkeypatch):
    base = ["convert", "dummy.osm.pbf", "-o", "dummy.mbtiles",
            "--tile-type", "vector"]
    args, _ = arguments(base)
    assert args.vector_spatial_shard_zoom is None
    assert args.vector_spatial_max_batches == 32

    args, _ = arguments(base + ["--vector-tiler", "batch",
                                "--vector-spatial-shard-zoom", "11",
                                "--vector-spatial-max-batches", "16"])
    assert (args.vector_spatial_shard_zoom, args.vector_spatial_max_batches) == (11, 16)
