"""Fail-closed vector-only runtime tests: Mapnik/Carto not required."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from osm2mbtiles import cli
from osm2mbtiles.runtime import Runtime, PipelineError


TOOLS = ("initdb", "pg_ctl", "createdb", "psql", "osm2pgsql")


def fake_native_tools(path: Path, monkeypatch):
    path.mkdir()
    for name in TOOLS:
        target = path / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
    monkeypatch.setenv("PATH", str(path))


def test_vector_only_uses_system_native_tools_not_carto(monkeypatch, tmp_path):
    fake_native_tools(tmp_path / "bin", monkeypatch)
    original_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name: object() if name == "psycopg2" else original_find_spec(name)
    )
    runtime = Runtime(tmp_path / "no-bundle", vector_only=True)
    runtime.validate()
    assert runtime.python == Path(sys.executable)
    assert runtime.style.is_dir() is False
    for name in TOOLS:
        assert runtime.bin(name) == tmp_path / "bin" / name


def test_raster_and_both_keep_strict_full_runtime(monkeypatch, tmp_path):
    fake_native_tools(tmp_path / "bin", monkeypatch)
    full = Runtime(tmp_path / "no-bundle")
    with pytest.raises(PipelineError, match="Native runtime incomplete") as exc:
        full.validate()
    assert "python-with-mapnik" in str(exc.value)
    assert "osm-carto/project.xml" in str(exc.value)
    with pytest.raises(PipelineError, match="Missing bundled executable"):
        full.bin("osm2pgsql")


def test_vector_only_missing_psycopg2_is_an_actionable_error(monkeypatch, tmp_path):
    fake_native_tools(tmp_path / "bin", monkeypatch)
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: None if name == "psycopg2" else object())
    with pytest.raises(PipelineError, match=r"pip install -e \.\[vector\]"):
        Runtime(tmp_path / "no-bundle", vector_only=True).validate()


def test_doctor_vector_only_and_default_raster_flag(monkeypatch, tmp_path, capsys):
    fake_native_tools(tmp_path / "bin", monkeypatch)
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: object())
    assert cli.main(["doctor", "--vector-only", "--runtime",
                     str(tmp_path / "absent-carto")]) == 0
    assert "Vector-only system tools OK" in capsys.readouterr().out
    assert cli.main(["doctor", "--runtime",
                     str(tmp_path / "absent-carto")]) == 2
    assert "Native runtime incomplete" in capsys.readouterr().err


def test_convert_selects_vector_only_runtime_and_keeps_output_guard(monkeypatch, tmp_path):
    fake_native_tools(tmp_path / "bin", monkeypatch)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    pbf = tmp_path / "data.osm.pbf"
    pbf.write_bytes(b"test fixture; --bbox bypasses header reading")
    output = tmp_path / "out.mbtiles"
    got = []

    def fake_run_pipeline(pbf, out, bbox, lo, hi, args, work, runtime):
        got.append((runtime.vector_only, runtime.python, tuple(bbox), lo, hi))
        out.write_bytes(b"sentinel")
    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)
    argv = ["convert", str(pbf), "--bbox", "7.41,43.72,7.44,43.75",
            "--zoom", "12-13", "--tile-type", "vector", "--output",
            str(output), "--runtime", str(tmp_path / "missing-carto")]
    assert cli.main(argv) == 0
    assert output.read_bytes() == b"sentinel"
    assert got == [(True, Path(sys.executable), (7.41,43.72,7.44,43.75), 12,13)]
    assert cli.main(argv) == 2
    assert output.read_bytes() == b"sentinel"


def test_vector_pipeline_does_not_read_or_erase_carto_cache(monkeypatch, tmp_path):
    pbf = tmp_path / "fixture.osm.pbf"
    pbf.write_bytes(b"pbf fixture only hashed here")
    work = tmp_path / "work"
    work.mkdir()
    (work / "import-state.json").write_text('{"import_key":"old-raster-style"}')
    (work / "pgdata").mkdir()
    (work / "pgdata" / "PG_VERSION").write_text("16")
    # A stale raster cache must never cause vector-only to delete our cluster.
    args,_ = cli.arguments(["convert", str(pbf), "--output", str(tmp_path / "vec.mbtiles"),
                            "--tile-type", "vector", "--bbox", "7.41,43.72,7.44,43.75"])
    imports = []
    workers = []

    class FakeDB:
        def __init__(self, runtime, work):
            assert runtime.vector_only
            self.env = {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def import_vector_extract(self, pbf_arg, directory, profile):
            imports.append((pbf_arg, directory, profile))

    def fake_run(cmd, env=None, cwd=None):
        assert cmd[0] == Path(sys.executable)
        assert Path(cmd[1]).name == "vector_worker.py"
        assert cwd is None
        job = json.loads(Path(cmd[2]).read_text())
        Path(job["output"]).write_bytes(b"mock vector output")
        workers.append(job)

    monkeypatch.setattr(cli, "LocalDatabase", FakeDB)
    monkeypatch.setattr(cli, "run", fake_run)
    runtime = Runtime(tmp_path / "no-runtime", vector_only=True)
    out = tmp_path / "vec.mbtiles"
    cli.run_pipeline(pbf, out, (7.41,43.72,7.44,43.75), 12,13,
                     args, work, runtime)
    assert out.read_bytes() == b"mock vector output"
    assert len(imports) == 1
    assert workers[0]["tiler"] == "postgis"
    assert (work / "vector-state.json").is_file()
    assert not (work / "style").exists()
    assert (work / "pgdata" / "PG_VERSION").read_text() == "16"
    assert json.loads((work / "import-state.json").read_text()) == {
        "import_key": "old-raster-style"
    }

    # Reusing the exact same persistent work folder should reuse the vector
    # import irrespective of stale raster state, not reimport or change its key.
    out2 = tmp_path / "vec2.mbtiles"
    cli.run_pipeline(pbf, out2, (7.41,43.72,7.44,43.75), 12,13,
                     args, work, runtime)
    assert out2.read_bytes() == b"mock vector output"
    assert len(imports) == 1
    assert len(workers) == 2
