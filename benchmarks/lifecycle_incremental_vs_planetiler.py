#!/usr/bin/env python3
"""Two-snapshot lifecycle comparison: PostGIS append vs two Planetiler full builds.

Real Geofabrik input PBF + one DETERMINISTIC SYNTHETIC node create OSC.
No production source is modified and no claim of equal engine output is made.
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
import sys
import time

from osm2mbtiles import vector_incremental as incremental
from osm2mbtiles.vector import export as full_export
from postgis_batch_parity import compare_strict, content_signature, planetiler_stats

SCOPE = {
    "monaco": {
        "bbox": (7.41, 43.72, 7.44, 43.75),
        "lon": 7.425, "lat": 43.735,
        "role": "smoke only; too small for broad performance claims",
    },
    "berlin": {
        "bbox": (13.20, 52.45, 13.60, 52.60),
        "lon": 13.405, "lat": 52.525,
        "role": "development sample; repeat independently before publicity",
    },
}
ZOOMS = (12, 16)
def choose_new_node_id(pbf: Path) -> int:
    """Choose max existing node ID + 1 to avoid huge sparse-node-index allocations.

    An enormous artificial ID (e.g. 9e12) can make direct tilers allocate
    unreasonable sparse index memory. Stream OPL without keeping it in RAM.
    This is input-fixture setup, not a timed engine operation.
    """
    proc = subprocess.Popen(
        ["osmium", "cat", str(pbf), "-f", "opl"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    highest = 0
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith(b"n"):
            token = line.split(maxsplit=1)[0][1:]
            if token.isdigit():
                highest = max(highest, int(token))
    if proc.wait() != 0 or highest == 0:
        raise RuntimeError("Could not enumerate source OSM node IDs")
    return highest + 1


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_synthetic_diff(path: Path, *, lat: float, lon: float,
                         node_id: int) -> None:
    """No existing real OSM objects are modified; this is NOT an OSM feed."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<osmChange version="0.6" generator="osm2mbtiles-lifecycle-ci">\n'
        ' <create>\n'
        f'  <node id="{node_id}" version="1"'
        f' lat="{lat}" lon="{lon}"'
        ' timestamp="2026-10-29T00:00:00Z" changeset="1">\n'
        '   <tag k="amenity" v="hospital"/>\n'
        '   <tag k="name" v="osm2mbtiles synthetic update benchmark"/>\n'
        '  </node>\n'
        ' </create>\n'
        '</osmChange>\n'
    ).encode("utf-8")
    path.write_bytes(gzip.compress(xml, compresslevel=6, mtime=0))
    assert gzip.decompress(path.read_bytes()) == xml


def run(cmd, *, cwd=None, env=None, logfile=None, timeout=1800):
    if logfile is None:
        subprocess.run([str(part) for part in cmd], check=True, cwd=cwd,
                       env=env, timeout=timeout)
    else:
        with Path(logfile).open("w") as sink:
            subprocess.run([str(part) for part in cmd], check=True, cwd=cwd,
                           env=env, timeout=timeout, stdout=sink,
                           stderr=subprocess.STDOUT)


def measure(fn):
    started = time.perf_counter()
    result = fn()
    return round(time.perf_counter() - started, 3), result


def compare_archives(a: Path, b: Path) -> dict:
    """Count changed gzip payloads, not just changed SQLite file bytes."""
    def rows(path):
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            return {
                (z, x, (1 << z) - 1 - y): bytes(blob)
                for z, x, y, blob in db.execute(
                    "SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles")
            }
    old, new = rows(a), rows(b)
    changed = [key for key in old.keys() | new.keys()
               if old.get(key) != new.get(key)]
    return {
        "before_nonempty": len(old),
        "after_nonempty": len(new),
        "changed_gzip_tile_rows": len(changed),
        "first_changed_xyz": sorted(changed)[:8],
    }


