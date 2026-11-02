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

def test_import_and_append_use_native_osm2pgsql_cache_default():
    import ast
    tree = ast.parse(Path(v.__file__).read_text(encoding="utf-8"))
    methods = {node.name: node for node in tree.body
               if isinstance(node, ast.FunctionDef)}
    for name in ("init", "append"):
        calls = [node for node in ast.walk(methods[name])
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == "_run"]
        importer = [node for node in calls if node.args
                    and isinstance(node.args[0], ast.List)
                    and node.args[0].elts
                    and isinstance(node.args[0].elts[0], ast.Constant)
                    and node.args[0].elts[0].value == "osm2pgsql"]
        assert len(importer) == 1
        flags = [e.value for e in importer[0].args[0].elts
                 if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        assert "--slim" in flags
        assert "--number-processes=1" in flags
        assert not any(flag == "-C" or flag.startswith("--cache") for flag in flags)
        assert ("--append" in flags) == (name == "append")
