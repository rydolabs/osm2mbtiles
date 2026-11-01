#!/usr/bin/env python3
"""Isolated import parameter A/B. No production files or schemas changed.

Each repetition creates a NEW disposable PostGIS database, imports the exact
same PBF with the existing flex style, runs existing post-import SQL, and
fingerprints representative *decoded* MVT output to detect semantic drift.
Only import --cache and --number-processes are varied.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import subprocess
import time

BASE_SHA = "4b7f84ce8bf7a8d4227c1e7f557e9a4056c3db7a"
KANTO_SHA = "1af62f945ecb25a1c52db30967d7d2db1c6aaea5a17f1dbb1d53dbe55691040e"
# Derive the actual sample XYZ from fixed lat/lon instead of using guessed coordinates.
CENTERS = {"monaco": (7.425, 43.735), "kanto": (139.75, 35.68)}
TABLES = ["vector_points", "vector_lines", "vector_areas", "vector_relations", "vector_label_points"]


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(8*1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd, log):
    start = time.perf_counter()
    with open(log, "a", encoding="utf-8") as out:
        out.write("$ " + " ".join(str(x) for x in cmd) + "\n")
        out.flush()
        proc = subprocess.run([str(x) for x in cmd], stdout=out, stderr=subprocess.STDOUT)
    if proc.returncode:
        raise RuntimeError(f"command failed {proc.returncode}: {cmd[0]}")
    return round(time.perf_counter() - start, 3)


def tile_xyz(lon, lat, z):
    return (int((lon+180)/360*(1 << z)),
            int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)*(1 << z)/2))


def canonical_tile(decoded):
    """Sort features; preserve duplicate multiplicity and all feature properties."""
    features = []
    for layer_name, layer in decoded.items():
        for f in layer.get("features", []):
            features.append(json.dumps([
                layer_name, f.get("id"), f.get("type"),
                f.get("geometry"), f.get("properties")
            ], ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    features.sort()
    return {"features": len(features),
            "sha256": hashlib.sha256("\n".join(features).encode()).hexdigest()}


def audit(dbname, region):
    import psycopg2
    import mapbox_vector_tile
    from osm2mbtiles.vector import query_for_zoom
    result = {"tables": {}, "samples": {}}
    with psycopg2.connect(dbname=dbname) as conn:
        with conn.cursor() as c:
            for name in TABLES:
                c.execute("SELECT to_regclass(%s)", ("public."+name,))
                if c.fetchone()[0] is None:
                    raise RuntimeError("missing table: " + name)
                c.execute(f'SELECT count(*) FROM "{name}"')
                result["tables"][name] = c.fetchone()[0]
            lon, lat = CENTERS[region]
            for z in (12, 14, 16):
                x, y = tile_xyz(lon, lat, z)
                sql = query_for_zoom(z, projected=True)
                c.execute(sql, (z, x, y, z, x, y))
                raw = bytes(c.fetchone()[0] or b"")
                result["samples"][f"{z}/{x}/{y}"] = canonical_tile(
                    mapbox_vector_tile.decode(raw) if raw else {})
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--region", choices=("monaco", "kanto"), required=True)
    p.add_argument("--profile", choices=("safe", "candidate"), required=True)
    p.add_argument("--pbf", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--repeats", type=int, default=2)
    a = p.parse_args()
    if a.repeats < 1 or a.repeats > 3:
        raise ValueError("1..3 repeats only")
    if os.environ.get("PGDATABASE") != "gis":
        raise RuntimeError("Only disposable PGDATABASE=gis is allowed")
    expected = os.environ.get("EXPECTED_PBF_SHA256")
    if expected and digest(a.pbf) != expected:
        raise RuntimeError("PBF checksum mismatch: expected " + expected)
    a.work.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[1]
    style = repo/"vector"/"vector-flex.lua"
    post = repo/"vector"/"post-import.sql"
    if not style.exists() or not post.exists():
        raise RuntimeError("Frozen production SQL/style not found")
    cache, processes = (256, 1) if a.profile == "safe" else (384, 2)
    report = {"baseline_sha": BASE_SHA, "region": a.region,
              "pbf_sha256": digest(a.pbf), "profile": a.profile,
              "cache_mb": cache, "import_processes": processes,
              "parallel_indexing": False, "retains_slim_middle": True,
              "repeats": [], "status": "RUNNING"}
    result_file = a.work/"import-profile-results.json"
    def save():
        result_file.write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n")
    save()
    # PostgreSQL cluster is started by the workflow and is not included in timings.
    # Every repeat gets a fresh DB; no cached middle tables or data from prior runs.
    for i in range(a.repeats):
        name = f"gis_import_{a.profile}_{i}"
        log = a.work/f"{name}.log"
        run(["createdb", name], log)
        run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", name, "-c",
             "CREATE EXTENSION postgis; CREATE EXTENSION hstore;"], log)
        command = ["osm2pgsql", "--slim", f"--cache={cache}",
                   f"--number-processes={processes}",
                   "--disable-parallel-indexing", "-O", "flex",
                   "-S", style, "-d", name, a.pbf]
        entry = {"index": i+1, "database": name, "import_command": [str(x) for x in command]}
        report["repeats"].append(entry)
        save()
        try:
            entry["osm2pgsql_s"] = run(command, log)
            save()
            entry["post_import_s"] = run(["psql", "-X", "-v", "ON_ERROR_STOP=1",
                                           "-d", name, "-f", post], log)
            entry["total_import_and_post_s"] = round(
                entry["osm2pgsql_s"] + entry["post_import_s"], 3)
            entry["audit"] = audit(name, a.region)
            entry["maxrss_kb_parent"] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            entry["status"] = "PASS"
            save()
            print("IMPORT_PROFILE_SAMPLE", json.dumps(
                {"region": a.region, "profile": a.profile, "repeat": i+1,
                 "import_s": entry["osm2pgsql_s"],
                 "post_s": entry["post_import_s"],
                 "table_counts": entry["audit"]["tables"],
                 "sample_fingerprints": entry["audit"]["samples"]}), flush=True)
        except BaseException as e:
            entry["status"] = "FAIL"
            entry["error"] = str(e)
            report["status"] = "FAIL"
            save()
            raise
        finally:
            # Drop database even after failure. Never modify user databases.
            run(["dropdb", "--if-exists", name], log)
    audits = [json.dumps(x["audit"], sort_keys=True) for x in report["repeats"]]
    report["same_profile_repeats_semantically_equal"] = len(set(audits)) == 1
    report["status"] = "PASS" if report["same_profile_repeats_semantically_equal"] else "SEMANTIC_MISMATCH"
    save()
    if report["status"] != "PASS":
        raise RuntimeError("Same profile repeated imports generated different decoded MVT")


if __name__ == "__main__":
    main()
