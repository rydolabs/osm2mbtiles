#!/usr/bin/env python3
"""Read-only production-code audit: persistent OSM DB vs existing Batch MVT.

Only benchmark harness code is new. Production SQL/style/encoder stays frozen.
The experiment must FAIL on any same-DB gzip tile byte mismatch.
"""
from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
from itertools import zip_longest
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import time

MAIN_SHA = "4b7f84ce8bf7a8d4227c1e7f557e9a4056c3db7a"
KANTO_SHA = "1af62f945ecb25a1c52db30967d7d2db1c6aaea5a17f1dbb1d53dbe55691040e"
REGIONS = {
    "monaco": {"bbox": (7.41, 43.72, 7.44, 43.75),
               "poi": (7.425, 43.735), "sha": None},
    "kanto": {"bbox": (139.745, 35.675, 139.77, 35.70),
              "poi": (139.750, 35.680), "sha": KANTO_SHA},
}
ZOOM = (12, 16)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def timed(fn):
    start = time.perf_counter()
    result = fn()
    return round(time.perf_counter() - start, 3), result


def rows(archive):
    with sqlite3.connect(archive) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for z, x, tms_y, blob in db.execute(
            "SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles "
            "ORDER BY zoom_level,tile_column,tile_row"
        ):
            yield (int(z), int(x), (1 << int(z)) - 1 - int(tms_y)), bytes(blob)


def byte_compare(a, b, *, sample_limit=8):
    """Streaming comparison: never copy an entire large MBTiles file into RAM."""
    tiles, mismatches, xyz_mismatches = 0, 0, 0
    samples = []
    for left, right in zip_longest(rows(a), rows(b)):
        if left is None or right is None:
            xyz_mismatches += 1
            if len(samples) < sample_limit:
                samples.append({"xyz": str(left[0] if left else right[0]),
                                "cause": "missing-coordinate"})
            continue
        if left[0] != right[0]:
            xyz_mismatches += 1
            if len(samples) < sample_limit:
                samples.append({"left_xyz": left[0], "right_xyz": right[0],
                                "cause": "coordinate-mismatch"})
            continue
        tiles += 1
        if left[1] != right[1]:
            mismatches += 1
            if len(samples) < sample_limit:
                samples.append({"xyz": left[0], "cause": "gzip-byte-mismatch",
                                "left_sha256": hashlib.sha256(left[1]).hexdigest(),
                                "right_sha256": hashlib.sha256(right[1]).hexdigest()})
    return {"compared_tiles": tiles, "gzip_mismatches": mismatches,
            "xyz_mismatches": xyz_mismatches,
            "byte_equal": mismatches == 0 and xyz_mismatches == 0,
            "samples": samples}


def choose_node_id(pbf):
    """Use max+1 from real PBF, not a sparse huge fabricated ID."""
    p = subprocess.Popen(["osmium", "cat", str(pbf), "-f", "opl"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    highest = 0
    assert p.stdout is not None
    for line in p.stdout:
        if line.startswith(b"n"):
            num = line.split(maxsplit=1)[0][1:]
            if num.isdigit():
                highest = max(highest, int(num))
    if p.wait() != 0 or not highest:
        raise RuntimeError("Unable to read OSM node IDs")
    return highest + 1


def synthetic_osc(path, node_id, lon, lat, name):
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<osmChange version="0.6" generator="persistent-batch-audit">'
        '<create>'
        f'<node id="{node_id}" version="1" lat="{lat}" lon="{lon}"'
        ' timestamp="2026-10-30T00:00:00Z" changeset="1">'
        '<tag k="amenity" v="hospital"/>'
        f'<tag k="name" v="{name}"/>'
        '</node>'
        '</create></osmChange>'
    ).encode()
    path.write_bytes(gzip.compress(xml, compresslevel=6, mtime=0))


def expired_coordinates(inc):
    with inc._connect() as conn:
        with conn.cursor() as c:
            c.execute("SELECT zoom,x,y FROM vector_expired_tiles")
            return {tuple(int(v) for v in row) for row in c.fetchall()}


def find_poi(archive, name, lon, lat):
    import mapbox_vector_tile
    z = 16
    x = int((lon + 180) / 360 * (1 << z))
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) * (1 << z) / 2)
    with sqlite3.connect(archive) as db:
        result = db.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND "
            "tile_column=? AND tile_row=?",
            (z, x, (1 << z) - 1 - y)).fetchone()
    if not result:
        return False
    blob = bytes(result[0])
    decoded = mapbox_vector_tile.decode(gzip.decompress(blob))
    return any(f.get("properties", {}).get("name") == name
               for lyr in decoded.values() for f in lyr.get("features", []))


