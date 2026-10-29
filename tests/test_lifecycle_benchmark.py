"""Pure lifecycle benchmark regression checks (no PostGIS/Planetiler installed)."""
from __future__ import annotations

import gzip
import importlib.util
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ET

import pytest


@pytest.fixture(scope="module")
def module():
    path = Path(__file__).resolve().parents[1] / "benchmarks" / "lifecycle_incremental_vs_planetiler.py"
    spec = importlib.util.spec_from_file_location("_lifecycle_benchmark_test", path)
    assert spec and spec.loader
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def test_cumulative_includes_first_import_and_second_full_build(module):
    t = module.lifecycle_costs(383.71, 4.0, 89.97, 89.97, pbf_patch=2.5)
    assert t["postgis_two_snapshots_seconds"] == 387.71
    assert t["planetiler_two_builds_seconds"] == 179.94
    assert t["planetiler_two_builds_plus_pbf_patch_seconds"] == 182.44
    assert t["postgis_faster_at_two_snapshots_excluding_pbf_patch"] is False
    assert t["hypothetical_equal_size_updates_to_break_even"] == 4


def test_smoke_example_and_no_possible_amortization(module):
    t = module.lifecycle_costs(2.149, 0.875, 6.494, 4.643)
    assert t["postgis_two_snapshots_seconds"] == 3.024
    assert t["planetiler_two_builds_seconds"] == 11.137
    assert t["hypothetical_equal_size_updates_to_break_even"] == 0
    t = module.lifecycle_costs(10, 9, 1, 5)
    assert t["hypothetical_equal_size_updates_to_break_even"] is None


def test_invalid_times_rejected(module):
    for value in (-1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            module.lifecycle_costs(value, 1, 2, 3)


def test_synthetic_diff_is_valid_deterministic_osc_gz(tmp_path, module):
    a, b = tmp_path / "first.osc.gz", tmp_path / "second.osc.gz"
    for path in (a, b):
        module.write_synthetic_diff(path, lat=43.735, lon=7.425, node_id=12345)
    assert a.read_bytes() == b.read_bytes()
    root = ET.fromstring(gzip.decompress(a.read_bytes()))
    assert root.tag == "osmChange"
    node = root.find("./create/node")
    assert node is not None and node.attrib["id"] == "12345"
    assert node.find("./tag[@k='amenity']").attrib["v"] == "hospital"


def test_synthetic_id_is_near_existing_node_ids(monkeypatch, module, tmp_path):
    class FakePopen:
        stdout = [
            b"n8 v1 dV c1 t2026-10-01T00:00:00Z\n",
            b"n1400 v1 dV c1 t2026-10-01T00:00:00Z\n",
            b"w200 v1 dV c1 t2026-10-01T00:00:00Z\n",
        ]
        def wait(self):
            return 0
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: FakePopen())
    assert module.choose_new_node_id(tmp_path / "input.osm.pbf") == 1401


def test_archive_diff_detects_changed_compressed_tile_rows(tmp_path, module):
    before, after = tmp_path / "before.mbtiles", tmp_path / "after.mbtiles"
    for path, raw in ((before, b"first"), (after, b"second")):
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE tiles(zoom_level INTEGER,tile_column INTEGER,"
                "tile_row INTEGER,tile_data BLOB)")
            db.execute("INSERT INTO tiles VALUES(12,2132,1493,?)",
                       (gzip.compress(raw, mtime=0),))
    stats = module.compare_archives(before, after)
    assert stats["before_nonempty"] == stats["after_nonempty"] == 1
    assert stats["changed_gzip_tile_rows"] == 1
