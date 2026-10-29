"""Directly executed under the WSL interpreter with system psycopg2.

Original tile SQL remains the default. Batch is an explicit experimental opt-in.
"""
import json
import sys
from pathlib import Path

from osm2mbtiles.vector import export


def run_job(job):
    pbf = Path(job["pbf"])
    out = Path(job["output"])
    bbox = tuple(job["bbox"])
    lo = int(job["minzoom"])
    hi = int(job["maxzoom"])
    processes = int(job["processes"])
    tiler = job.get("tiler", "postgis")
    if tiler == "batch":
        from osm2mbtiles.vector_batch import export_batch
        return export_batch(pbf, out, bbox, lo, hi, processes,
                            side=int(job.get("batch_side", 2)))
    if tiler == "postgis":
        return export(pbf, out, bbox, lo, hi, processes)
    raise ValueError("Unknown vector tiler: " + str(tiler))


if __name__ == "__main__":
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    run_job(job)
