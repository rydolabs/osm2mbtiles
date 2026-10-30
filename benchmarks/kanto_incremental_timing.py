#!/usr/bin/env python3
"""Frozen-main real Kanto PostGIS persistent-vector append + tile flush timing.

Creates three deterministic synthetic POI OSC diffs, applies each once and
checks stored gzip MVT tiles outside the expired XYZ set are untouched.
Not a real daily/hourly replication workload; never extrapolate to one.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from osm2mbtiles import vector_incremental as inc
from osm2mbtiles.tiles import xyz_range

BBOX = (139.745, 35.675, 139.77, 35.70)
ZLO, ZHI = 12, 16
PBF_SHA = "1af62f945ecb25a1c52db30967d7d2db1c6aaea5a17f1dbb1d53dbe55691040e"
MAIN_SHA = "4b7f84ce8bf7a8d4227c1e7f557e9a4056c3db7a"


def sha_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def choose_id(pbf):
    """Avoid absurd synthetic IDs that make sparse-index engines OOM."""
    p = subprocess.Popen(["osmium", "cat", str(pbf), "-f", "opl"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    highest = 0
    assert p.stdout is not None
    for line in p.stdout:
        if line.startswith(b"n"):
            token = line.split(maxsplit=1)[0][1:]
            if token.isdigit():
                highest = max(highest, int(token))
    if p.wait() != 0 or highest < 1:
        raise RuntimeError("OSM IDs could not be enumerated")
    return highest + 1


def write_diff(path, node_id, name, *, lon, lat):
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<osmChange version="0.6" generator="osm2mbtiles-kanto-bench">\n'
        '<create>\n'
        f'<node id="{node_id}" version="1" lat="{lat}" lon="{lon}"'
        ' timestamp="2026-10-30T00:00:00Z" changeset="1">\n'
        '<tag k="amenity" v="hospital"/>\n'
        f'<tag k="name" v="{name}"/>\n'
        '</node>\n'
        '</create>\n'
        '</osmChange>\n'
    ).encode()
    path.write_bytes(gzip.compress(xml, mtime=0))
    assert gzip.decompress(path.read_bytes()) == xml


def raw_fingerprints(path):
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return {
            (int(z), int(x), (1 << int(z)) - 1 - int(row)):
            hashlib.sha256(blob).digest()
            for z, x, row, blob in db.execute(
                "SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles")
        }


def dirty_xyz():
    with inc._connect() as db:
        with db.cursor() as cur:
            cur.execute("SELECT zoom,x,y FROM vector_expired_tiles")
            result = {(int(z), int(x), int(y)) for z, x, y in cur.fetchall()}
    return result


def within_bbox(xyz):
    z, x, y = xyz
    if not ZLO <= z <= ZHI:
        return False
    xmin, xmax, ymin, ymax = xyz_range(BBOX, z)
    return xmin <= x <= xmax and ymin <= y <= ymax


def check_updated_poi(path, name, *, lon, lat):
    import mapbox_vector_tile
    z = 16
    x = int((lon + 180.0) / 360.0 * (1 << z))
    r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(r)) / math.pi) * (1 << z) / 2.0)
    with sqlite3.connect(path) as db:
        found = db.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? "
            "AND tile_row=?", (z, x, (1 << z) - 1 - y)).fetchone()
    if not found:
        return False
    blob = found[0]
    features = mapbox_vector_tile.decode(
        gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob)
    return any(
        item.get("properties", {}).get("name") == name
        for layer in features.values() for item in layer.get("features", [])
    )


def measure(fn):
    started = time.perf_counter()
    result = fn()
    return round(time.perf_counter() - started, 3), result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--pbf", type=Path, required=True)
    a = p.parse_args()
    if os.environ.get("PGDATABASE") != "gis":
        raise RuntimeError("Must use isolated disposable PGDATABASE=gis")
    work, pbf = a.work.resolve(), a.pbf.resolve()
    work.mkdir(parents=True, exist_ok=True)
    input_hash = sha_file(pbf)
    if input_hash != PBF_SHA:
        raise RuntimeError("Kanto PBF differs from frozen initial benchmark SHA: " + input_hash)
    node_id = choose_id(pbf)
    archive = work / "kanto-incremental.mbtiles"
    report = {
        "status": "IN_PROGRESS",
        "source_sha": MAIN_SHA, "pbf_sha256": input_hash,
        "bbox": BBOX, "zoom": [ZLO, ZHI],
        "workflow": "main vector_incremental init, append (--no-flush), flush",
        "snapshot": "full Geofabrik Kanto PBF in persistent DB; small central Tokyo tile window only",
        "changes": "three SEPARATE synthetic hospital node creates near Tokyo; NOT a real replication feed",
        "synthetic_ids": [node_id + i for i in range(3)],
        "samples": [],
        "timing_excludes": "download, package install, PostgreSQL cluster creation, OSM ID scan, SQLite verification",
    }
    out = work / "kanto-incremental-results.json"
    def save():
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    save()
    init_sec, _ = measure(lambda: inc.init(
        pbf, archive, BBOX, ZLO, ZHI, processes=4))
    report["full_initial_incremental_mode_seconds"] = init_sec
    report["initial_tiles"] = len(raw_fingerprints(archive))
    save()
    for i in range(3):
        lon = 139.7500 + 0.006 * i
        lat = 35.6800 + 0.004 * i
        name = f"osm2mbtiles kanto benchmark update {i + 1}"
        diff = work / f"change-{i+1}.osc.gz"
        write_diff(diff, node_id + i, name, lon=lon, lat=lat)
        previous = raw_fingerprints(archive)
        append_s, append_data = measure(
            lambda: inc.append(diff, archive, flush=False))
        if not append_data["flush_pending"]:
            raise RuntimeError("append did not leave durable pending state")
        dirty = dirty_xyz()
        selected = {xyz for xyz in dirty if within_bbox(xyz)}
        flush_s, flush_data = measure(lambda: inc.flush_tiles(archive))
        after = raw_fingerprints(archive)
        changed = {xyz for xyz in previous.keys() | after.keys()
                   if previous.get(xyz) != after.get(xyz)}
        found = check_updated_poi(archive, name, lon=lon, lat=lat)
        sample = {
            "n": i+1, "osm_node_id": node_id+i,
            "diff_sha256": sha_file(diff), "append_sec": append_s,
            "flush_sec": flush_s, "total_update_sec": round(append_s+flush_s, 3),
            "dirty_xyz_count": len(dirty), "dirty_in_bbox": len(selected),
            "changed_gzip_tiles": len(changed),
            "changed_outside_expired": sorted(changed-selected)[:15],
            "all_changed_tiles_expired": changed <= selected,
            "poi_present_in_z16_tile": found,
            "flush": flush_data,
            "initial_nonempty_tiles": len(previous),
            "final_nonempty_tiles": len(after),
        }
        report["samples"].append(sample)
        save()
        print("KANTO_UPDATE_SAMPLE", json.dumps(sample), flush=True)
        if not found or not changed or not changed <= selected or not dirty:
            raise RuntimeError("Synthetic update did not pass output correctness checks")
    vals = [x["total_update_sec"] for x in report["samples"]]
    import statistics
    report["median_update_sec"] = statistics.median(vals)
    report["min_update_sec"] = min(vals)
    report["max_update_sec"] = max(vals)
    report["postgis_three_updates_cumulative_seconds"] = round(sum(vals), 3)
    report["status"] = "PASS"
    save()
    lines = [
        "# Frozen main: Kanto persistent PostGIS vector incremental benchmark",
        "",
        "Full Kanto source SHA256: " + input_hash,
        "Main source SHA: " + MAIN_SHA,
        "Output: localized central Tokyo bbox 139.745,35.675,139.77,35.70, z12-16",
        "",
        f"Initial persistent DB init + original non-Batch full export: **{init_sec:.3f}s**",
        "",
        "| Synthetic change | osm2pgsql append + derived SQL (s) | Dirty-tile flush (s) | Total (s) | Expired XYZ | Actually changed gzip tiles |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for s in report["samples"]:
        lines.append(
            f"| {s['n']} | {s['append_sec']:.3f} | {s['flush_sec']:.3f} "
            f"| {s['total_update_sec']:.3f} | {s['dirty_xyz_count']} | "
            f"{s['changed_gzip_tiles']} |"
        )
    lines.extend([
        "",
        f"**Median:** {report['median_update_sec']:.3f}s; "
        f"range {report['min_update_sec']:.3f}–{report['max_update_sec']:.3f}s.",
        "",
        "Correctness: every change has a new POI in z16 tile; no compressed tile bytes "
        "outside the expired XYZ changed; SQLite integrity_check passed.",
        "",
        "These are deliberately tiny synthetic changes; a real minutely/daily OSM "
        "replication batch (especially way/relation changes) may cost substantially "
        "more. This DOES NOT validate parity with a newly imported full Kanto database.",
        "The init timing uses the original per-tile SQL exporter with retained --slim "
        "tables, NOT optimized batch and NOT the 379.98s --drop benchmark setup.",
        "Independent Planetiler full build 3-run median on this source was 91.86s "
        "(different vector feature profile), but no updated PBF was regenerated "
        "with Planetiler in this benchmark.",
    ])
    (work / "kanto-incremental-results.md").write_text("\n".join(lines) + "\n")
    print("KANTO_REAL_PBF_INCREMENTAL_TIMINGS_PASS", json.dumps(
        {"median_seconds":report["median_update_sec"],
         "initial_seconds":init_sec, "n":len(vals)}), flush=True)


if __name__ == "__main__":
    main()