def lifecycle_costs(postgis_initial, postgis_update, planetiler_initial,
                    planetiler_rebuild, *, pbf_patch=0.0) -> dict:
    """Published comparisons never include a hidden SQL import or omit a build."""
    values = (postgis_initial, postgis_update, planetiler_initial,
              planetiler_rebuild, pbf_patch)
    if any(v < 0 or not math.isfinite(v) for v in values):
        raise ValueError("times must be finite nonnegative seconds")
    pg_two = postgis_initial + postgis_update
    pl_two = planetiler_initial + planetiler_rebuild
    step_saving = planetiler_rebuild - postgis_update
    # Predictive only: assumes repeated diffs of similar size and fixed
    # engine/output profile, rather than proving a live long-run cost.
    breakeven = None
    if step_saving > 0:
        breakeven = max(0, math.ceil(
            (postgis_initial - planetiler_initial) / step_saving - 1e-12))
    return {
        "postgis_initial_seconds": round(postgis_initial, 3),
        "postgis_update_seconds": round(postgis_update, 3),
        "postgis_two_snapshots_seconds": round(pg_two, 3),
        "planetiler_initial_seconds": round(planetiler_initial, 3),
        "planetiler_rebuild_seconds": round(planetiler_rebuild, 3),
        "planetiler_two_builds_seconds": round(pl_two, 3),
        "planetiler_update_pbf_patch_seconds": round(pbf_patch, 3),
        "planetiler_two_builds_plus_pbf_patch_seconds": round(
            pl_two + pbf_patch, 3),
        "postgis_faster_at_two_snapshots_excluding_pbf_patch": pg_two < pl_two,
        "two_snapshot_difference_seconds_planetiler_minus_postgis": round(
            pl_two - pg_two, 3),
        "hypothetical_equal_size_updates_to_break_even": breakeven,
        "break_even_is_model_not_measured_long_run": True,
    }


