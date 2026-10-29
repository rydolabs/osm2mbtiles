"""Tile-based progress reporting and cache cleanup safety (no native Mapnik required)."""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'src' / 'osm2mbtiles' / 'worker.py'
CLEANUP = ROOT / 'tools' / 'cache_cleanup.sh'


def worker_module():
    spec = importlib.util.spec_from_file_location('worker_progress_under_test', WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_exact_metatile_totals_match_scheduler():
    m = worker_module()
    for bbox, lo, hi, meta in [((-180, -85, 180, 85), 0, 3, 8),
                               ((126.8, 34, 129.5, 38), 0, 9, 8),
                               ((126, 35, 128, 38), 5, 14, 2)]:
        job = dict(bbox=bbox, minzoom=lo, maxzoom=hi, metatile=meta)
        expected_tiles, expected_metas = m.expected_counts(job)
        tasks = list(m.tasks(job))
        assert expected_metas == len(tasks)
        assert expected_tiles == sum((t[6] - t[5] + 1) * (t[8] - t[7] + 1) for t in tasks)


def test_progress_reports_full_tile_percentage_eta_and_resume():
    m = worker_module()
    stdout = io.StringIO()
    now = [0.0]
    meter = m.ProgressBar(200, 10, 50, stream=stdout, clock=lambda: now[0])
    now[0] = 5
    meter.update(50)
    now[0] = 10
    meter.update(50)
    now[0] = 15
    meter.update(50)
    meter.finish()
    out = stdout.getvalue()
    assert 'Total: 200 PNG tiles | 10 metatiles | preexisting: 50 tiles' in out
    assert '25.00%' in out
    assert '50.00%' in out
    assert '75.00%' in out
    assert '100.00%' in out
    assert 'PNG 200/200' in out
    assert 'ETA 00:00' in out
    assert out.count('%') <= 7  # coarse checkpoints, not a log line per tile


def test_tty_progress_reuses_same_line():
    m = worker_module()
    class Terminal(io.StringIO):
        def isatty(self): return True
    t = Terminal()
    now = [0]
    bar = m.ProgressBar(4, 2, 0, stream=t, clock=lambda: now[0])
    now[0] = 1
    bar.update(2)
    now[0] = 2
    bar.update(2)
    bar.finish()
    output = t.getvalue()
    assert '\r[' in output
    assert '50.00%' in output
    assert '100.00%' in output
    assert output.endswith('\n')


def cache_tree(tmp_path):
    home = tmp_path / 'home'
    root = home / '.cache' / 'osm2mbtiles'
    for n in ('downloads', 'runtime', 'native', 'style-v6.0.0', 'exports', 'work/pgdata'):
        (root / n).mkdir(parents=True)
    (root / 'downloads/south-korea-latest.osm.pbf').write_bytes(b'PBF')
    (root / 'native/osm2pgsql').write_bytes(b'BIN')
    (root / 'work/.osm2mbtiles-workspace').touch()
    (root / 'work/pgdata/PG_VERSION').write_text('16')
    (root / 'work/import-state.json').write_text('{}')
    (root / 'exports/south-korea-latest.mbtiles').write_bytes(b'MBTILES')
    return home, root


def cleanup(tmp_path, mode, output='south-korea-latest.mbtiles', copied=True, trusted=False):
    env = dict(os.environ, HOME=str(tmp_path / 'home'))
    argv = ['bash', str(CLEANUP), mode, output]
    if copied and output == 'south-korea-latest.mbtiles':
        verified = tmp_path / 'windows' / output
        verified.parent.mkdir(parents=True, exist_ok=True)
        if not verified.exists():
            verified.write_bytes(b'MBTILES')
        argv += ['--runner-locked' if trusted else '', str(verified)]
    return subprocess.run(argv, env=env, text=True, capture_output=True, timeout=20)


def test_compact_removes_only_redundant_wsl_output(tmp_path):
    home, base = cache_tree(tmp_path)
    result = cleanup(tmp_path, 'compact')
    assert result.returncode == 0, result.stderr
    assert not (base / 'exports/south-korea-latest.mbtiles').exists()
    assert (base / 'work/pgdata/PG_VERSION').exists()
    assert (base / 'work/import-state.json').exists()
    assert (base / 'downloads/south-korea-latest.osm.pbf').exists()


def test_prune_removes_private_db_and_import_state_only(tmp_path):
    home, base = cache_tree(tmp_path)
    result = cleanup(tmp_path, 'prune')
    assert result.returncode == 0, result.stderr
    assert not (base / 'work/pgdata').exists()
    assert not (base / 'work/import-state.json').exists()
    assert not (base / 'exports/south-korea-latest.mbtiles').exists()
    assert (base / 'downloads/south-korea-latest.osm.pbf').exists()
    assert (base / 'native/osm2pgsql').exists()
    assert (base / 'style-v6.0.0').is_dir()


def test_prune_refuses_running_postgres_and_unsafe_export(tmp_path):
    home, base = cache_tree(tmp_path)
    (base / 'work/pgdata/postmaster.pid').write_text('1111')
    result = cleanup(tmp_path, 'prune')
    assert result.returncode == 4
    assert (base / 'work/pgdata/PG_VERSION').exists()
    assert (base / 'work/import-state.json').exists()
    home2, base2 = cache_tree(tmp_path / 'other')
    env = dict(os.environ, HOME=str(home2))
    res = subprocess.run(['bash', str(CLEANUP), 'compact', '../unsafe.mbtiles'],
                         env=env, text=True, capture_output=True)
    assert res.returncode == 3
    assert (base2 / 'exports/south-korea-latest.mbtiles').exists()


def test_keep_and_status_without_verified_export_are_nondestructive(tmp_path):
    home, base = cache_tree(tmp_path)
    for mode in ('status', 'keep'):
        res = cleanup(tmp_path, mode, output='', copied=False)
        assert res.returncode == 0, res.stderr
    assert (base / 'exports/south-korea-latest.mbtiles').exists()
    assert (base / 'work/pgdata/PG_VERSION').exists()


def test_wsl_cleanup_occurs_only_after_verified_windows_output():
    wsl = (ROOT / 'tools/run_wsl.sh').read_text()
    windows = (ROOT / 'tools/run_windows.ps1').read_text()
    pos = wsl.index('python3 -m osm2mbtiles verify')
    pos_check = wsl.index('cmp -s -- "$SRC" "$DEST.tmp"')
    pos_copy = wsl.index('mv -f -- "$DEST.tmp" "$DEST"')
    pos_cleanup = wsl.index('cache_cleanup.sh')
    assert pos < pos_check < pos_copy < pos_cleanup
    assert 'compact' in windows and 'OSM2MBTILES_CLEANUP' in windows
    assert '"$mode" "$name" --runner-locked' in wsl
    assert 'verify-vector' in wsl and 'format' in windows.lower()


def test_cleanup_bat_and_powershell_entry_points_present():
    bat = (ROOT / 'cleanup.bat').read_text(encoding='utf-8')
    ps = (ROOT / 'tools/cache_windows.ps1').read_text(encoding='utf-8')
    assert 'cache_windows.ps1' in bat
    assert 'Select [1/2/3]' in bat
    assert not (ROOT / 'cache.bat').exists()
    assert 'Confirm DB cleanup' in bat
    assert 'wsl.exe --exec bash' in ps
    assert "ValidateSet('status','prune')" in ps


def test_keep_still_removes_verified_export_after_success(tmp_path):
    home, base = cache_tree(tmp_path)
    result = cleanup(tmp_path, 'keep')
    assert result.returncode == 0, result.stderr
    assert not (base / 'exports/south-korea-latest.mbtiles').exists()
    assert (base / 'work/pgdata/PG_VERSION').exists()
    assert (base / 'downloads/south-korea-latest.osm.pbf').exists()


def test_output_not_removed_if_windows_copy_differs(tmp_path):
    home, base = cache_tree(tmp_path)
    verified = tmp_path / 'windows' / 'south-korea-latest.mbtiles'
    verified.parent.mkdir(parents=True)
    verified.write_bytes(b'CORRUPTED OR DIFFERENT')
    result = cleanup(tmp_path, 'compact')
    assert result.returncode == 6
    assert (base / 'exports/south-korea-latest.mbtiles').read_bytes() == b'MBTILES'
    assert (base / 'work/pgdata/PG_VERSION').exists()


def test_output_not_removed_without_copy_evidence(tmp_path):
    home, base = cache_tree(tmp_path)
    result = cleanup(tmp_path, 'compact', copied=False)
    assert result.returncode == 6
    assert (base / 'exports/south-korea-latest.mbtiles').exists()


def test_old_wsl_cache_migrates_without_losing_data(tmp_path):
    home, new_cache = cache_tree(tmp_path)
    old = home / '.cache' / 'osm2mbtiles-oneclick'
    new_cache.rename(old)
    env = dict(os.environ, HOME=str(home))
    result = subprocess.run(['bash', str(CLEANUP), 'status'],
                            env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert new_cache.exists() and not old.exists()
    assert (new_cache / 'downloads/south-korea-latest.osm.pbf').read_bytes() == b'PBF'
    assert (new_cache / 'work/pgdata/PG_VERSION').read_text() == '16'
    assert 'Migrated existing WSL cache' in result.stderr


def test_both_cache_directories_are_kept_unchanged(tmp_path):
    home, new = cache_tree(tmp_path)
    old = home / '.cache' / 'osm2mbtiles-oneclick'
    old.mkdir()
    (old / 'old-only').write_bytes(b'old')
    res = cleanup(tmp_path, 'status', output='', copied=False)
    assert res.returncode == 0, res.stderr
    assert (old / 'old-only').read_bytes() == b'old'
    assert (new / 'downloads/south-korea-latest.osm.pbf').exists()
    assert 'Both old and new' in res.stderr


def test_shared_cache_layout_and_single_sqlite_writer_documented():
    root = ROOT
    assert (root / 'tools/cache_layout.sh').exists()
    wsl = (root / 'tools/run_wsl.sh').read_text(encoding='utf-8')
    cleanup_sh = (root / 'tools/cache_cleanup.sh').read_text(encoding='utf-8')
    worker = (root / 'src/osm2mbtiles/worker.py').read_text(encoding='utf-8')
    assert 'source "$PROJECT/tools/cache_layout.sh"' in wsl
    assert 'source "$(dirname "${BASH_SOURCE[0]}")/cache_layout.sh"' in cleanup_sh
    assert 'BASE="$OSM_CACHE_BASE"' in wsl
    assert 'BASE="$OSM_CACHE_BASE"' in cleanup_sh
    assert 'with sqlite3.connect(path) as db:' in worker
    assert 'db.executemany(' in worker
    assert 'sqlite3.connect(' not in worker.split('def render_metatile(', 1)[-1].split('def render(job):', 1)[0]


def test_manual_prune_after_auto_compact_still_removes_database(tmp_path):
    home, base = cache_tree(tmp_path)
    assert cleanup(tmp_path, 'compact').returncode == 0
    assert not (base / 'exports/south-korea-latest.mbtiles').exists()
    result = cleanup(tmp_path, 'prune')
    assert result.returncode == 0, result.stderr
    assert not (base / 'work/pgdata').exists()
    assert (base / 'downloads/south-korea-latest.osm.pbf').exists()


def test_manual_prune_preserves_unknown_partial_if_export_missing(tmp_path):
    home, base = cache_tree(tmp_path)
    (base / 'exports/south-korea-latest.mbtiles').unlink()
    (base / 'exports/south-korea-latest.mbtiles.partial').write_bytes(b'UNVERIFIED PARTIAL')
    result = cleanup(tmp_path, 'prune')
    assert result.returncode == 0, result.stderr
    assert (base / 'exports/south-korea-latest.mbtiles.partial').read_bytes() == b'UNVERIFIED PARTIAL'


def test_manual_cache_power_shell_forwards_nonempty_marker():
    ps = (ROOT / 'tools/cache_windows.ps1').read_text(encoding='utf-8')
    assert '$Action $exportName --manual $verifiedWindowsPath' in ps