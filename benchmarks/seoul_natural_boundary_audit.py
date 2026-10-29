#!/usr/bin/env python3
"""Explain water/landcover/boundary count differences for the same Seoul XYZ.

Output taxonomy, source-ID reuse and geometry overlap.  It does NOT claim
strict OSM-object equivalence when separate engines emit ways vs relations.
"""
import collections
import gzip
import json
import sqlite3
import sys
from pathlib import Path

import mapbox_vector_tile
from shapely.geometry import shape
from shapely.ops import unary_union

Z, X, Y = 14, 13970, 6344
LAYERS = ("water", "landcover", "boundary")


def read_tile(path):
    with sqlite3.connect(path) as db:
        blob = db.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (Z, X, (1 << Z) - 1 - Y),
        ).fetchone()
    assert blob and blob[0], "No target tile: " + str(path)
    tile = blob[0]
    return mapbox_vector_tile.decode(
        gzip.decompress(tile) if tile[:2] == b"\x1f\x8b" else tile
    )


def histogram(features, key):
    return dict(sorted(collections.Counter(
        str(f.get("properties", {}).get(key, "<missing>")) for f in features
    ).items(), key=lambda item: (-item[1], item[0])))


def ids(features, origin):
    if origin == "planetiler":
        return [str(f["id"]) for f in features if f.get("id") is not None]
    return [str(f.get("properties", {}).get("osm_id")) for f in features
            if f.get("properties", {}).get("osm_id") is not None]


def polygons(features):
    geoms = []
    for f in features:
        try:
            g = shape(f["geometry"])
            if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty:
                geoms.append(g)
        except (TypeError, ValueError):
            pass
    return geoms


def lines(features):
    geoms = []
    for f in features:
        try:
            g = shape(f["geometry"])
            if g.geom_type in ("LineString", "MultiLineString") and not g.is_empty:
                geoms.append(g)
        except (TypeError, ValueError):
            pass
    return geoms


def geometry_overlap(a, b, kind):
    a = lines(a) if kind == "line" else polygons(a)
    b = lines(b) if kind == "line" else polygons(b)
    if not a or not b:
        return {"error": "Missing comparable geometries"}
    ua = unary_union(a)
    ub = unary_union(b)
    if kind == "line":
        # A and B may have different segmentation and slight simplification.
        # We compare aggregate clipped line length near the other output, not IDs.
        # MVT coordinate tolerance is in 4096-unit tile space.
        ratios = {}
        for tol in (2, 8, 24):
            ratios[str(tol)] = {
                "osm2mbtiles_near_planetiler": round(
                    ua.intersection(ub.buffer(tol)).length / ua.length, 5
                ) if ua.length else None,
                "planetiler_near_osm2mbtiles": round(
                    ub.intersection(ua.buffer(tol)).length / ub.length, 5
                ) if ub.length else None,
            }
        return {"osm2mbtiles_total_length": round(ua.length, 1),
                "planetiler_total_length": round(ub.length, 1),
                "coverage_by_tolerance_mvt_units": ratios}
    both = ua.intersection(ub).area
    return {
        "osm2mbtiles_union_area": round(ua.area, 1),
        "planetiler_union_area": round(ub.area, 1),
        "intersection_area": round(both, 1),
        "osm2mbtiles_area_covered_by_planetiler":
            round(both / ua.area, 5) if ua.area else None,
        "planetiler_area_covered_by_osm2mbtiles":
            round(both / ub.area, 5) if ub.area else None,
    }


