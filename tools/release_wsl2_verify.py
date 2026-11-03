#!/usr/bin/env python3
"""Read-only release evidence for actual Windows+WSL2 Raster and Vector MBTiles.

Usage:
  python3 release_wsl2_verify.py both.raster.mbtiles both.vector.mbtiles \
      vector.mbtiles result.json preview.png

Output is deliberately path-free. No PBF, database, or full MBTiles is copied
into the result ZIP. This script never changes or removes source archives.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys

try:
    from PIL import Image
except ImportError as exc:
    raise SystemExit("python3-pil required in Ubuntu WSL: " + str(exc))


def describe(path: Path, required_format: str):
    if not path.is_file():
        raise RuntimeError(f"Expected output not found: {path.name}")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        meta = dict(db.execute("SELECT name,value FROM metadata"))
        assert meta.get("format") == required_format, (path.name, meta.get("format"))
        rows = db.execute(
            "SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles "
            "ORDER BY zoom_level,tile_column,tile_row"
        ).fetchall()
        assert rows, f"No tile rows: {path.name}"
        report = {
            "filename": path.name,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "format": required_format,
            "tiles": len(rows),
            "zooms": sorted({z for z, _, _, _ in rows}),
            "sqlite_integrity": "ok",
        }
        for z, x, tms_y, _ in rows:
            assert 12 <= z <= 13 and 0 <= x < 2 ** z and 0 <= tms_y < 2 ** z
        if required_format == "png":
            best = None
            for _, _, _, blob in rows:
                assert blob.startswith(bytes.fromhex("89504e470d0a1a0a"))
                im = Image.open(io.BytesIO(blob))
                assert im.size == (256, 256), im.size
                rgb = im.convert("RGB")
                # A 64x64 sample lets us catch a flat/empty style without
                # storing any raw personal computer or map input data.
                diversity = len(set(rgb.resize((64, 64)).getdata()))
                if best is None or diversity > best[0]:
                    best = (diversity, rgb)
            assert best is not None and best[0] >= 10, (
                "Actual OSM Carto PNG tiles appear blank/flat", report["filename"]
            )
            report["max_distinct_sampled_colors"] = best[0]
            return report, best[1]
        assert all(blob[:2] == b"\x1f\x8b" for _, _, _, blob in rows)
        assert any(len(gzip.decompress(blob)) > 100 for _, _, _, blob in rows)
        layers = json.loads(meta["json"])["vector_layers"]
        ids = [layer["id"] for layer in layers]
        assert "road" in ids and "water" in ids, ids
        # Only zoom-eligible schema layers appear in this z12-13 MBTiles.
        assert len(ids) == len(set(ids)) and 10 <= len(ids) <= 19, ids
        report["metadata_layer_ids"] = ids
        report["metadata_layer_count"] = len(ids)
        report["gzip_mvt_decodable"] = True
        return report, None


def main(argv):
    if len(argv) != 5:
        raise SystemExit(
            "Expected both raster, both vector, vector, result.json, preview.png"
        )
    *inputs, json_dest, png_dest = map(Path, argv)
    result = {"schema": "wsl2-mbtiles-evidence-v1", "passed": False,
              "outputs": [], "preview": None}
    try:
        best_preview = None
        for file, kind in zip(inputs, ("png", "pbf", "pbf")):
            info, sample = describe(file, kind)
            result["outputs"].append(info)
            if sample is not None and best_preview is None:
                best_preview = sample
        assert best_preview is not None
        best_preview.save(png_dest, format="PNG", optimize=True)
        result["preview"] = "preview-raster.png"
        result["passed"] = True
        print("REAL_WINDOWS_WSL2_MBTILES_VERIFIED", flush=True)
    except Exception as exc:
        result["error"] = str(exc)
        print("REAL_WINDOWS_WSL2_MBTILES_VERIFICATION_FAILED",
              type(exc).__name__, str(exc), file=sys.stderr, flush=True)
    finally:
        json_dest.parent.mkdir(parents=True, exist_ok=True)
        json_dest.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
