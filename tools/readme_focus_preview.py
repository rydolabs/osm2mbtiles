#!/usr/bin/env python3
"""Zoom and annotate an existing REAL MVT incremental comparison PNG.

Only the preview PNG and the checked, pinned source-evidence JSON are used.
Source map pixels are cropped/resized, never synthesized or retouched.
The synthetic added café is already marked in red by readme_capture.py.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

INK = "#152f3c"
MID = "#526c7a"


def make_image(source: Path, evidence_path: Path, output: Path) -> None:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["pbf_sha256"] == (
        "51d3845df2e4b1334ebec7cf6516e41e71735ce2fc1c0975c52d57f58f1952ad"
    ), "Not the pinned Berlin source"
    assert evidence["requested_tile_xyz"] == [14, 8802, 5373]
    assert evidence["demo_added_node_name"] == "osm2mbtiles README demo cafe"
    assert evidence["proven_new_poi_count"] == 1
    before_count = evidence["original_features"]["poi"]
    after_count = evidence["updated_features"]["poi"]
    assert after_count - before_count == 1, "The real POI delta was not exactly one"
    assert evidence["original_gzip_sha256"] != evidence["updated_gzip_sha256"]
    assert evidence["raster_style"].startswith("none;"), "Do not relabel as Carto"

    src = Image.open(source).convert("RGB")
    assert src.size == (1360, 740), "Unexpected real-data source composition"

    # The existing README comparison has 680px left/right cards. Find only
    # the original red POI annotation in the AFTER map to anchor the callout.
    red = []
    for y in range(60, 630):
        for x in range(740, 1300):
            r, g, b = src.getpixel((x, y))
            if r > 180 and r > g * 1.65 and g < 125 and b < 160:
                red.append((x, y))
    assert len(red) > 150, "Missing verified synthetic POI annotation"
    cx = (min(x for x, _ in red) + max(x for x, _ in red)) / 2
    cy = (min(y for _, y in red) + max(y for _, y in red)) / 2

    # Exact same map pixel window before and after. Do not invent or redraw
    # vector features. The synthetic POI happens near the west tile edge.
    left, top, crop_w, crop_h = 740, 90, 275, 245
    assert left <= cx < left + crop_w and top <= cy < top + crop_h
    original = src.crop((left - 680, top, left - 680 + crop_w, top + crop_h))
    changed = src.crop((left, top, left + crop_w, top + crop_h))

    w, h = 1220, 780
    result = Image.new("RGB", (w, h), "#f7fafb")
    draw = ImageDraw.Draw(result)

    def font(size: int, bold: bool = False):
        stem = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        return ImageFont.truetype(stem, size)

    draw.rectangle((0, 0, w, 122), fill="#123447")
    draw.text((52, 24), "ONE OSM EDIT  →  UPDATED VECTOR TILE",
              font=font(34, True), fill="white")
    draw.text((52, 82),
              "Same Berlin z14 tile • real osm2pgsql --append • synthetic café POI",
              font=font(19), fill="#c4e3e5")

    card_top = 145
    for index, (label, bitmap) in enumerate(
        (("BEFORE  ·  original PBF", original),
         ("AFTER  ·  change applied", changed))
    ):
        x = 46 + index * 590
        draw.rounded_rectangle((x, card_top, x + 545, 682), radius=16,
                               fill="white", outline="#d8e3e7", width=2)
        draw.text((x + 24, card_top + 18), label,
                  font=font(24, True), fill=INK)
        # NEAREST means these are enlarged *real pixels*, not a generated map.
        bitmap = bitmap.resize((493, 439), Image.Resampling.NEAREST)
        result.paste(bitmap, (x + 26, card_top + 64))
        draw.rectangle((x + 25, card_top + 63, x + 520, card_top + 504),
                       outline="#bed0d7", width=2)

    after_x = 46 + 590 + 26 + (cx - left) * 493 / crop_w
    after_y = card_top + 64 + (cy - top) * 439 / crop_h
    ring = 19
    draw.ellipse((after_x-ring, after_y-ring, after_x+ring, after_y+ring),
                 outline="#bb1f4c", width=4)
    draw.line((851, 535, after_x + 20, after_y + 15),
              fill="#bb1f4c", width=4)
    draw.rounded_rectangle((831, 535, 1157, 628), radius=10,
                           fill="#fff8f9", outline="#e6acbc", width=2)
    draw.text((851, 550), "NEW CAFÉ", font=font(22, True), fill="#a02d4e")
    draw.text((851, 583), "1 verified point added", font=font(17), fill=INK)
    draw.text((51, 699),
              f"POI features in shown tile: {before_count:,}  →  {after_count:,}",
              font=font(22, True), fill=INK)
    draw.text((51, 742),
              "Zoomed crop of decoded real MVT geometry; not an OSM Carto or MapLibre style.",
              font=font(14), fill=MID)

    output.parent.mkdir(parents=True, exist_ok=True)
    result.save(output, optimize=True)
    assert output.stat().st_size > 10_000
    print("README_FOCUS_REAL_MVT_OK",
          json.dumps({"pbf_sha256": evidence["pbf_sha256"],
                      "new_poi_count": after_count - before_count,
                      "image_dimensions": [w, h],
                      "output": str(output)}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    make_image(args.source, args.evidence, args.output)


if __name__ == "__main__":
    main()