def main(work):
    tiles = {eng: read_tile(work / (eng + ".mbtiles"))
             for eng in ("osm2mbtiles", "planetiler")}
    output = {
        "XYZ": [Z, X, Y],
        "profiles": {
            "osm2mbtiles": "vector.py layer SQL filters + flex import",
            "planetiler": "seoul-planetiler.yml benchmark custom YAML; NOT general OpenMapTiles",
        },
        "layers": {},
    }
    for layer in LAYERS:
        left = tiles["osm2mbtiles"].get(layer, {}).get("features", [])
        right = tiles["planetiler"].get(layer, {}).get("features", [])
        pids = ids(right, "planetiler")
        counts = collections.Counter(pids)
        output["layers"][layer] = {
            "osm2mbtiles": {
                "features": len(left),
                "class_histogram": histogram(left, "class"),
                "natural_histogram": histogram(left, "natural"),
                "boundary_histogram": histogram(left, "boundary"),
                "geometry_types": histogram([
                    {"properties": {"gtype": f["geometry"]["type"]}} for f in left
                ], "gtype"),
                "unique_ids_in_mvt_properties": len(set(ids(left, "osm2mbtiles"))),
            },
            "planetiler": {
                "features": len(right),
                "class_histogram": histogram(right, "class"),
                "natural_histogram": histogram(right, "natural"),
                "boundary_histogram": histogram(right, "boundary"),
                "geometry_types": histogram([
                    {"properties": {"gtype": f["geometry"]["type"]}} for f in right
                ], "gtype"),
                "unique_mvt_feature_ids": len(set(pids)),
                "duplicate_id_count": len([k for k, n in counts.items() if n > 1]),
                "features_with_repeated_id": sum(n for n in counts.values() if n > 1),
            },
            "geometry": geometry_overlap(left, right, "line" if layer == "boundary" else "polygon"),
        }
    # For administrative vs legal boundary way segments, quantify linework
    # independently; aggregated feature counts mix different classifications.
    osm_boundary = tiles["osm2mbtiles"].get("boundary", {}).get("features", [])
    planet_boundary = tiles["planetiler"].get("boundary", {}).get("features", [])
    categories = sorted(set(
        str(f.get("properties", {}).get("boundary", "<missing>"))
        for f in planet_boundary
    ))
    output["layers"]["boundary"]["geometry_by_planetiler_class"] = {
        category: geometry_overlap(
            osm_boundary,
            [f for f in planet_boundary
             if str(f.get("properties", {}).get("boundary", "<missing>")) == category],
            "line",
        )
        for category in categories
    }
    pw = tiles["planetiler"].get("water", {}).get("features", [])
    pl = tiles["planetiler"].get("landcover", {}).get("features", [])
    pwids = set(ids(pw, "planetiler"))
    plids = set(ids(pl, "planetiler"))
    natural_water = [f for f in pw if f.get("properties", {}).get("natural") == "water"]
    other_natural = [f for f in pw if f.get("properties", {}).get("natural") not in (None, "water")]
    output["planetiler_cross_layer"] = {
        "water_and_landcover_shared_source_ids": len(pwids & plids),
        "water_distinct_source_ids": len(pwids),
        "landcover_distinct_source_ids": len(plids),
        "water_features_natural_water": len(natural_water),
        "water_features_with_non_water_natural": len(other_natural),
        "water_features_without_natural_tag": len(pw) - len(natural_water) - len(other_natural),
        "water_and_landcover_shared_examples": sorted(pwids & plids)[:20],
    }
    (work / "natural-boundary-audit.json").write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    lines = [
        "# Same Seoul tile: why water/landcover/boundary differ",
        "",
        "Real OSM PBF and exactly XYZ 14/13970/6344.",
        "",
        "| Layer | osm2mbtiles | Planetiler custom | Planetiler unique IDs | Duplicated Planetiler IDs |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in LAYERS:
        a = output["layers"][name]
        lines.append(
            f"| {name} | {a['osm2mbtiles']['features']} | {a['planetiler']['features']} | "
            f"{a['planetiler']['unique_mvt_feature_ids']} | {a['planetiler']['duplicate_id_count']} |"
        )
        lines.extend(["", f"### {name} class distribution", "",
                      "**osm2mbtiles**: " + json.dumps(a["osm2mbtiles"]["class_histogram"], ensure_ascii=False),
                      "",
                      "**Planetiler**: " + json.dumps(a["planetiler"]["class_histogram"], ensure_ascii=False),
                      ""])
    lines += [
        "### Planetiler legal vs administrative boundary geometries",
        "",
        json.dumps(
            output["layers"]["boundary"]["geometry_by_planetiler_class"],
            ensure_ascii=False, indent=2
        ),
        "",
    ]
    lines += [
        "### Planetiler shared IDs across water and landcover", "",
        json.dumps(output["planetiler_cross_layer"], ensure_ascii=False, indent=2),
        "", "## Boundary linework overlap (MVT tile-coordinate units)", "",
        json.dumps(output["layers"]["boundary"]["geometry"], ensure_ascii=False, indent=2),
        "", "**Caution:** The two styles emit different geometries (relation MultiLineStrings vs way LineStrings); "
        "feature counts and OSM IDs alone do not establish source completeness.",
    ]
    (work / "natural-boundary-audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("NATURAL_BOUNDARY_SAME_TILE_AUDIT_PASS", json.dumps({
        "counts": {k: [v["osm2mbtiles"]["features"], v["planetiler"]["features"]]
                   for k, v in output["layers"].items()},
        "planetiler_water_natural_water":
            output["planetiler_cross_layer"]["water_features_natural_water"],
        "planetiler_water_non_water_natural":
            output["planetiler_cross_layer"]["water_features_with_non_water_natural"],
        "cross_layer_shared":
            output["planetiler_cross_layer"]["water_and_landcover_shared_source_ids"],
        "boundary_line_coverage":
            output["layers"]["boundary"]["geometry"]["coverage_by_tolerance_mvt_units"]["8"],
        "admin_boundary_line_coverage":
            output["layers"]["boundary"]["geometry_by_planetiler_class"].get(
                "administrative", {}
            ).get("coverage_by_tolerance_mvt_units", {}).get("8"),
    }), flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
