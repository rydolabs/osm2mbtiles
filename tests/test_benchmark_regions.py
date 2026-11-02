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
    # Preserve Berlin-main as the primary three-engine test, while the larger
    # Berlin-stress dataset is the default for internal MVT speed regressions.
    assert config["default_internal_performance_region"] == "berlin-stress"
    stress = next(e for e in config["regions"] if e["id"] == "berlin-stress")
    baseline = json.loads((ROOT / stress["baseline_file"]).read_text(encoding="utf-8"))
    assert stress["pbf_url"] == baseline["input"]["url"]
    assert stress["pbf_sha256"] == baseline["input"]["sha256"]
    assert stress["pbf_size_bytes"] == baseline["input"]["size_bytes"]
    assert stress["bbox"] == baseline["input"]["bbox"]
    assert [stress["minzoom"], stress["maxzoom"]] == baseline["input"]["zoom"]
    assert stress["xyz_grid_cells"] == baseline["input"]["planned_xyz"]
    for entry in config["regions"]:
        assert entry["pbf_url"].startswith("https://download.geofabrik.de/")
        assert tile_count(entry["bbox"], entry["minzoom"], entry["maxzoom"]) == entry["xyz_grid_cells"]
        assert entry["measured"] is (entry["id"] == "berlin-stress")


def test_public_readme_is_global_and_seoul_entrypoint_is_manual():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Berlin" in readme and "Monaco" in readme and "Tilemaker" in readme
    assert "tests\\test-seoul.bat" in readme
    assert not (ROOT / "test-seoul.bat").exists()
    assert not (ROOT / "tools" / "test_seoul.ps1").exists()
    assert (ROOT / "tests" / "test-seoul.bat").exists()
    assert (ROOT / "tests" / "test_seoul.ps1").exists()
