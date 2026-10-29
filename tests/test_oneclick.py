"""Basic portability regression for the optional Windows WSL launcher.

These are not Windows/WSL end-to-end tests and do not claim that status.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_wsl_shell_syntax():
    result = subprocess.run(
        ['bash', '-n', str(ROOT / 'tools' / 'run_wsl.sh'), str(ROOT / 'tools' / 'download_geofabrik.sh')],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_wsl_shell_rejects_incomplete_arguments_before_side_effects():
    result = subprocess.run(
        ['bash', str(ROOT / 'tools' / 'run_wsl.sh')],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert 'usage: run_wsl.sh' in result.stderr


def test_windows_launcher_and_dependencies_present():
    bat = (ROOT / 'run.bat').read_text(encoding='utf-8')
    ps = (ROOT / 'tools' / 'run_windows.ps1').read_text(encoding='utf-8')
    shell = (ROOT / 'tools' / 'run_wsl.sh').read_text(encoding='utf-8')
    assert 'tools\\run_windows.ps1' in bat
    assert 'wsl.exe --exec bash' in ps
    assert 'Select' not in bat or 'run_windows.ps1' in bat
    assert '--runtime "$RUNTIME" --workdir "$WORKDIR"' in shell
    assert 'postgresql-$major-postgis-3' in shell
    assert 'tools/stage_from_system.py' in shell
    assert 'osm2mbtiles verify' in shell
    assert 'if [[ $# -lt 7 || $# -gt 13 ]]' in shell  # PowerShell passes 9 arguments