"""Regression: Python Mapnik font registration accepts a directory, not a bool."""
from pathlib import Path
import sys
import types

import pytest

from osm2mbtiles import worker


def test_mapnik_font_registration_uses_python_api(tmp_path, monkeypatch):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "subdir").mkdir()
    calls = []
    class ReachedMapConstructor(Exception):
        pass

    def register_fonts(path, valid_extensions=(".ttf", ".otf")):
        assert valid_extensions == (".ttf", ".otf"), (
            "A bool was passed as Python Mapnik's font extension list"
        )
        calls.append(path)
        return True

    def map_constructor(*args):
        raise ReachedMapConstructor()

    fake = types.SimpleNamespace(register_fonts=register_fonts, Map=map_constructor)
    monkeypatch.setitem(sys.modules, "mapnik", fake)
    # This regression exercises the font API and stops before tile rendering.
    monkeypatch.setattr(worker, "create_db", lambda db, job: None)
    with pytest.raises(ReachedMapConstructor):
        worker.render({
            "runtime": str(tmp_path),
            "fonts": str(fonts),
            "partial": str(tmp_path / "render.mbtiles"),
            "mapfile": str(tmp_path / "style.xml"),
            "minzoom": 12, "maxzoom": 12,
            "bbox": [126.99, 37.49, 127.01, 37.51],
        })
    assert calls == [str(fonts)]


def test_mapnik_font_registration_skips_missing_directory(tmp_path, monkeypatch):
    calls = []

    class ReachedMapConstructor(Exception):
        pass

    fake = types.SimpleNamespace(
        register_fonts=lambda *a, **k: calls.append((a, k)),
        Map=lambda *args: (_ for _ in ()).throw(ReachedMapConstructor()),
    )
    monkeypatch.setitem(sys.modules, "mapnik", fake)
    # This regression exercises the font API and stops before tile rendering.
    monkeypatch.setattr(worker, "create_db", lambda db, job: None)
    with pytest.raises(ReachedMapConstructor):
        worker.render({"runtime": str(tmp_path),
                       "fonts": str(tmp_path / "no-font-directory"),
                       "partial": str(tmp_path / "out.mbtiles")})
    assert calls == []
