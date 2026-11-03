"""Release WSL2 progress harness: deterministic local tests, not a real WSL run."""
from __future__ import annotations

import gzip
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from PIL import Image, ImageDraw

from tools import progress_wsl2_verify as v

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "tools/cache_layout.sh"


def test_progress_log_accepts_real_counts_and_six_ordered_stages():
    log = "\n".join(
        [f"[STAGE] {i}/6 | stage {i} | elapsed=00:02" for i in range(1, 6)]
        + [
            "[IMPORT] osm2pgsql still running | elapsed=00:15 (no measured per-object progress)",
            "[RASTER]  20.0% | 2/10 tiles | 2.0 tile/s | ETA 00:04 | metatiles=1 z13",
            "[RASTER] 100.0% | 10/10 tiles | 4.0 tile/s | ETA 00:00 | metatiles=3 z16",
            "[RASTER] COMPLETE: 10/10 tiles",
            "[VECTOR]  50.0% | 5/10 tiles | nonempty=4 | 2.5 tile/s | ETA 00:02",
            "[VECTOR] 100.0% | 10/10 tiles | nonempty=8 | 3.3 tile/s | ETA 00:00",
            "[VECTOR] COMPLETE: 8 nonempty/10 planned tiles; 1234 bytes",
            "[STAGE] 6/6 | copying | elapsed=00:05",
            "[TIME] Windows end-to-end elapsed (WSL + verify + copy): 5.123s (00:00:05)",
        ]
    )
    result = v.analyze_progress(log)
    assert result["stages"] == [1, 2, 3, 4, 5, 6]
    assert result["raster"]["last_completed"] == 10
    assert result["vector"]["progress_rows"] == 2
    assert result["import_heartbeat_seen"] is True


@pytest.mark.parametrize("bad", [
    "[STAGE] 6/6 | copy",  # missing earlier phases
    "[RASTER] 100.0% | 10/10 tiles | 2.0 tile/s | ETA 00:00",
    "[RASTER]  20.0% | 2/10 tiles | 2.0 tile/s | ETA 00:04\n"
    "[RASTER]  10.0% | 1/10 tiles | 2.0 tile/s | ETA 00:04",
])
def test_release_progress_rejects_unsupported_pass_claims(bad):
    with pytest.raises(ValueError):
        v.analyze_progress(bad)


def create_mbtiles(path: Path, kind: str, *, broken=False):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE metadata(name TEXT,value TEXT)")
        db.execute("CREATE TABLE tiles(zoom_level INTEGER,tile_column INTEGER,"
                   "tile_row INTEGER,tile_data BLOB)")
        db.execute("INSERT INTO metadata VALUES('format',?)", (kind,))
        if kind == "png":
            image = Image.new("RGB", (256, 256), "white")
            draw = ImageDraw.Draw(image)
            for i in range(20):
                draw.rectangle((i*12, 0, i*12+10, 255),
                               fill=(i*11, 100+i*3, 220-i*7))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            blob = buffer.getvalue()
        else:
            ids = ["road", "water", "waterway", "place", "landuse", "railway",
                   "poi", "building", "transport", "boundary"]
            db.execute("INSERT INTO metadata VALUES('json',?)",
                       (json.dumps({"vector_layers":[{"id":n} for n in ids]}),))
            blob = gzip.compress(b"synthetic compressed sample" * 9)
            if broken:
                blob = b"notgzip"
        db.execute("INSERT INTO tiles VALUES(12,2191,1501,?)", (blob,))


def test_release_verifier_rejects_corrupted_mvt_and_saves_only_safe_stats(tmp_path):
    png = tmp_path/"raster.mbtiles"
    vector = tmp_path/"vector.mbtiles"
    create_mbtiles(png, "png")
    create_mbtiles(vector, "pbf")
    raster_stat, preview = v.inspect_mbtiles(png, "png")
    vector_stat, no_image = v.inspect_mbtiles(vector, "pbf")
    assert preview.size == (256, 256) and no_image is None
    assert raster_stat["tile_count"] == 1
    assert vector_stat["gzip_verified"]
    assert list(vector_stat) and "filename" in vector_stat
    with sqlite3.connect(vector) as db:
        db.execute("UPDATE tiles SET tile_data=?", (b"notgzip",))
    with pytest.raises(ValueError, match="non-gzip"):
        v.inspect_mbtiles(vector, "pbf")


def test_private_wsl_cache_preserves_normal_cache(tmp_path):
    home=tmp_path/"home"
    normal=home/".cache"/"osm2mbtiles"
    normal.mkdir(parents=True)
    (normal/"do-not-touch").write_text("original")
    env=dict(os.environ, HOME=str(home), OSM2MBTILES_TEST_RUN_ID="qa_20261104_4567")
    p=subprocess.run(["bash","-c",
        f'source "{CACHE}"; osm_cache_select; printf "%s" "$OSM_CACHE_BASE"'],
        capture_output=True,text=True,env=env)
    assert p.returncode==0, p.stderr
    assert "osm2mbtiles-release-tests/qa_20261104_4567" in p.stdout
    assert (normal/"do-not-touch").read_text()=="original"


def test_untrusted_cache_id_fails_closed(tmp_path):
    env=dict(os.environ,HOME=str(tmp_path),OSM2MBTILES_TEST_RUN_ID="../../sneaky")
    p=subprocess.run(["bash","-c",f'source "{CACHE}"; echo SHOULD_NOT_RUN'],
                     capture_output=True,text=True,env=env)
    assert p.returncode==4 and "SHOULD_NOT_RUN" not in p.stdout


def test_progress_harness_has_actual_windows_to_wsl_entrypoint():
    bat=(ROOT/"TEST-Progress-WSL2.bat").read_text(encoding="utf-8")
    ps=(ROOT/"tools/progress_wsl2_test.ps1").read_text(encoding="utf-8")
    assert "progress_wsl2_test.ps1" in bat
    assert "Invoke-Logged 'powershell.exe'" in ps
    assert "tools/run_windows.ps1" in ps
    assert "'12-16'" in ps
    assert "OSM2MBTILES_TEST_RUN_ID" in ps and "WSLENV" in ps
    assert "Compress-Archive" in ps and "Safe-Logs" in ps
    assert "ArchiveSelfTestFail" in ps
    assert (ROOT/"tools/progress_wsl2_verify.py").is_file()