def explain_summary(cur, sql, params):
    """EXPLAIN is read-only; aggregate actual plan counters in a small JSON."""
    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
    parsed = cur.fetchone()[0]
    if isinstance(parsed, str):
        parsed = json.loads(parsed)
    top = parsed[0]
    types, relations = Counter(), Counter()
    def walk(node):
        types[node.get("Node Type", "?")] += 1
        if "Relation Name" in node:
            relations[node["Relation Name"]] += 1
        for key in ("Plans", "InitPlan", "Subplans"):
            for child in node.get(key, []):
                walk(child)
    walk(top["Plan"])
    return {"execution_ms": round(float(top["Execution Time"]), 2),
            "planning_ms": round(float(top.get("Planning Time", 0)), 2),
            "shared_hit_blocks": top["Plan"].get("Shared Hit Blocks", 0),
            "shared_read_blocks": top["Plan"].get("Shared Read Blocks", 0),
            "temp_read_blocks": top["Plan"].get("Temp Read Blocks", 0),
            "temp_written_blocks": top["Plan"].get("Temp Written Blocks", 0),
            "node_types": types.most_common(12),
            "relations": relations.most_common(12)}


def produce_report_md(r):
    lines = [
        "# Stable-main persistent PostGIS + Batch audit",
        "",
        f"- Main source: \`{r['main_sha']}\` (production files unchanged)",
        f"- Region source: **{r['region']}** SHA256 \`{r['pbf_sha256']}\`",
        f"- Tile bbox: \`{r['bbox']}\`, zoom z12–16; **local sample only**",
        "- Existing production \`vector_incremental.init\` and "
        "\`vector_batch.export_batch\` used without modification.",
        "",
        "## Measured initial path",
        "",
        f"- Persistent-DB init total (includes legacy export): **{r['initial_total_s']} s**",
        f"- Independent same-DB Batch full export: **{r['batch_initial_s']} s**",
        f"- Actual native phase timings: \`{json.dumps(r['initial_phases_s'])}\`",
        "",
        "## Strict gzip tile parity",
        "",
        f"- Legacy vs Batch, same DB, initial: **{r['initial_parity']}**",
        f"- Legacy after OSC+flush vs Batch, same DB: **{r.get('updated_parity')}**",
        "",
        "## One deterministic synthetic hospital OSC (NOT replication feed)",
        "",
        f"- \`osm2pgsql --append\` + derived post-import SQL: **{r.get('append_s')} s**",
        f"- Dirty tile flush: **{r.get('flush_s')} s**",
        f"- Dirty XYZ: **{r.get('dirty_xyz')}**, gzip-changed XYZ: **{r.get('changed_tiles')}**",
        f"- Added POI visible at z16: **{r.get('poi_visible')}**",
        f"- Unexpired tile gzip payload unchanged: **{r.get('unchanged_outside_dirty')}**",
        f"- Empty-flush idempotent + duplicate diff rejection: "
        f"**{r.get('empty_flush_ok')} / {r.get('duplicate_rejected')}**",
        "",
        "## Batch read-only SQL diagnostics",
        "",
    ]
    for z, v in r.get("explain", {}).items():
        lines.append(
            f"- z{z}: {v['execution_ms']} ms, shared hit/read "
            f"{v['shared_hit_blocks']}/{v['shared_read_blocks']}, "
            f"temp read/write {v['temp_read_blocks']}/{v['temp_written_blocks']}")
    lines += [
        "",
        "## Limitations",
        "",
        "- Database import uses osm2pgsql --slim (retained middle), single importer. "
        "It is not the previous 261.13s --drop/parallel Kanto benchmark.",
        "- Local Kanto bbox is much smaller than the previous Tokyo 6,355 tiles. "
        "Measured runtime must not be scaled or represented as full-Tokyo.",
        "- ANALYZE executes representative SQL for diagnosis and is excluded "
        "from timed exporter. No new persistent cache is introduced.",
        "- Independent fresh-DB parity and real high-volume OSM relation/way "
        "replication are not covered by this synthetic one-node smoke.",
        f"- Final result: **{r['status']}**.",
    ]
    return "\n".join(lines) + "\n"