def md_report(report: dict) -> str:
    t = report["times"]
    success = report["comparison_validated"]
    lines = [
        "# Two-snapshot map maintenance benchmark",
        "",
        f"- Region: **{report['region']}** ({report['role']}); z12–16.",
        f"- Initial PBF SHA256: \`{report['inputs']['initial_pbf_sha256']}\`",
        f"- Updated PBF SHA256: \`{report['inputs']['updated_pbf_sha256']}\`",
        f"- Synthetic OSC SHA256: \`{report['inputs']['diff_osc_sha256']}\`",
        "- The identical initial and final **OSM states** are fed to each engine;",
        "  engine output profiles differ, so tile/feature throughput is not equivalent.",
        "",
        "| Work | PostGIS + persistent DB | Planetiler full builds |",
        "|---|---:|---:|",
        f"| Initial creation | {t['postgis_initial_seconds']:.3f}s | {t['planetiler_initial_seconds']:.3f}s |",
        f"| After one change | {t['postgis_update_seconds']:.3f}s | {t['planetiler_rebuild_seconds']:.3f}s |",
        f"| **Two-snapshot cumulative** | **{t['postgis_two_snapshots_seconds']:.3f}s** | **{t['planetiler_two_builds_seconds']:.3f}s** |",
        "",
        f"- Planetiler second-source PBF patch with osmium (separate): "
        f"**{t['planetiler_update_pbf_patch_seconds']:.3f}s**. "
        "Main table **excludes** it, favoring Planetiler.",
        f"- PostGIS minus Planetiler cumulative difference is "
        f"**{-t['two_snapshot_difference_seconds_planetiler_minus_postgis']:+.3f}s**.",
        f"- PostGIS ahead after the second snapshot: "
        f"**{t['postgis_faster_at_two_snapshots_excluding_pbf_patch']}**.",
        f"- Illustrative update-count break-even if each future diff costs the "
        f"same: **{t['hypothetical_equal_size_updates_to_break_even']}** "
        "(not measured long-run performance).",
        "",
        "| Validation | Result |",
        "|---|---|",
        f"| PostGIS independent final full-rebuild semantic parity | {report['postgis_reference_parity']['equal']} |",
        f"| PostGIS generated changed MVT tiles | {report['postgis_change']['changed_gzip_tile_rows']} |",
        f"| Planetiler generated changed MVT tiles | {report['planetiler_change']['changed_gzip_tile_rows']} |",
        f"| Both engines generated nonempty initial and final tiles | {success} |",
        "",
        "**Interpretation caveats:** one hosted runner/one synthetic POI change; "
        "persistent PostGIS initial DB/server creation, downloaded PBF, runtime "
        "installation and Java installation are excluded from the timed engine "
        "operations. Planetiler PBF patch is reported separately. PostGIS is "
        "vector-only here; no PNG updates or automated replication. Never "
        "generalize Monaco microbenchmarks to Tokyo/Kanto or nationwide updates.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--region", choices=SCOPE, required=True)
    p.add_argument("--pbf", type=Path, required=True)
    p.add_argument("--planetiler-jar", type=Path, required=True)
    p.add_argument("--planetiler-profile", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--project", type=Path, required=True)
    args = p.parse_args(argv)
    if os.environ.get("PGDATABASE") != "gis":
        raise RuntimeError("Run ONLY with disposable PGDATABASE=gis")
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    input_pbf = args.pbf.resolve()
    jar = args.planetiler_jar.resolve()
    profile = args.planetiler_profile.resolve()
    project = args.project.resolve()
    assert input_pbf.is_file() and jar.is_file() and profile.is_file()
    params = SCOPE[args.region]
    bbox = params["bbox"]
    lo, hi = ZOOMS
    diff = work / "synthetic-change.osc.gz"
    updated = work / "after.osm.pbf"
    synthetic_node_id = choose_new_node_id(input_pbf)
    write_synthetic_diff(diff, lat=params["lat"], lon=params["lon"],
                         node_id=synthetic_node_id)
    patch_seconds, _ = measure(lambda: run(
        ["osmium", "apply-changes", input_pbf, diff, "-o", updated],
        logfile=work / "osmium-apply.log"))
    assert sha256(input_pbf) != sha256(updated), "PBF state did not change"

    report = {
        "region": args.region,
        "role": params["role"],
        "bbox": bbox, "zoom": [lo, hi],
        "input_change": "single synthetic created hospital node, NOT a real replication diff",
        "synthetic_created_node_id": synthetic_node_id,
        "versions": {"planetiler": "v0.10.2", "python": sys.version.split()[0]},
        "inputs": {
            "initial_pbf_sha256": sha256(input_pbf),
            "updated_pbf_sha256": sha256(updated),
            "diff_osc_sha256": sha256(diff),
            "planetiler_profile_sha256": sha256(profile),
        },
        "assumptions": [
            "One already running EMPTY PostGIS server and database; server setup excluded.",
            "PostGIS initial includes osm2pgsql --slim import, post-import SQL and full MVT export.",
            "PostGIS update includes osm2pgsql --append, post-import SQL and dirty tile flush.",
            "Planetiler initial and second stage each read a complete independent OSM PBF.",
            "Planetiler PBF patch from OSC is EXCLUDED from primary comparison and shown separately.",
            "Native PostGIS and Planetiler layer/attribute schemas are NOT equivalent.",
            "Cache warming and first-run effects are not controlled; one run is a smoke only.",
        ],
    }
    output = work / "lifecycle-results.json"
    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    save()

    postgis_output = work / "postgis-incremental.mbtiles"
    t_init, _ = measure(lambda: incremental.init(
        input_pbf, postgis_output, bbox, lo, hi, processes=4))
    # Capture the exact initial map before any append.
    initial_archive = work / "postgis-initial.mbtiles"
    import shutil
    shutil.copy2(postgis_output, initial_archive)
    t_append, append_data = measure(lambda: incremental.append(
        diff, postgis_output, flush=False))
    assert append_data["flush_pending"]
    t_flush, flush_data = measure(lambda: incremental.flush_tiles(postgis_output))
    assert flush_data["dirty_total"] > 0, "No dirty XYZ tiles from synthetic OSM diff"
    report["postgis_change"] = compare_archives(initial_archive, postgis_output)
    assert report["postgis_change"]["changed_gzip_tile_rows"] > 0
    report["postgis_flush"] = flush_data
    report["postgis_stages"] = {
        "initial_seconds": t_init,
        "append_seconds": t_append,
        "flush_seconds": t_flush,
    }
    save()

    planet_outputs = []
    planet_times = []
    for step, source in (("initial", input_pbf), ("updated", updated)):
        directory = work / f"planetiler-{step}"
        directory.mkdir()
        (directory / "snapshot.osm.pbf").symlink_to(source)
        archive = work / f"planetiler-{step}.mbtiles"
        command = [
            "java", "-Xmx4g", "-jar", str(jar), "generate-custom",
            "--schema=" + str(profile), "--output=" + str(archive),
            "--bounds=" + ",".join(map(str, bbox)),
            "--minzoom=" + str(lo), "--maxzoom=" + str(hi),
            "--threads=4", "--tmpdir=" + str(directory / "tmp"), "--force",
        ]
        seconds, _ = measure(lambda: run(
            command, cwd=directory, logfile=work / f"planetiler-{step}.log"))
        planet_times.append(seconds)
        planet_outputs.append(archive)
    report["planetiler_change"] = compare_archives(*planet_outputs)
    assert report["planetiler_change"]["changed_gzip_tile_rows"] > 0, (
        "Planetiler profile did not reflect the generated OSM change")
    report["times"] = lifecycle_costs(
        t_init, t_append + t_flush, *planet_times, pbf_patch=patch_seconds)
    report["planetiler_output"] = {
        step: planetiler_stats(archive) for step, archive in
        zip(("initial", "updated"), planet_outputs)
    }
    save()

    # Safety gate: a totally fresh PostGIS import of the final source must
    # have equal decoded features/geometry/attributes/multiplicity to updates.
    # This expensive independent reference build is NOT in timed engine totals.
    run(["createdb", "refgis"], logfile=work / "refdb-createdb.log")
    run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", "refgis",
         "-c", "CREATE EXTENSION postgis; CREATE EXTENSION hstore;"],
        logfile=work / "refdb-extensions.log")
    run(["osm2pgsql", "--slim", "--drop", "--cache=256",
         "--number-processes=1", "-O", "flex", "-S",
         incremental.source_style(), "-d", "refgis", updated],
        logfile=work / "refdb-import.log")
    run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", "refgis",
         "-f", incremental.post_import_sql()],
        logfile=work / "refdb-post-import.log")
    from osm2mbtiles.vector import export as export_vector
    old_pg = os.environ["PGDATABASE"]
    os.environ["PGDATABASE"] = "refgis"
    try:
        reference = work / "reference-full-rebuild.mbtiles"
        export_vector(updated, reference, bbox, lo, hi, 4)
    finally:
        os.environ["PGDATABASE"] = old_pg
    report["postgis_reference_parity"] = compare_strict(
        reference, postgis_output)
    report["comparison_validated"] = bool(report["postgis_reference_parity"]["equal"]
        and report["postgis_change"]["changed_gzip_tile_rows"] > 0
        and report["planetiler_change"]["changed_gzip_tile_rows"] > 0
        and report["planetiler_output"]["initial"]["tiles"] > 0
        and report["planetiler_output"]["updated"]["tiles"] > 0)
    save()
    if not report["comparison_validated"]:
        raise AssertionError("Input/output sanity or PostGIS full-rebuild parity failed")
    md = md_report(report)
    (work / "lifecycle-summary.md").write_text(md)
    print(md, flush=True)
    print("TWO_SNAPSHOT_LIFECYCLE_PASS", json.dumps(report["times"]), flush=True)


if __name__ == "__main__":
    main()
