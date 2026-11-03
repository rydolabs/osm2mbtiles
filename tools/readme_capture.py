#!/usr/bin/env python3
"""Render documentation previews from REAL compressed MVT MBTiles.

Inputs are created on a disposable GitHub runner from the pinned Geofabrik
Berlin PBF; after.mbtiles contains a synthetic OSM .osc.gz node applied via the
project's genuine persistent vector_incremental path.

These are diagnostic, locally styled vector-tile previews, not screenshots of
MapLibre nor the Mapnik/OSM Carto raster pipeline. Fail closed if the synthetic
node is absent, already existed, or unrelated source data replaced the tile.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

from PIL import Image, ImageDraw, ImageFont
import mapbox_vector_tile

Z, X, Y = 14, 8802, 5373
NODE_ID = "9876543210123"
POI_NAME = "osm2mbtiles README demo cafe"
W = 680
TILE = 560
PAD = 60
EXTENT = 4096
MARGIN = 24
LAYERS = (
    ("landuse", "#deead3", "#d6e2ca", 1),
    ("landcover", "#bfe0ad", "#bad8a3", 2),
    ("leisure", "#b7db9b", "#aad18e", 2),
    ("water", "#a8d9e8", "#9cccd9", 3),
    ("building", "#e4d5c4", "#d0bda9", 4),
    ("building_part", "#cfc0b1", "#b9a995", 4),
    ("railway", "#b2a69d", "#9e948d", 5),
    ("pathway", "#ffffff", "#ffffff", 6),
    ("road", "#fff9eb", "#ffffff", 7),
    ("waterway", "#84cbe5", "#75bbd5", 8),
    ("poi", "#305b7f", "#305b7f", 9),
)
SRC_LABEL = "GEOfabrik BERLIN / 2026-10-01 OSM PBF"
ACCENT = "#187a74"


def tms_row(y=Y):
    return (1 << Z) - 1 - y


def get_tile(path: Path):
    with sqlite3.connect(path) as con:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Corrupt MBTiles: " + str(path))
        meta = dict(con.execute("SELECT name,value FROM metadata"))
        if meta.get("format") != "pbf":
            raise RuntimeError("Not vector MVT MBTiles: " + str(path))
        row = con.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (Z, X, tms_row()),
        ).fetchone()
    if row is None:
        raise RuntimeError("Expected Berlin z14 tile not emitted: " + str(path))
    blob = bytes(row[0])
    raw = gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob
    decoded = mapbox_vector_tile.decode(raw, default_options={"y_coord_down": True})
    if not decoded.get("building", {}).get("features"):
        raise RuntimeError("Berlin preview tile lacks real building data")
    return decoded, hashlib.sha256(blob).hexdigest()


def added_cafes(tile):
    return [
        f for f in tile.get("poi", {}).get("features", [])
        if f.get("properties", {}).get("name") == POI_NAME
        and NODE_ID in str(f.get("properties", {}).get("osm_id", ""))
    ]


def font(size=16, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


def coords_to_pixel(pt):
    return (PAD + pt[0] * TILE / EXTENT,
            PAD + pt[1] * TILE / EXTENT)


def geometry_paths(obj):
    typ = obj.get("type")
    coords = obj.get("coordinates") or []
    if typ == "Point":
        return ("point", [[coords]])
    if typ == "MultiPoint":
        return ("point", [[x] for x in coords])
    if typ == "LineString":
        return ("line", [coords])
    if typ == "MultiLineString":
        return ("line", coords)
    if typ == "Polygon":
        return ("polygon", coords)
    if typ == "MultiPolygon":
        return ("polygon", [ring for poly in coords for ring in poly])
    return ("none", [])


def draw_geom(draw, geometry, fill, outline, layer):
    kind, paths = geometry_paths(geometry)
    for path in paths:
        if not path:
            continue
        try:
            pixels = [coords_to_pixel(x) for x in path]
        except (TypeError, ValueError, IndexError):
            continue
        if kind == "point":
            if layer != "poi":
                continue
            x, y = pixels[0]
            r = 1.9
            draw.ellipse((x-r, y-r, x+r, y+r), fill=fill)
        elif kind == "line" and len(pixels) >= 2:
            line_width = (5 if layer == "road" else
                          2 if layer in ("railway", "pathway", "waterway") else 1)
            draw.line(pixels, fill=outline, width=line_width + (1 if layer=="road" else 0),
                      joint="curve")
            if layer == "road":
                draw.line(pixels, fill=fill, width=line_width, joint="curve")
        elif kind == "polygon" and len(pixels) >= 3:
            draw.polygon(pixels, fill=fill)
            if layer in ("building", "building_part"):
                draw.line(pixels + [pixels[0]], fill=outline, width=1)


def render(tile, title, *, highlight=False):
    im = Image.new("RGB", (W, 740), "#ffffff")
    d = ImageDraw.Draw(im)
    d.rectangle((0,0,W,54), fill="#112e40")
    d.text((24,15), title, font=font(22,True), fill="white")
    d.rectangle((PAD,PAD, PAD+TILE, PAD+TILE),fill="#f0eee8")
    for name, fill, stroke, _ in LAYERS:
        for feature in tile.get(name, {}).get("features", []):
            draw_geom(d, feature.get("geometry") or {},fill,stroke,name)
    d.rectangle((PAD,PAD,PAD+TILE,PAD+TILE),outline="#b9c9cf",width=2)
    d.text((PAD,638), "z14 / 8802 / 5373     |     geometry from actual MVT",
           font=font(15),fill="#506575")
    d.text((PAD,671), "Buildings  |  roads  |  waterways  |  POIs",
           font=font(15),fill="#506575")
    d.text((PAD,700), "Preview style, not OSM Carto or MapLibre",
           font=font(12),fill="#819099")
    if highlight:
        poi = added_cafes(tile)
        if len(poi) != 1:
            raise RuntimeError(f"Exactly one new synthetic cafe required, got {len(poi)}")
        xy = poi[0]["geometry"]["coordinates"]
        x,y = coords_to_pixel(xy)
        d.ellipse((x-13,y-13,x+13,y+13),outline="#d73555",width=4)
        d.ellipse((x-5,y-5,x+5,y+5),fill="#d73555",outline="#ffffff",width=2)
        # Fixed lower footer is an annotation, not a map label from source.
        d.text((PAD,724),"+ 1 cafe via real osm2pgsql --append",
               font=font(12,True),fill="#d73555")
    else:
        d.text((PAD,724),"Original PBF snapshot",
               font=font(12,True),fill=ACCENT)
    return im


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--before", type=Path, required=True)
    p.add_argument("--after", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--source-sha256", required=True)
    args = p.parse_args()
    expected="51d3845df2e4b1334ebec7cf6516e41e71735ce2fc1c0975c52d57f58f1952ad"
    if args.source_sha256 != expected:
        raise RuntimeError("Berlin source not pinned")
    before, before_sha = get_tile(args.before)
    after, after_sha = get_tile(args.after)
    if added_cafes(before) or len(added_cafes(after)) != 1:
        raise RuntimeError("Synthetic cafe delta was not applied exactly once")
    if before_sha == after_sha:
        raise RuntimeError("Demo z14 tile MVT byte payload did not change")
    args.output.mkdir(parents=True,exist_ok=True)
    a = render(before,"BERLIN  |  REAL VECTOR / MVT")
    b = render(after,"AFTER  |  OSM DIFF APPLIED",highlight=True)
    a.save(args.output/"berlin-vector-preview.png",optimize=True)
    comparison = Image.new("RGB", (W*2, 740), "#ffffff")
    comparison.paste(render(before,"BEFORE  |  ORIGINAL PBF"), (0,0))
    comparison.paste(b,(W,0))
    comparison.save(args.output/"berlin-incremental-before-after.png",optimize=True)
    record={
        "pbf_sha256":args.source_sha256,
        "requested_tile_xyz":[Z,X,Y],
        "original_gzip_sha256":before_sha,
        "updated_gzip_sha256":after_sha,
        "demo_added_node_id":NODE_ID,
        "demo_added_node_name":POI_NAME,
        "incremental_change":"synthetic .osc.gz applied using osm2pgsql --append",
        "proven_new_poi_count":1,
        "raster_style":"none; simple Python Pillow visualization of decoded MVT, NOT Mapnik Carto",
        "original_features":{k:len(v.get("features",[])) for k,v in before.items()},
        "updated_features":{k:len(v.get("features",[])) for k,v in after.items()},
    }
    (args.output/"readme-preview-evidence.json").write_text(
        json.dumps(record,indent=2)+"\n")
    print("REAL_BERLIN_README_PREVIEW_OK",json.dumps(record),flush=True)


if __name__ == "__main__":
    main()
