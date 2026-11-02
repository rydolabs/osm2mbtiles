#!/usr/bin/env python3
"""Three fresh-PostGIS-DB Berlin PBF-to-MBTiles initial-generation measurements.

This measures independently rebuilt local PostgreSQL clusters, safe osm2pgsql
--slim --drop source import, complete vector/post-import.sql, and unchanged
production osm2mbtiles.vector.export(). No source/MVT/SQL changes are proposed.
The runner's OS page cache may warm between repetitions; this is not a disk-
cold or full packaged-CLI setup measurement. No non-disposable DB is touched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import time

from osm2mbtiles import vector

BBOX=(13.088,52.338,13.761,52.676)
LO,HI=12,16
EXPECTED_PBF_SHA="51d3845df2e4b1334ebec7cf6516e41e71735ce2fc1c0975c52d57f58f1952ad"
EXPECTED_PBF_BYTES=99604748
EXPECTED_NONEMPTY=12047
EXPECTED_GZIP_BYTES=229882912
SOURCE_COMMIT="a29c9b9fd18ee4e85a8e9b08cb21ea51ab004024"


def digest(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for block in iter(lambda:f.read(8*1024*1024),b""):
            h.update(block)
    return h.hexdigest()


def run_logged(args, logfile, env, cwd=None):
    with open(logfile,"w",encoding="utf-8") as log:
        subprocess.run([str(x) for x in args],env=env,cwd=cwd,
                       stdout=log,stderr=subprocess.STDOUT,check=True)


def archive_manifest(path):
    with sqlite3.connect(path) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
            raise RuntimeError("SQLite integrity check failed")
        meta=dict(db.execute("SELECT name,value FROM metadata"))
        if meta.get("format")!="pbf":
            raise RuntimeError("Wrong MBTiles codec")
        meta.pop("name",None)
        rows={}
        total=0
        for z,x,tms,gzip_data in db.execute(
                "SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles"):
            xyz=(int(z),int(x),(1<<int(z))-1-int(tms))
            if xyz in rows or not gzip_data or gzip_data[:2]!=b"\x1f\x8b":
                raise RuntimeError("Missing gzip data or duplicate XYZ")
            rows[xyz]=hashlib.sha256(gzip_data).hexdigest()
            total+=len(gzip_data)
        return {"metadata":meta,"rows":rows,"compressed_bytes":total,
                "file_bytes":path.stat().st_size}


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--pbf",type=Path,required=True)
    p.add_argument("--work",type=Path,required=True)
    p.add_argument("--source-root",type=Path,required=True)
    p.add_argument("--repeats",type=int,default=3)
    args=p.parse_args()
    work=args.work.resolve()
    root=args.source_root.resolve()
    if args.repeats!=3 or str(work)!="/tmp/osm2mbtiles-cold-berlin":
        raise RuntimeError("Only 3 repeat trials on dedicated disposable runner")
    if os.environ.get("PGHOST")!=str(work/"socket"):
        raise RuntimeError("Refusing unknown PostGIS host")
    if os.environ.get("PGUSER")!="coldprobe" or os.environ.get("PGDATABASE")!="gis":
        raise RuntimeError("Refusing non-disposable DB")
    if digest(args.pbf)!=EXPECTED_PBF_SHA or args.pbf.stat().st_size!=EXPECTED_PBF_BYTES:
        raise RuntimeError("Berlin input differs from frozen reference")
    if vector.tile_count(BBOX,LO,HI)!=16943:
        raise RuntimeError("Berlin bbox/zoom drift")
    work.mkdir(parents=True,exist_ok=True)
    if not (root/"vector"/"vector-flex.lua").is_file():
        raise RuntimeError("Missing exact vector source")
    env=os.environ.copy()
    report={"status":"RUNNING","production_sha":SOURCE_COMMIT,
            "pbf_sha256":EXPECTED_PBF_SHA,"pbf_bytes":EXPECTED_PBF_BYTES,
            "bbox":BBOX,"zooms":[LO,HI],"tile_workers":4,
            "osm2pgsql_cache_mb":384,"import_workers":1,
            "measurement_scope":"fresh_postgresql_cluster_to_complete_vector_mbtiles",
            "includes":["initdb","pg_ctl start","createdb and extensions",
                        "osm2pgsql import","derived SQL","vector export and integrity"],
            "excludes":["network PBF download","OS package installation","runtime asset preparation",
                        "Postgres shutdown","cold OS page-cache control"],
            "trials":[],"independent_db_gzip_mismatches":None,
            "adopt_production":False}
    output=work/"cold-berlin-results.json"
    def save():
        output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    save()
    prior=None
    comparisons=[]
    try:
        for iteration in range(1,args.repeats+1):
            pgdir=work/f"pgdata-{iteration}"
            if pgdir.exists():
                raise RuntimeError("Preexisting postgres directory; refusing overwrite")
            archive=work/f"berlin-cold-{iteration}.mbtiles"
            if archive.exists():
                raise RuntimeError("Preexisting archive; refusing overwrite")
            steps={}
            started=time.perf_counter()
            pgstarted=False
            try:
                t=time.perf_counter()
                run_logged(["initdb","-D",pgdir,"-U","coldprobe",
                            "--auth-local=trust","--auth-host=trust"],
                           work/f"initdb-{iteration}.log",env)
                steps["pg_initdb_s"]=round(time.perf_counter()-t,3)
                t=time.perf_counter()
                run_logged(["pg_ctl","-D",pgdir,"-l",work/f"postgres-{iteration}.log",
                            "-w","-o",
                            "-c listen_addresses='' -c unix_socket_directories="+str(work/"socket")
                            +" -c port="+env.get("PGPORT","55516")
                            +" -c shared_buffers=96MB -c work_mem=8MB"
                            +" -c maintenance_work_mem=64MB"
                            +" -c max_parallel_maintenance_workers=0"
                            +" -c max_parallel_workers_per_gather=0 -c jit=off -c fsync=on",
                            "start"],work/f"start-{iteration}.log",env)
                pgstarted=True
                steps["pg_start_s"]=round(time.perf_counter()-t,3)
                t=time.perf_counter()
                run_logged(["createdb","gis"],work/f"createdb-{iteration}.log",env)
                run_logged(["psql","-X","-v","ON_ERROR_STOP=1","-c",
                            "CREATE EXTENSION postgis; CREATE EXTENSION hstore;"],
                           work/f"extensions-{iteration}.log",env)
                steps["pg_database_setup_s"]=round(time.perf_counter()-t,3)
                t=time.perf_counter()
                run_logged(["osm2pgsql","--slim","--drop","--cache=384",
                            "--number-processes=1","--disable-parallel-indexing",
                            "-O","flex","-S",root/"vector"/"vector-flex.lua",
                            "-d","gis",args.pbf],work/f"import-{iteration}.log",env)
                steps["import_s"]=round(time.perf_counter()-t,3)
                t=time.perf_counter()
                run_logged(["psql","-X","-v","ON_ERROR_STOP=1",
                            "-f",root/"vector"/"post-import.sql"],
                           work/f"derived-{iteration}.log",env)
                steps["derived_s"]=round(time.perf_counter()-t,3)
                t=time.perf_counter()
                written=vector.export(args.pbf,archive,BBOX,LO,HI,4)
                steps["vector_export_s"]=round(time.perf_counter()-t,3)
                steps["full_wall_s"]=round(time.perf_counter()-started,3)
                sig=archive_manifest(archive)
                if len(sig["rows"])!=EXPECTED_NONEMPTY or written!=EXPECTED_NONEMPTY:
                    raise RuntimeError("Berlin output XYZ count changed")
                if sig["compressed_bytes"]!=EXPECTED_GZIP_BYTES:
                    raise RuntimeError("Berlin gzip byte count changed")
                entry={"trial":iteration,"status":"PASS","stages":steps,
                       "nonempty_xyz":len(sig["rows"]),
                       "compressed_tile_bytes":sig["compressed_bytes"],
                       "mbtiles_file_bytes":sig["file_bytes"]}
                report["trials"].append(entry)
                if prior:
                    differing=sum(1 for xyz in set(prior["rows"]) | set(sig["rows"])
                                   if prior["rows"].get(xyz)!=sig["rows"].get(xyz))
                    comparisons.append({
                        "reference_trial":1,"other_trial":iteration,
                        "gzip_mismatches":differing,
                        "xyz_equal":set(prior["rows"])==set(sig["rows"]),
                        "metadata_equal":prior["metadata"]==sig["metadata"]})
                else:
                    prior=sig
                print("COLD_BERLIN_TRIAL",json.dumps(entry),flush=True)
                save()
                # Archive is no longer needed; keep only manifest in RAM.
                archive.unlink()
            finally:
                if pgstarted:
                    try:
                        run_logged(["pg_ctl","-D",pgdir,"-m","immediate","-w","stop"],
                                   work/f"stop-{iteration}.log",env)
                    except Exception as ex:
                        print("POSTGRES_STOP_WARNING",repr(ex),flush=True)
        report["independent_db_gzip_mismatches"]=comparisons
        report["medians_s"]={key:round(statistics.median(
            entry["stages"][key] for entry in report["trials"]),3)
            for key in ("pg_initdb_s","pg_start_s","pg_database_setup_s",
                        "import_s","derived_s","vector_export_s","full_wall_s")}
        report["status"]="PASS"
        save()
        print("COLD_BERLIN_DONE",json.dumps({
            "status":report["status"],"medians_s":report["medians_s"],
            "cross_db_gzip_differences":comparisons}),flush=True)
    except BaseException as exc:
        report["status"]="FAIL"
        report["error"]=repr(exc)
        save()
        raise


if __name__=="__main__":
    main()
