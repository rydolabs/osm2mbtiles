"""Verifier + private cache regressions. Synthetic data is NOT a WSL2 E2E."""
import gzip
import io
import json
from pathlib import Path
import os
import sqlite3
import subprocess
import sys

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "tools" / "release_wsl2_verify.py"
CACHE = ROOT / "tools" / "cache_layout.sh"


def fixture_mbtiles(path: Path, kind: str, valid: bool = True):
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE metadata(name TEXT, value TEXT)")
        db.execute("CREATE TABLE tiles(zoom_level INTEGER, tile_column INTEGER,"
                   " tile_row INTEGER, tile_data BLOB)")
        db.execute("INSERT INTO metadata VALUES('format',?)", (kind,))
        if kind == "png":
            image = Image.new("RGB", (256, 256), (245, 245, 245))
            draw = ImageDraw.Draw(image)
            for i in range(18):
                draw.rectangle((i * 14, 5, i * 14 + 10, 250),
                               fill=(12 * i, 120, 255 - 11 * i))
            buf = io.BytesIO()
            image.save(buf, "PNG")
            blob = buf.getvalue()
        else:
            layers = [{"id": key} for key in (
                "road", "water", "place", "landuse", "railway", "poi",
                "building", "waterway", "boundary", "transport"
            )]
            db.execute("INSERT INTO metadata VALUES('json',?)",
                       (json.dumps({"vector_layers": layers}),))
            blob = gzip.compress(b"real-mvt-like-protobuf-fixture" * 9) if valid else b"bad"
        db.execute("INSERT INTO tiles VALUES (12,2191,1501,?)", (blob,))
        db.commit()
    finally:
        db.close()


def test_release_wsl2_verifier_outputs_proof_and_png(tmp_path):
    raster = tmp_path / "both.raster.mbtiles"
    both_vector = tmp_path / "both.vector.mbtiles"
    standalone = tmp_path / "vector.vector.mbtiles"
    for file, kind in ((raster, "png"), (both_vector, "pbf"),
                       (standalone, "pbf")):
        fixture_mbtiles(file, kind)
    proof = tmp_path / "proof.json"
    preview = tmp_path / "preview.png"
    result = subprocess.run(
        [sys.executable, str(VERIFY), str(raster), str(both_vector),
         str(standalone), str(proof), str(preview)],
        capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(proof.read_text())
    assert report["passed"] is True
    assert [x["format"] for x in report["outputs"]] == ["png", "pbf", "pbf"]
    assert report["outputs"][0]["max_distinct_sampled_colors"] >= 10
    assert report["outputs"][1]["gzip_mvt_decodable"]
    assert Image.open(preview).size == (256, 256)
    assert "REAL_WINDOWS_WSL2_MBTILES_VERIFIED" in result.stdout


def test_release_wsl2_verifier_marks_corrupt_vector_failed(tmp_path):
    raster, vector, second = [tmp_path / name for name in ("a.db", "b.db", "c.db")]
    fixture_mbtiles(raster, "png")
    fixture_mbtiles(vector, "pbf", valid=False)
    fixture_mbtiles(second, "pbf")
    proof, preview = tmp_path / "bad.json", tmp_path / "bad.png"
    result = subprocess.run(
        [sys.executable, str(VERIFY), str(raster), str(vector),
         str(second), str(proof), str(preview)],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert json.loads(proof.read_text())["passed"] is False
    assert not preview.is_file()


def test_release_qa_cache_is_isolated_and_never_migrates(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), OSM2MBTILES_TEST_RUN_ID="qa_test123")
    result = subprocess.run(
        ["bash", "-c",
         f'source "{CACHE}"; osm_cache_select; printf "%s" "$OSM_CACHE_BASE"'],
        env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == str(tmp_path / ".cache/osm2mbtiles-release-tests/qa_test123")
    assert not (tmp_path / ".cache/osm2mbtiles").exists()


def test_release_qa_cache_rejects_untrusted_path_components(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), OSM2MBTILES_TEST_RUN_ID="../../bad")
    result = subprocess.run(
        ["bash", "-c", f'source "{CACHE}"; echo SHOULD_NOT_RUN'],
        env=env, capture_output=True, text=True
    )
    assert result.returncode == 4
    assert "SHOULD_NOT_RUN" not in result.stdout


def test_one_click_launcher_is_separate_from_normal_converter():
    bat = (ROOT / "TEST-Windows-WSL2.bat").read_text(encoding="utf-8")
    ps = (ROOT / "tools/release_wsl2_test.ps1").read_text(encoding="utf-8")
    assert 'tools\\release_wsl2_test.ps1' in bat
    assert 'release-test-results' in ps
    assert "OSM2MBTILES_TEST_RUN_ID" in ps
    assert "OSM2MBTILES_FORMAT = 'both'" in ps
    assert "OSM2MBTILES_FORMAT = 'vector'" in ps
    assert "Compress-Archive" in ps
    assert "ArchiveSelfTest" in ps
