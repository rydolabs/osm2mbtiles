"""Validate public international benchmark area definitions and launcher layout."""
import json
from pathlib import Path

from osm2mbtiles.tiles import tile_count


ROOT = Path(__file__).resolve().parents[1]


def test_benchmark_regions_have_reproducible_calculated_xyz_coverage():
    config = json.loads((ROOT / "benchmarks" / "regions.json").read_text())
    assert config["version"] == 1
    assert {entry["id"] for entry in config["regions"]} >= {
        "berlin-main", "berlin-center", "monaco-smoke"
    }
    assert len([entry for entry in config["regions"] if entry["role"] == "primary"]) == 1
    for entry in config["regions"]:
        assert entry["pbf_url"].startswith("https://download.geofabrik.de/")
        assert tile_count(entry["bbox"], entry["minzoom"], entry["maxzoom"]) == entry["xyz_grid_cells"]
        assert entry["measured"] is False


def test_public_readme_is_global_and_seoul_entrypoint_is_manual():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Berlin" in readme and "Monaco" in readme and "Tilemaker" in readme
    assert "tests\\test-seoul.bat" in readme
    assert not (ROOT / "test-seoul.bat").exists()
    assert not (ROOT / "tools" / "test_seoul.ps1").exists()
    assert (ROOT / "tests" / "test-seoul.bat").exists()
    assert (ROOT / "tests" / "test_seoul.ps1").exists()
