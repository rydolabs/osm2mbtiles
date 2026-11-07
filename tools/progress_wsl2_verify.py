#!/usr/bin/env python3
"""Validate real Windows->WSL2 tile progress and MBTiles, without importing project code.

The standard Ubuntu /usr/bin/python3 + python3-pil is sufficient. The script
reads, never edits, the live run's logfile and MBTiles. Evidence excludes the
original PBF, full MBTiles, SQL credentials and any absolute file path.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import sys

from PIL import Image


TILE_LINE = re.compile(
    r"^\[(RASTER|VECTOR)\]\s*(\d+(?:\.\d+)?)%\s*\|\s*"
    r"([\d,]+)/([\d,]+) tiles\b.*?([\d,.]+) tile/s\s*\|\s*ETA (\S+)"
)
STAGE_LINE = re.compile(r"^\[STAGE\]\s*([1-6])/6\s*\|")
PNG_HEADER = b"\x89PNG\r\n\x1a\n"
GZIP_HEADER = b"\x1f\x8b"


def analyze_progress(text: str) -> dict:
    stages = [int(m.group(1)) for line in text.splitlines()
              if (m := STAGE_LINE.match(line))]
    if stages != list(range(1, 7)):
        raise ValueError(f"Missing/out-of-order WSL pipeline stages: {stages}")
    summary = {}
    for name in ("RASTER", "VECTOR"):
        rows = []
        for line in text.splitlines():
            m = TILE_LINE.match(line)
            if m and m.group(1) == name:
                rows.append({
                    "percent": float(m.group(2)),
                    "completed": int(m.group(3).replace(",", "")),
                    "total": int(m.group(4).replace(",", "")),
                    "tiles_per_second": float(m.group(5).replace(",", "")),
                    "eta": m.group(6),
                })
        if not rows:
            raise ValueError(f"{name} displayed no measured tile/s or ETA")
        totals = {r["total"] for r in rows}
        if len(totals) != 1 or min(totals) <= 0:
            raise ValueError(f"{name} tile denominator changed during rendering")
        total = rows[0]["total"]
        if any(r["completed"] < 0 or r["completed"] > total for r in rows):
            raise ValueError(f"{name} has impossible completed tile counts")
        if any(b["completed"] < a["completed"] for a, b in zip(rows, rows[1:])):
            raise ValueError(f"{name} completed tile count went backwards")
        if rows[-1]["completed"] != total or abs(rows[-1]["percent"] - 100) > 0.05:
            raise ValueError(f"{name} did not report 100% measured completion")
        if not any(0 < r["percent"] < 100 for r in rows):
            raise ValueError(f"{name} reported no intermediate progress")
        if not any(r["tiles_per_second"] > 0 for r in rows):
            raise ValueError(f"{name} did not report actual positive throughput")
        summary[name.lower()] = {
            "progress_rows": len(rows),
            "planned_tiles": total,
            "last_completed": rows[-1]["completed"],
            "has_intermediate_percent": True,
            "has_positive_throughput": True,
            "has_eta_field": True,
            "percent_samples": [round(rows[i]["percent"], 1)
                                for i in sorted({0, len(rows)//2, len(rows)-1})],
        }
    if "[RASTER] COMPLETE" not in text:
        raise ValueError("Raster COMPLETE line missing")
    if "[VECTOR] COMPLETE" not in text:
        raise ValueError("Vector COMPLETE line missing")
    if "[TIME] Windows end-to-end elapsed" not in text:
        raise ValueError("Windows elapsed-time line missing")
    return {
        "stages": stages,
        "import_heartbeat_seen": "[IMPORT] osm2pgsql still running" in text,
        "heartbeat_required": False,
        **summary,
    }


def read_windows_log(path: Path) -> str:
    """PowerShell 5.1 Tee-Object writes UTF-16LE; newer shells use UTF-8."""
    raw=path.read_bytes()
    if raw.startswith((bytes((255,254)), bytes((254,255)))):
        return raw.decode("utf-16",errors="replace")
    if raw[:512].count(b"\x00") > 20:
        return raw.decode("utf-16-le",errors="replace")
    return raw.decode("utf-8-sig",errors="replace")


def stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_mbtiles(path: Path, kind: str):
    if not path.is_file():
        raise ValueError(f"Output missing: {path.name}")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError(f"SQLite integrity failure: {path.name}")
        metadata = dict(db.execute("SELECT name,value FROM metadata"))
        if metadata.get("format") != kind:
            raise ValueError(f"Invalid MBTiles format for {path.name}")
        rows = db.execute("SELECT zoom_level,tile_column,tile_row,tile_data "
                          "FROM tiles ORDER BY zoom_level,tile_column,tile_row").fetchall()
        if not rows:
            raise ValueError(f"Empty MBTiles: {path.name}")
        info = {
            "filename": path.name,
            "bytes": path.stat().st_size,
            "sha256": stream_sha256(path),
            "format": kind,
            "tile_count": len(rows),
            "zoom_levels": sorted({int(row[0]) for row in rows}),
            "sqlite_integrity": "ok",
        }
        for z,x,y,_ in rows:
            if not 12 <= z <= 16 or not 0 <= x < 2**z or not 0 <= y < 2**z:
                raise ValueError("Invalid tile coordinate or unexpected zoom level")
        if kind == "png":
            best = (0, None)
            for _,_,_,blob in rows:
                if not blob.startswith(PNG_HEADER):
                    raise ValueError("Raster MBTiles contains non-PNG data")
                im = Image.open(io.BytesIO(blob))
                if im.size != (256, 256):
                    raise ValueError("Unexpected raster tile size")
                rgb = im.convert("RGB")
                diversity = len(set(rgb.resize((64,64)).getdata()))
                if diversity > best[0]:
                    best = (diversity, rgb)
            if best[0] < 10:
                raise ValueError("Rendered Mapnik tiles look blank or flat")
            info["max_sampled_color_count"] = best[0]
            return info, best[1]
        layers = json.loads(metadata.get("json", "{}")).get("vector_layers", [])
        ids = [layer.get("id") for layer in layers]
        if len(ids) < 10 or len(ids) != len(set(ids)) or "road" not in ids:
            raise ValueError("Vector layer metadata absent or malformed")
        if not all(blob.startswith(GZIP_HEADER) for _,_,_,blob in rows):
            raise ValueError("Found non-gzip MVT tile")
        if not any(len(gzip.decompress(blob)) > 10 for _,_,_,blob in rows):
            raise ValueError("No nonempty decompressed MVT payloads")
        info["logical_layer_count"] = len(ids)
        info["gzip_verified"] = True
        return info, None


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--raster", type=Path, required=True)
    parser.add_argument("--vector", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--preview", type=Path, required=True)
    args=parser.parse_args(argv)
    result={"schema":"real-wsl2-progress-v1", "mode":"real_wsl2",
            "passed":False,"progress":None,"archives":[]}
    try:
        raw=read_windows_log(args.log)
        result["progress"]=analyze_progress(raw)
        raster,png=inspect_mbtiles(args.raster,"png")
        vector,_=inspect_mbtiles(args.vector,"pbf")
        result["archives"]=[raster,vector]
        assert png is not None
        args.preview.parent.mkdir(parents=True,exist_ok=True)
        png.save(args.preview,format="PNG",optimize=True)
        result["preview_filename"]=args.preview.name
        result["passed"]=True
        print("REAL_WINDOWS_WSL2_PROGRESS_AND_TILE_OUTPUT_PASS",flush=True)
    except Exception as exc:
        result["error"]=f"{type(exc).__name__}: {exc}"
        print("REAL_WINDOWS_WSL2_PROGRESS_VALIDATION_FAILED",
              result["error"],file=sys.stderr,flush=True)
    finally:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",
                               encoding="utf-8")
    return 0 if result["passed"] else 2


if __name__=="__main__":
    sys.exit(main())
