"""Update profile, safety invariants and CLI structural tests (no PostGIS)."""
from pathlib import Path

import pytest

from osm2mbtiles import vector_incremental as v


def test_update_style_preserves_legacy_source_and_expires_all_geometries():
    source = v.source_style().read_text()
    generated = v.configured_style(12, 15)
    assert "define_expire_output" in generated
    assert "minzoom = 12" in generated and "maxzoom = 15" in generated
    assert generated.count("output = vector_expire") == 4
    assert generated.count("buffer = 64.0 / 4096") == 4
    assert source.count("osm2pgsql.process_") == generated.count("osm2pgsql.process_")
    assert "--drop" not in generated


def test_configured_style_invalid_zoom_rejected():
    for lo, hi in [(-1, 15), (14, 12), (12, 23)]:
        with pytest.raises(ValueError):
            v.configured_style(lo, hi)


def test_missing_diff_fails_before_db_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "_connect", lambda: pytest.fail("DB accessed"))
    with pytest.raises(FileNotFoundError):
        v.append(tmp_path / "missing.osc", tmp_path / "out.mbtiles")


def test_missing_pbf_fails_before_db_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "_connect", lambda: pytest.fail("DB accessed"))
    with pytest.raises(FileNotFoundError):
        v.init(tmp_path / "missing.pbf", tmp_path / "out.mbtiles",
               (126.97, 37.48, 127.03, 37.53), 12, 15)


def test_incremental_fingerprint_tracks_vector_query_source():
    from pathlib import Path
    from osm2mbtiles import vector_incremental as inc
    from osm2mbtiles import vector
    p = Path(vector.__file__).resolve()
    assert p.is_file()
    assert inc.style_digest()
    assert "with_name('vector.py')" in Path(inc.__file__).read_text()