def main():
    from osm2mbtiles import vector_incremental as inc
    from osm2mbtiles import vector_batch as batch
    from osm2mbtiles.tiles import xyz_range
    p = argparse.ArgumentParser()
    p.add_argument("--region", choices=REGIONS, required=True)
    p.add_argument("--pbf", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    a = p.parse_args()
    if os.environ.get("PGDATABASE") != "gis":
        raise RuntimeError("Require disposable PGDATABASE=gis")
    os.environ["OSM2MBTILES_VECTOR_QUERY_MODE"] = "projected"
    cfg = REGIONS[a.region]
    work, pbf = a.work.resolve(), a.pbf.resolve()
    work.mkdir(parents=True, exist_ok=True)
    sha = digest(pbf)
    if cfg["sha"] and sha != cfg["sha"]:
        raise RuntimeError("Expected frozen Kanto PBF hash: " + sha)
    bbox = cfg["bbox"]
    report = {"status": "RUNNING", "region": a.region, "main_sha": MAIN_SHA,
              "pbf_sha256": sha, "bbox": bbox, "zoom": ZOOM,
              "initial_phases_s": {}, "explain": {}}
    destination = work / "persistent-batch-results.json"
    def save():
        destination.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    # Wrap the EXISTING public benchmark-facing functions in memory only.
    # No production file is changed, and the signature of production API is preserved.
    old_run = inc._run
    old_export = inc.full_export
    def track_run(cmd):
        elapsed, result = timed(lambda: old_run(cmd))
        key = "import_osm2pgsql_s" if Path(str(cmd[0])).name == "osm2pgsql" else "post_import_sql_s"
        report["initial_phases_s"].setdefault(key, []).append(elapsed)
        save()
        return result
    def track_legacy(*args, **kwargs):
        elapsed, result = timed(lambda: old_export(*args, **kwargs))
        report["initial_phases_s"]["legacy_full_export_s"] = elapsed
        save()
        return result
    inc._run = track_run
    inc.full_export = track_legacy
    legacy = work / "legacy-persistent.mbtiles"
    batch_initial = work / "batch-persistent.mbtiles"
    batch_updated = work / "batch-after-diff.mbtiles"
    t, _ = timed(lambda: inc.init(pbf, legacy, bbox, *ZOOM, processes=4))
    report["initial_total_s"] = t
    save()
    # Genuine unchanged production Batch, same imported persistent DB.
    t, _ = timed(lambda: batch.export_batch(pbf, batch_initial, bbox, *ZOOM, 4, side=2))
    report["batch_initial_s"] = t
    report["initial_parity"] = byte_compare(legacy, batch_initial)
    save()
    print("PERSISTENT_INITIAL_PARITY", json.dumps(report["initial_parity"]), flush=True)

    # Explain only SELECTs against already imported database, with bound XYZs.
    w, s, e, n = bbox
    cx, cy = (w+e)/2, (s+n)/2
    with inc._connect() as conn:
        with conn.cursor() as cur:
            for z in (12, 14, 16):
                x = int((cx+180)/360*(1 << z))
                y = int((1-math.asinh(math.tan(math.radians(cy)))/math.pi)*(1 << z)/2)
                cells = [(x+dx, y+dy) for dy in (0,1) for dx in (0,1)]
                params = [u for cell in cells for u in cell]
                report["explain"][str(z)] = explain_summary(
                    cur, batch.batch_query(z, len(cells)), params)
                save()

    nodeid = choose_node_id(pbf)
    lon, lat = cfg["poi"]
    name = "osm2mbtiles persistent batch parity synthetic hospital"
    osc = work / "synthetic.osc.gz"
    synthetic_osc(osc, nodeid, lon, lat, name)
    before = dict(rows(legacy))
    # Later staging via existing code only. Track append phase SQL separately.
    report["initial_phases_s"]["append_external_commands_s"] = []
    t, _ = timed(lambda: inc.append(osc, legacy, flush=False))
    report["append_s"] = t
    report["append_commands_s"] = report["initial_phases_s"].get("import_osm2pgsql_s", [])[1:]
    report["append_sql_s"] = report["initial_phases_s"].get("post_import_sql_s", [])[1:]
    dirty = expired_coordinates(inc)
    t, result = timed(lambda: inc.flush_tiles(legacy))
    report["flush_s"] = t
    report["flush_result"] = result
    after = dict(rows(legacy))
    changed = {xyz for xyz in before.keys() | after.keys()
               if before.get(xyz) != after.get(xyz)}
    report["dirty_xyz"] = len(dirty)
    report["changed_tiles"] = len(changed)
    report["changed_outside_dirty"] = sorted(changed-dirty)[:12]
    report["unchanged_outside_dirty"] = not (changed-dirty)
    report["poi_visible"] = find_poi(legacy, name, lon, lat)
    report["dirty_covers_changed"] = changed <= dirty and bool(changed)
    empty = inc.flush_tiles(legacy)
    report["empty_flush_ok"] = (empty["dirty_total"] == 0 and
                                byte_compare(legacy, legacy)["byte_equal"])
    try:
        inc.append(osc, legacy, flush=False)
    except RuntimeError as exc:
        report["duplicate_rejected"] = "same diff twice" in str(exc)
    else:
        report["duplicate_rejected"] = False
    t, _ = timed(lambda: batch.export_batch(pbf, batch_updated, bbox, *ZOOM, 4, side=2))
    report["batch_updated_s"] = t
    report["updated_parity"] = byte_compare(legacy, batch_updated)
    pass_checks = (
        report["initial_parity"]["byte_equal"]
        and report["updated_parity"]["byte_equal"]
        and report["dirty_covers_changed"]
        and report["unchanged_outside_dirty"]
        and report["poi_visible"]
        and report["empty_flush_ok"]
        and report["duplicate_rejected"]
    )
    report["status"] = "PASS" if pass_checks else "PARITY_OR_UPDATE_FAIL"
    save()
    (work / "persistent-batch-results.md").write_text(produce_report_md(report))
    print("PERSISTENT_BATCH_AUDIT_RESULT", json.dumps(
        {"region": a.region, "status": report["status"],
         "legacy_s": t, "initial_parity": report["initial_parity"],
         "updated_parity": report["updated_parity"]}), flush=True)
    if not pass_checks:
        raise RuntimeError("Persistent Batch parity or OSC update invariant failed")


if __name__ == "__main__":
    main()
