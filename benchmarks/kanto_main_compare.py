#!/usr/bin/env python3
"""Compare frozen-main PostGIS Batch Tokyo output against full Planetiler builds.

Same complete Geofabrik Kanto PBF, same Tokyo bbox and z12-16. PostGIS
reuses a single imported DB; Planetiler regenerates from PBF on every run.
Schemas differ; never claim equal-output throughput.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import subprocess
import sys

from osm2mbtiles.tiles import tile_count, xyz_range

BBOX = (139.55, 35.55, 139.95, 35.85)
ZOOMS = (12, 16)
WORKERS = 4
ENGINES = ("batch", "planetiler")


def sha_file(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def timed(path):
    fields = path.read_text().strip().split()
    assert len(fields) == 2, ("bad /usr/bin/time output", str(path))
    return {"elapsed_sec": float(fields[0]), "max_command_rss_kib": int(fields[1])}


def run_timed(work, name, args, *, cwd=None):
    timer = work / (name + ".time")
    log = work / (name + ".log")
    cmd = ["/usr/bin/time", "-f", "%e %M", "-o", str(timer),
           "timeout", "1800", *map(str, args)]
    with log.open("w") as output:
        subprocess.run(cmd, cwd=cwd, check=True, timeout=1900,
                       stdout=output, stderr=subprocess.STDOUT)
    return timed(timer)


def audit(path):
    """All tile XYZ coordinates, SQLite integrity, MVT gzip header and sample."""
    import mapbox_vector_tile
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        meta = dict(db.execute("SELECT name,value FROM metadata"))
        assert meta.get("format") == "pbf"
        digest = hashlib.sha256()
        counts = {}
        out_of_bbox = invalid_payload = 0
        sample_layers = None
        count = 0
        gzip_count = 0
        for z, x, row, data in db.execute(
                "SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles "
                "ORDER BY zoom_level,tile_column,tile_row"):
            count += 1
            counts[str(z)] = counts.get(str(z), 0) + 1
            if not ZOOMS[0] <= z <= ZOOMS[1]:
                out_of_bbox += 1
            else:
                x0, x1, y0, y1 = xyz_range(BBOX, z)
                y = (1 << z) - 1 - row
                if not (x0 <= x <= x1 and y0 <= y <= y1):
                    out_of_bbox += 1
            if not data:
                invalid_payload += 1
                continue
            digest.update(f"{z}/{x}/{row}|{len(data)}|".encode())
            digest.update(data)
            gzip_count += data[:2] == b"\x1f\x8b"
            if sample_layers is None:
                payload = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
                sample_layers = sorted(mapbox_vector_tile.decode(payload))
        assert count > 0 and out_of_bbox == 0 and invalid_payload == 0
        assert count <= tile_count(BBOX, *ZOOMS)
        return {
            "tiles": count, "by_zoom": counts, "gzip_tiles": gzip_count,
            "archive_bytes": path.stat().st_size,
            "payload_fingerprint": digest.hexdigest(),
            "sample_layers": sample_layers, "out_of_bbox": out_of_bbox,
        }


def make_markdown(report):
    q = report["aggregate"]
    imp = report["postgis_import"]["elapsed_sec"]
    batch = q["batch"]["median_sec"]
    planet = q["planetiler"]["median_sec"]
    cold = report["cold_component_sum_sec"]
    lines = [
        "# Kanto PBF / Tokyo bbox z12-16: frozen main vs Planetiler",
        "",
        "PostGIS source commit: " + report["source_sha"],
        "Kanto PBF SHA256: " + report["pbf_sha256"],
        "Planetiler profile SHA256: " + report["profile_sha256"],
        "Same runner / Kanto source / Tokyo bbox 139.55,35.55,139.95,35.85 / 4 workers / z12-16.",
        "",
        "| Stage | Main PostGIS Batch | Planetiler v0.10.2 |",
        "|---|---:|---:|",
        f"| DB create + import full Kanto + post-import | {imp:.2f}s | n/a |",
        f"| Warm Batch tile stage vs complete PBF build, 3-run median | {batch:.2f}s | {planet:.2f}s |",
        f"| Run range | {q['batch']['min_sec']:.2f}-{q['batch']['max_sec']:.2f}s | {q['planetiler']['min_sec']:.2f}-{q['planetiler']['max_sec']:.2f}s |",
        f"| Cold PostGIS component sum vs Planetiler PBF-to-MBTiles median | {cold:.2f}s | {planet:.2f}s |",
        f"| Nonempty tiles (first run) | {report['runs'][0]['batch']['archive']['tiles']:,} | {report['runs'][0]['planetiler']['archive']['tiles']:,} |",
        f"| MBTiles file MiB (first run) | {report['runs'][0]['batch']['archive']['archive_bytes']/1048576:.1f} | {report['runs'][0]['planetiler']['archive']['archive_bytes']/1048576:.1f} |",
        "",
        f"Cold component sum / Planetiler median: {cold/planet:.2f}x (different output profiles; not equivalent work).",
        "",
        "| Round (alternating order) | Warm PostGIS Batch | Complete Planetiler |",
        "|---:|---:|---:|",
    ]
    for r in report["runs"]:
        lines.append(f"| {r['n']} | {r['batch']['timing']['elapsed_sec']:.2f}s | {r['planetiler']['timing']['elapsed_sec']:.2f}s |")
    lines += [
        "",
        "Caveats:",
        "- PostGIS imports the ENTIRE Kanto PBF; both render only Tokyo bbox.",
        "- PostGIS tile-only is WARM and excludes import, unlike Planetiler PBF-to-MBTiles.",
        "- Different layer selection/attributes and output sizes: not an equal-workload parity comparison.",
        "- The cold PostGIS number is a sum of distinct stages, not one continuous process run.",
        "- Three sequential runs on one hosted runner, not three independent machines.",
        "- Downloads, package installs and PostgreSQL server shutdown are not timed.",
        "- Measured command peak RSS excludes resident Postgres DB server memory.",
        "- This comparison does not benchmark .osc.gz incremental update; that uses non-Batch SQL.",
    ]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--pbf", type=Path, required=True)
    ap.add_argument("--jar", type=Path, required=True)
    ap.add_argument("--profile", type=Path, required=True)
    ap.add_argument("--source-sha", required=True)
    ap.add_argument("--runs", type=int, default=3)
    cfg = ap.parse_args()
    if cfg.runs < 3:
        raise ValueError("At least three alternating runs required")
    work = cfg.work.resolve()
    pbf, jar, profile = (p.resolve() for p in (cfg.pbf, cfg.jar, cfg.profile))
    assert all(p.is_file() for p in (pbf, jar, profile))
    report = {
        "status": "IN_PROGRESS", "source_sha": cfg.source_sha,
        "pbf_sha256": sha_file(pbf), "pbf_bytes": pbf.stat().st_size,
        "planetiler_jar_sha256": sha_file(jar),
        "profile_sha256": sha_file(profile),
        "bbox": BBOX, "zoom": ZOOMS,
        "workers": WORKERS, "batch_side": 2,
        "postgis_import": timed(work / "postgis-import.time"),
        "runs": [], "native_profiles_not_equivalent": True
    }
    result = work / "kanto-current-main-results.json"
    def save():
        result.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    save()
    project = Path(__file__).resolve().parents[1]
    for n in range(1, cfg.runs + 1):
        row = {"n": n}
        order = ENGINES if n % 2 else tuple(reversed(ENGINES))
        print("BENCHMARK_ORDER", n, *order, flush=True)
        for engine in order:
            stem = engine + "-r" + str(n)
            archive = work / (stem + ".mbtiles")
            if engine == "batch":
                job = work / (stem + "-job.json")
                job.write_text(json.dumps({
                    "pbf": str(pbf), "output": str(archive),
                    "bbox": BBOX, "minzoom": ZOOMS[0],
                    "maxzoom": ZOOMS[1], "processes": WORKERS,
                    "tiler": "batch", "batch_side": 2,
                }))
                cmd = [sys.executable,
                       project / "src/osm2mbtiles/vector_worker.py", job]
                cwd = None
            else:
                cwd = work / (stem + "-work")
                cwd.mkdir()
                (cwd / "kanto.osm.pbf").symlink_to(pbf)
                cmd = [
                    "java", "-Xmx5g", "-jar", jar, "generate-custom",
                    "--schema=" + str(profile),
                    "--output=" + str(archive),
                    "--bounds=" + ",".join(map(str, BBOX)),
                    "--minzoom=12", "--maxzoom=16", "--threads=4",
                    "--tmpdir=" + str(cwd / "tmp"), "--force"
                ]
            timing = run_timed(work, stem, cmd, cwd=cwd)
            stats = audit(archive)
            row[engine] = {"timing": timing, "archive": stats}
            print("BENCHMARK_STAGE", n, engine,
                  timing["elapsed_sec"], stats["tiles"], flush=True)
            # Save every stage and clean up before the next full build.
            report["current_run"] = row
            save()
            archive.unlink()
            if cwd is not None:
                shutil.rmtree(cwd)
        del report["current_run"]
        report["runs"].append(row)
        save()
    summary = {}
    for engine in ENGINES:
        times = [r[engine]["timing"]["elapsed_sec"] for r in report["runs"]]
        summary[engine] = {
            "times": times, "median_sec": statistics.median(times),
            "min_sec": min(times), "max_sec": max(times),
            "identical_gzip_payloads_in_repeats": len(set(
                r[engine]["archive"]["payload_fingerprint"] for r in report["runs"]
            )) == 1
        }
    report["aggregate"] = summary
    report["cold_component_sum_sec"] = round(
        report["postgis_import"]["elapsed_sec"] +
        summary["batch"]["median_sec"], 3)
    report["status"] = "PASS"
    save()
    md = make_markdown(report)
    (work / "kanto-current-main-results.md").write_text(md)
    print(md, flush=True)
    print("MAIN_KANTO_VS_PLANETILER_PASS", json.dumps({
        "postgis_import": report["postgis_import"]["elapsed_sec"],
        "postgis_batch_median": summary["batch"]["median_sec"],
        "planetiler_median": summary["planetiler"]["median_sec"],
        "cold_sum": report["cold_component_sum_sec"],
    }), flush=True)


if __name__ == "__main__":
    main()
