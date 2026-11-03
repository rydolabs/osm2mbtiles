"""Regressions for the README's honest positioning and real-data imagery."""
from pathlib import Path
import json
import struct

ROOT = Path(__file__).resolve().parents[1]


def test_readme_puts_evidence_and_selection_before_technical_catalog():
    eng = (ROOT / "README.md").read_text(encoding="utf-8")
    kor = (ROOT / "README.ko.md").read_text(encoding="utf-8")
    assert eng.index("## See a real OSM change") < eng.index("## Quick start")
    assert eng.index("## Quick start") < eng.index("## When should you choose")
    assert eng.index("## When should you choose") < eng.index("## What is inside")
    assert "docs/assets/berlin-incremental-focus.png" in eng
    assert "docs/assets/berlin-incremental-focus.png" in kor
    assert "docs/assets/readme-preview-evidence.json" in eng
    assert "docs/VECTOR-TILE-SCHEMA.md" in eng
    assert "docs/VECTOR-TILE-SCHEMA.md" in kor
    assert "https://github.com/onthegomap/planetiler" in eng
    assert "https://github.com/systemed/tilemaker" in eng
    assert "not a proven speed win" in eng
    assert "does not manage replication sequences" in eng


def test_focus_image_is_real_proof_with_expected_dimensions():
    asset = ROOT / "docs/assets/berlin-incremental-focus.png"
    data = asset.read_bytes()
    assert data.startswith(bytes.fromhex("89504e470d0a1a0a"))
    assert struct.unpack(">II", data[16:24]) == (1220, 780)
    evidence = json.loads(
        (ROOT / "docs/assets/readme-preview-evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["requested_tile_xyz"] == [14, 8802, 5373]
    assert evidence["proven_new_poi_count"] == 1
    assert evidence["updated_features"]["poi"] - evidence["original_features"]["poi"] == 1
    assert evidence["original_gzip_sha256"] != evidence["updated_gzip_sha256"]
    assert (ROOT / "tools/readme_focus_preview.py").is_file()
