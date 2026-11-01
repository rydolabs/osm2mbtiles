"""Pure correctness gates for isolated importer benchmark."""
import importlib.util
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "benchmarks/kanto_import_factorial.py"
spec = importlib.util.spec_from_file_location("kanto_import_factorial", SRC)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def sig(n=2, features="same", raw="a"):
    return {"metadata":{"format":"pbf"},
            "tiles":{"12/1/2":{"n":n,"features_sha":features,"gzip_sha":raw}}}


def test_gzip_mismatch_does_not_hide_semantic_parity():
    result = audit.compare(sig(), sig(raw="different"))
    assert result["semantic_equal"]
    assert result["gzip_mismatched"] == 1
    assert result["decoded_mismatched"] == 0


def test_feature_count_geometry_or_attributes_mismatch_fails():
    assert not audit.compare(sig(), sig(n=1))["semantic_equal"]
    assert not audit.compare(sig(), sig(features="different"))["semantic_equal"]


def test_xyz_and_metadata_mismatch_fails():
    assert not audit.compare(sig(), {"metadata":{"format":"pbf"},"tiles":{}})["semantic_equal"]
    bad=sig()
    bad["metadata"]["format"]="png"
    assert not audit.compare(sig(), bad)["semantic_equal"]


def test_one_factor_profiles():
    assert audit.PROFILES["safe"] == (1, 256)
    assert audit.PROFILES["cache384"] == (1, 384)
    assert audit.PROFILES["workers2"] == (2, 256)
    assert audit.PROFILES["both"] == (2, 384)
