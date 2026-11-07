"""Real progress displays are based on completed XYZ tiles, never guessed import %."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import types

import pytest

from osm2mbtiles import runtime, vector, vector_batch, worker
from osm2mbtiles.tiles import tile_count

ROOT = Path(__file__).resolve().parents[1]


def test_raster_expected_counts_match_xyz_tile_plan():
    for bbox in [(-1, -1, 1, 1), (7.41, 43.72, 7.44, 43.75),
                 (126.95, 37.48, 127.12, 37.62)]:
        job = dict(bbox=bbox, minzoom=2, maxzoom=5)
        counts = worker.expected_counts(job)
        assert sum(counts.values()) == tile_count(bbox, 2, 5)
        assert all(v > 0 for v in counts.values())


def test_raster_progress_rate_eta_and_resumed_work(capsys):
    times = iter([10.0, 11.0, 13.0, 13.0])
    meter = worker.ProgressBar(10, initial=2, clock=lambda: next(times), interval=5)
    meter.update(2, zoom=12, force=True)
    meter.update(6, zoom=12, metatiles=4)
    meter.update(10, zoom=12, metatiles=8)
    out = capsys.readouterr().out
    assert '[RASTER]' in out
    assert '20.0%' in out and '60.0%' in out and '100.0%' in out
    assert '6/10 tiles' in out and '4/10' not in out
    assert 'tile/s' in out and 'ETA 00:03' in out  # 4 newly rendered/2s
    assert 'metatiles=8' in out


def test_raster_exact_tiles_and_resume_without_mapnik_native(monkeypatch, tmp_path, capsys):
    class FakeMap:
        def __init__(self, *args):
            self.srs = 'EPSG:3857'
            self.buffer_size = 0
        def resize(self, *args): pass
        def zoom_to_box(self, *args): pass

    class FakeImage:
        def __init__(self, *args): pass
        def view(self, *args): return self
        def tostring(self, *args): return b'\x89PNG\r\n\x1a\nmock-bytes'

    rendered = []
    fake_mapnik = types.SimpleNamespace(
        Map=FakeMap, Image=FakeImage,
        load_map=lambda *args: None,
        Box2d=lambda *args: tuple(args),
        render=lambda mp, im: rendered.append(1),
    )
    monkeypatch.setitem(sys.modules, 'mapnik', fake_mapnik)
    job = dict(
        bbox=(-1, -1, 1, 1), minzoom=2, maxzoom=2,
        metatile=1, buffer=64, name='test-progress',
        job_key='job-key', runtime=str(tmp_path / 'runtime'),
        mapfile=str(tmp_path / 'project.xml'),
        partial=str(tmp_path / 'render.mbtiles.partial'),
    )
    worker.render(job)
    expected = sum(worker.expected_counts(job).values())
    with sqlite3.connect(job['partial']) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT COUNT(*) FROM tiles').fetchone()[0] == expected
    first = len(rendered)
    assert first > 0
    assert f'COMPLETE TILES={expected}' in capsys.readouterr().out
    worker.render(job)
    assert len(rendered) == first, 'cached tiles rendered twice'
    out = capsys.readouterr().out
    assert 'already cached' in out and '100.0%' in out


def test_native_step_phase_detection_does_not_expose_command_arguments():
    assert runtime._step_label(['/usr/bin/osm2pgsql', '--password=secret']) == ('[IMPORT]', 'osm2pgsql')
    assert runtime._step_label(['/usr/bin/python3','/tmp/worker.py','secret.json']) == ('[RASTER]', 'Mapnik renderer')
    assert runtime._step_label(['/usr/bin/python3','/tmp/vector_worker.py','secret.json']) == ('[VECTOR]', 'MVT renderer')
    assert runtime._step_label(['/usr/bin/psql','-f','secret.sql']) == ('[POSTGIS]', 'SQL preparation')


def test_native_run_preserves_subprocess_exit_status_and_streams(monkeypatch):
    calls = []
    def execute(argv, **kw):
        calls.append((argv,kw))
    monkeypatch.setattr(runtime.subprocess, 'run', execute)
    runtime.run(['/usr/bin/osm2pgsql','--slim'], env={'SAFE':'1'}, cwd=Path('/tmp'))
    assert calls == [(['/usr/bin/osm2pgsql','--slim'], {
        'env': {'SAFE':'1'}, 'cwd':'/tmp', 'check':True,
    })]
    def failed(argv, **kw):
        raise subprocess.CalledProcessError(4,argv)
    monkeypatch.setattr(runtime.subprocess, 'run', failed)
    with pytest.raises(runtime.PipelineError, match='osm2pgsql'):
        runtime.run(['/usr/bin/osm2pgsql','--slim'])


def test_batch_progress_with_mocked_workers(monkeypatch, tmp_path, capsys):
    # Exercise the actual single SQLite writer without starting PostGIS.
    bbox = (-1, -1, 1, 1)
    z = 2
    tasks = [(z, [(1, 1), (2, 1)]), (z, [(1, 2), (2, 2)])]
    class FakePool:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def imap_unordered(self, fn, jobs, chunksize):
            for task in jobs:
                zz, coords = task
                yield [(zz,x,y,b'\x1f\x8bmock') for x,y in coords]
    monkeypatch.setattr(vector_batch.multiprocessing, 'get_context',
                        lambda name: types.SimpleNamespace(Pool=FakePool))
    monkeypatch.setattr(vector_batch, 'microbatches',
                        lambda *args, **kwargs: iter(tasks))
    out = tmp_path / 'batch.mbtiles'
    vector_batch.export_batch(tmp_path/'input.osm.pbf', out, bbox,z,z,processes=1,side=2)
    with sqlite3.connect(out) as db:
        count = db.execute('SELECT COUNT(*) FROM tiles').fetchone()[0]
        assert count == 4
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    line = capsys.readouterr().out
    assert '[VECTOR-BATCH] start' in line
    assert '[VECTOR-BATCH] 100.0%' in line
    assert 'tile/s' in line and 'ETA 00:00' in line
    assert '[VECTOR-BATCH] COMPLETE' in line


def test_wsl_stages_are_present_and_shell_is_parseable():
    script = ROOT / 'tools/run_wsl.sh'
    p = subprocess.run(['bash','-n',str(script)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    body = script.read_text(encoding='utf-8')
    assert all(f'stage {i} ' in body for i in range(1,7))
    assert 'verify-vector' in body
    assert 'cmp -s -- "$SRC" "$DEST.tmp"' in body
