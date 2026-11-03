"""Prevent the public vector-tile reference from drifting from PostGIS SQL."""
from __future__ import annotations

import json
from pathlib import Path
import re

from osm2mbtiles import vector

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "VECTOR-TILE-SCHEMA.md"


def _section(content, start, end):
    assert start in content, start
    assert end in content, end
    return content.split(start, 1)[1].split(end, 1)[0]


def _fields(section):
    match = re.search(r"```text\s*(.*?)\s*```", section, flags=re.S)
    assert match, "Missing documented field list"
    names = tuple(part.strip() for part in match.group(1).split(",") if part.strip())
    assert len(names) == len(set(names)), "Duplicated documentation key"
    return names


def test_exact_public_field_allowlists_and_z13_transition():
    content = DOC.read_text(encoding="utf-8")
    low = _fields(_section(content, "<!-- schema-fields-low -->", "<!-- /schema-fields-low -->"))
    high = _fields(_section(content, "<!-- schema-fields-high -->", "<!-- /schema-fields-high -->"))
    assert low == vector.LOW_FIELDS
    assert high == vector.ALL_FIELDS
    assert len(low) == 20
    assert len(high) == 74
    assert "z0–12" in content
    assert "z13+" in content


def test_all_layer_names_minimum_zooms_and_class_priority():
    content = DOC.read_text(encoding="utf-8")
    table = _section(content, "## Layer catalog", "### Zoom-dependent feature filtering")
    rows = {}
    for line in table.splitlines():
        if not re.match(r"^\| `[a-z_]+` \|", line):
            continue
        columns = [p.strip() for p in line.split("|")]
        assert len(columns) == 7, line
        layer = columns[1].strip("`")
        assert layer not in rows, f"Duplicate documented layer: {layer}"
        z = int(columns[2])
        class_keys = re.findall(r"`([^`]+)`", columns[5])
        assert class_keys, f"Missing class key priority: {layer}"
        rows[layer] = (z, class_keys)
    assert list(rows) == list(vector.LAYER_NAMES)
    minimums = {
        layer: min(z for n, z, *_ in vector.LAYERS if n == layer)
        for layer in vector.LAYER_NAMES
    }
    assert {name: z for name, (z, _) in rows.items()} == minimums
    for layer, (_, keys) in rows.items():
        table_id = next(key for n, _, _, key, _ in vector.LAYERS if n == layer)
        sql = vector._properties("t", 15, layer, table_id, projected=True)
        match = re.search(r"'class', COALESCE\(([^)]*)\)", sql)
        assert match, layer
        actual = re.findall(r"t\.tags->>'([^']+)'", match.group(1))
        assert keys == actual, f"{layer}: documented class tag priorities drifted"


def test_qualified_zoom_filters_are_documented():
    content = DOC.read_text(encoding="utf-8")
    filters = _section(content, "### Zoom-dependent feature filtering", "## Feature attributes")
    for layer in vector.ZOOM_FILTER:
        assert f"| `{layer}` |" in filters, f"Missing zoom restrictions for {layer}"


def test_mvt_metadata_field_declarations_match_allowlist():
    info = json.loads(vector.metadata("test", (13, 52, 14, 53), 0, 16)["json"])
    ids = [d["id"] for d in info["vector_layers"]]
    assert ids == list(vector.LAYER_NAMES)
    expected = set(vector.ALL_FIELDS) | {"class", "osm_id"}
    assert all(set(layer["fields"]) == expected for layer in info["vector_layers"])
    assert all(set(layer["fields"].values()) == {"String"} for layer in info["vector_layers"])


def test_readme_entry_points_and_real_preview_evidence():
    eng = (ROOT / "README.md").read_text(encoding="utf-8")
    kor = (ROOT / "README.ko.md").read_text(encoding="utf-8")
    for readme in (eng, kor):
        assert "docs/VECTOR-TILE-SCHEMA.md" in readme
        for name in vector.LAYER_NAMES:
            assert f"`{name}`" in readme, f"Missing {name} from README"
    content = DOC.read_text(encoding="utf-8")
    assert "source-layer" in content
    assert "(1 << z) - 1 - y" in content
    assert "osm_id" in content
    assert "MapLibre" in content
    assert "mapbox_vector_tile.decode" in content
    evidence = json.loads(
        (ROOT / "docs" / "assets" / "readme-preview-evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["proven_new_poi_count"] == 1
    assert evidence["updated_features"]["poi"] == evidence["original_features"]["poi"] + 1
    assert evidence["original_gzip_sha256"] != evidence["updated_gzip_sha256"]
    for file in ("berlin-vector-preview.png", "berlin-incremental-before-after.png"):
        assert (ROOT / "docs" / "assets" / file).read_bytes().startswith(
            bytes.fromhex("89504e470d0a1a0a")
        )
