"""User-facing progress: accurate denominators, resume and no fabricated ETA.

Pure Python Mapnik stub verifies the Raster DB path without installing Mapnik.
Production raster/Vector and PostGIS parity remain covered by real Actions.
"""
from __future__ import annotations

import io
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import types

import pytest

from osm2mbtiles import worker
from osm2mbtiles.progress import TileProgress, duration
from osm2mbtiles import runtime


def test_metatile_totals_exactly_match_real_task_scheduler():
    for bbox, lo, hi, size in (
        ((-180, -85, 180, 85), 0, 3, 8),
        ((126.8, 34, 129.5, 38), 0, 9, 8),
        ((126, 35, 128, 38), 5, 14, 2),
        ((7.41, 43.72, 7.44, 43.75), 12, 13, 8),
    ):
        job = dict(bbox=bbox, minzoom=lo, maxzoom=hi, metatile=size)
        tiles, metas = worker.expected_counts(job)
        scheduled = list(worker.tasks(job))
        assert metas == len(scheduled)
        assert tiles == sum(
            (t[6] - t[5] + 1) * (t[8] - t[7] + 1)
            for t in scheduled
        )


def test_raster_progress_resume_percent_rate_and_eta_use_real_work():
    out = io.StringIO()
    now = [0.0]
    with TileProgress("RASTER", 100, completed=20, metatiles=3,
                      stream=out, clock=lambda: now[0],
                      heartbeat_seconds=0) as tracker:
        now[0] = 5
        tracker.advance(40)
        now[0] = 10
        tracker.advance(40)
    text = out.getvalue()
    assert "total=100 tiles" in text
    assert "metatiles=3" in text
    assert "already-complete=20" in text
    assert "20.0%" in text
    assert "60.0%" in text
    assert "100.0%" in text
    assert "8.0 tiles/s" in text  # (100 - 20) / 10 seconds
    assert "ETA 00:00:00" in text
    assert "COMPLETE | 100/100" in text
    assert duration(3661) == "01:01:01"


def test_progress_cannot_exceed_total_or_claim_success_on_exception():
    output = io.StringIO()
    with pytest.raises(ValueError, match="More tiles"):
        with TileProgress("VECTOR", 2, stream=output,
                          heartbeat_seconds=0) as meter:
            meter.advance(3)
    assert "FAILED | 0/2" in output.getvalue()
    assert "100.0%" not in output.getvalue()


def test_stalled_sql_reports_working_not_fake_percentage():
    output = io.StringIO()
    with TileProgress("VECTOR-BATCH", 10, stream=output,
                      heartbeat_seconds=0.025) as progress:
        time.sleep(0.10)
        progress.advance(10)
    text = output.getvalue()
    assert "WORKING" in text and "current tile/query still running" in text
    assert text.count("100.0%") == 1


def test_real_worker_render_resume_and_no_extra_mapnik_render(monkeypatch, tmp_path, capsys):
    calls = []
    class FakeMap:
        srs = "+proj=merc +a=6378137"
        buffer_size = 0
        def __init__(self, w, h): pass
        def resize(self, w, h): pass
        def zoom_to_box(self, bbox): pass
    class FakeImage:
        def __init__(self, w, h): pass
        def view(self, *args):
            return self
        def tostring(self, encoding):
            assert encoding == "png"
            return b"not-real-png-test-fixture"
    monkeypatch.setitem(sys.modules, "mapnik", types.SimpleNamespace(
        Map=FakeMap, Image=FakeImage, Box2d=lambda *args: args,
        load_map=lambda *args: None,
        render=lambda *args: calls.append("render"),
    ))
    job = {
        "runtime": str(tmp_path / "absent-native-runtime"),
        "partial": str(tmp_path / "test.mbtiles.partial"),
        "bbox": (7.41,43.72,7.44,43.75),
        "minzoom": 12, "maxzoom": 13,
        "metatile": 8, "buffer": 128,
        "job_key": "test-only-job-key", "name": "test",
        "mapfile": str(tmp_path / "test.xml"),
    }
    worker.render(job)
    first = capsys.readouterr().out
    assert "[RASTER] START" in first
    assert "[RASTER] COMPLETE" in first
    total, _ = worker.expected_counts(job)
    with sqlite3.connect(job["partial"]) as db:
        assert db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0] == total
    rendered_once = len(calls)
    assert rendered_once > 0
    worker.render(job)
    resumed = capsys.readouterr().out
    assert f"already-complete={total}" in resumed
    assert "[RASTER] COMPLETE" in resumed
    assert len(calls) == rendered_once


def test_native_import_has_heartbeat_without_invented_percent(monkeypatch, capsys):
    class FakeProcess:
        count = 0
        def wait(self, timeout=None):
            self.count += 1
            if self.count == 1:
                raise subprocess.TimeoutExpired("osm2pgsql", timeout)
            return 0
        def poll(self): return 0
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    runtime.run(["osm2pgsql", "--slim", "data.osm.pbf"])
    out = capsys.readouterr().out
    assert "[IMPORT] osm2pgsql still running" in out
    assert "total work unknown" in out
    assert "%" not in out


def test_wsl_has_phase_guidance_and_keeps_original_runtime_args():
    content = (Path(__file__).resolve().parents[1]/"tools/run_wsl.sh").read_text()
    for label in ("[STAGE] 1/5", "[STAGE] 2/5",
                  "[STAGE] 3/5", "[STAGE] 4/5", "[STAGE] 5/5"):
        assert label in content
    assert '--runtime "$RUNTIME" --workdir "$WORKDIR"' in content
