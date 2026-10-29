"""CLI orchestration: automatic local DB lifecycle, import, raster render, cleanup."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from .pbf import read_bbox, PBFError
from .runtime import Runtime, LocalDatabase, PipelineError, file_digest, run
from .tiles import validate_bbox, tile_count, metatiles
from .validate import inspect, compare, ValidationError

MAX_TILES = 2_000_000

def arguments(argv=None):
    p = argparse.ArgumentParser(prog='osm2mbtiles',description='Docker-free OSM Carto raster + detailed PBF vector MBTiles exporter')
    sub = p.add_subparsers(dest='action')
    doctor = sub.add_parser('doctor',help='check packaged native runtime')
    doctor.add_argument('--runtime',type=Path)
    verify = sub.add_parser('verify',help='check PNG MBTiles integrity and tile coordinates')
    verify.add_argument('file',type=Path)
    vverify = sub.add_parser('verify-vector',help='check gzip MVT PBF MBTiles and metadata')
    vverify.add_argument('file',type=Path)
    cmp = sub.add_parser('compare',help='compare two raster MBTiles tile-for-tile')
    cmp.add_argument('reference',type=Path)
    cmp.add_argument('candidate',type=Path)
    cmp.add_argument('--pixels',action='store_true',help='compare decoded PNG pixels (Pillow required)')
    export = sub.add_parser('convert',help='convert an OSM PBF file to raster MBTiles')
    export.add_argument('pbf',type=Path)
    export.add_argument('-z','--zoom',default='0-15',help='zoom range, e.g. 0-15')
    export.add_argument('-o','--output',type=Path,required=True)
    export.add_argument('--tile-type',choices=('raster','vector','both'),default=None,
                        help='tile content to generate: raster PNG, vector MVT/PBF, or both')
    export.add_argument('--format',choices=('raster','vector','both'),default=None,
                        help='deprecated alias for --tile-type; maintained for older scripts')
    export.add_argument('--vector-import-profile',choices=('safe','fast'),default='safe',
                        help='vector OSM import: safe (default) or fast (4 threads, parallel indexes, no cluster)')
    export.add_argument('--vector-query-mode',choices=('baseline','cached','projected'),default='projected',
                        help='vector SQL: projected (default), baseline (reference), cached (experimental precompute)')
    export.add_argument('--vector-tiler',choices=('postgis','batch'),default='postgis',
                        help='postgis: original tile SQL (default); batch: experimental region SQL')
    export.add_argument('--vector-batch-side',type=int,default=2,
                        help='experimental batch tile block edge, 1..8 (default: 2)')
    export.add_argument('--bbox',help='west,south,east,north (otherwise from PBF header)')
    export.add_argument('--runtime',type=Path,help='prepared native runtime folder')
    export.add_argument('--workdir',type=Path,help='persist intermediate database for subsequent runs')
    export.add_argument('--force',action='store_true',help='replace existing output')
    export.add_argument('--dry-run',action='store_true',help='show planned tile counts without runtime')
    export.add_argument('--max-tiles',type=int,default=MAX_TILES,help='safety limit on selected tiles')
    export.add_argument('--buffer',type=int,default=128,help='Mapnik rendering buffer (pixels)')
    export.add_argument('--metatile',type=int,default=8,help='metatile edge length (tiles)')
    export.add_argument('--processes',type=int,default=min(4,os.cpu_count() or 1),help='parallel Mapnik rendering workers (default: up to 4)')
    export.add_argument('--raster-format',choices=('png','png256'),default=None,
                        help='raster image encoding (default: png); png256 is lossy')
    export.add_argument('--encoding',choices=('png','png256'),default=None,
                        help='deprecated alias for --raster-format')
    # Backwards-compatible one-command CLI without 'convert' keyword.
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] not in ('convert','doctor','verify','verify-vector','compare','-h','--help'):
        argv = ['convert',*argv]
    args = p.parse_args(argv)
    if args.action == 'convert':
        if args.tile_type is not None and args.format is not None and args.tile_type != args.format:
            p.error('--tile-type and deprecated --format must agree if both are supplied')
        if args.raster_format is not None and args.encoding is not None and args.raster_format != args.encoding:
            p.error('--raster-format and deprecated --encoding must agree if both are supplied')
        effective_type = args.tile_type or args.format or 'raster'
        if effective_type == 'vector' and args.raster_format is not None:
            p.error('--raster-format is not applicable to vector-only output')
        # Normalize once to preserve all existing internal render/import paths.
        args.tile_type = args.tile_type or args.format or 'raster'
        args.format = args.tile_type
        args.raster_format = args.raster_format or args.encoding or 'png'
        args.encoding = args.raster_format
    return args,p

def parse_zoom(text):
    try:
        a,b = text.split('-',1) if '-' in text else (text,text)
        lo,hi=int(a),int(b)
    except (TypeError, ValueError) as exc:
        raise ValueError('--zoom must look like 0-15') from exc
    if not 0 <= lo <= hi <= 22:
        raise ValueError('zoom range must be between 0 and 22')
    return lo,hi

def get_runtime(arg):
    home = os.environ.get('OSM2MBTILES_RUNTIME')
    default = Path(sys.executable).resolve().parent/'runtime' if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[2]/'runtime'
    return Runtime(Path(arg or home or default).expanduser().resolve())

def _job_key(pbf_digest,style,bbox,lo,hi,buffer,meta,encoding):
    # Full hashing only occurs after preflight, not in dry-run.
    fields = [pbf_digest,file_digest(style/'openstreetmap-carto-flex.lua'),
              file_digest(style/'project.xml'),str(bbox),str(lo),str(hi),str(buffer),str(meta),str(encoding)]
    return hashlib.sha256('|'.join(fields).encode()).hexdigest()

def _import_key(pbf_digest,style):
    files = [style/'openstreetmap-carto-flex.lua',style/'indexes.sql',
             style/'functions.sql',style/'common-values.sql']
    return hashlib.sha256('|'.join([pbf_digest] + [file_digest(p) for p in files]).encode()).hexdigest()

def _worker_path():
    # PyInstaller onefile also extracts package python sources under _MEIPASS.
    if getattr(sys,'frozen',False):
        return Path(sys._MEIPASS)/'osm2mbtiles'/'worker.py'
    return Path(__file__).with_name('worker.py')

def convert(args):
    pbf = args.pbf.expanduser().resolve()
    if not pbf.is_file():
        raise PipelineError(f'Input PBF not found: {pbf}')
    lo,hi = parse_zoom(args.zoom)
    if args.buffer < 0 or args.buffer > 1024 or not 1 <= args.metatile <= 32:
        raise PipelineError('--buffer must be 0..1024, --metatile 1..32')
    if not 1 <= args.processes <= 64:
        raise PipelineError('--processes must be 1..64')
    if not 1 <= args.vector_batch_side <= 8:
        raise PipelineError('--vector-batch-side must be 1..8')
    bbox = validate_bbox(tuple(float(x) for x in args.bbox.split(','))) if args.bbox else read_bbox(pbf)
    count = tile_count(bbox,lo,hi)
    if count > args.max_tiles:
        raise PipelineError(f'Too many tiles ({count:,} > {args.max_tiles:,}); set --max-tiles explicitly to allow')
    metas = sum(1 for _ in metatiles(bbox,lo,hi,args.metatile))
    print(f'bbox={bbox}, zoom={lo}-{hi}, tiles={count:,}, metatiles={metas:,}',flush=True)
    if args.dry_run:
        return
    out = args.output.expanduser().resolve()
    outputs = [out] if args.format != 'both' else [out.with_name(out.stem + '.raster.mbtiles'), out.with_name(out.stem + '.vector.mbtiles')]
    if not args.force and any(p.exists() for p in outputs):
        raise PipelineError('Output already exists; use --force to replace: ' + ', '.join(map(str,outputs)))
    runtime = get_runtime(args.runtime)
    runtime.validate()
    if args.workdir:
        work = args.workdir.expanduser().resolve()
        if work.is_dir() and any(work.iterdir()) and not (work/'.osm2mbtiles-workspace').is_file():
            raise PipelineError('--workdir is nonempty and not owned by osm2mbtiles')
        work.mkdir(parents=True,exist_ok=True)
        run_pipeline(pbf,out,bbox,lo,hi,args,work,runtime)
    else:
        with tempfile.TemporaryDirectory(prefix='osm2mbtiles-') as tmp:
            run_pipeline(pbf,out,bbox,lo,hi,args,Path(tmp),runtime)

def run_pipeline(pbf,out,bbox,lo,hi,args,work,runtime):
    # Persisted work directory never receives bare contents from other projects.
    work.mkdir(parents=True,exist_ok=True)
    (work/'.osm2mbtiles-workspace').touch(exist_ok=True)
    style = work / 'style'
    source_style = runtime.style
    style_inputs = ('project.xml','openstreetmap-carto-flex.lua','indexes.sql',
                    'functions.sql','common-values.sql')
    style_source_key = hashlib.sha256('|'.join(file_digest(source_style/f) for f in style_inputs).encode()).hexdigest()
    style_key_path = work/'.source-style-key'
    if style.is_dir() and (not style_key_path.is_file() or style_key_path.read_text() != style_source_key):
        shutil.rmtree(style)
    if not style.is_dir():
        shutil.copytree(source_style, style)
        style_key_path.write_text(style_source_key)
    # Personal temp key changes if a source/style changes. In that case, preserve logs
    # but discard previous private database before the new import.
    pbf_digest = file_digest(pbf)
    key = _import_key(pbf_digest,style)
    state_file = work/'import-state.json'
    previous = json.loads(state_file.read_text()) if state_file.is_file() else {}
    if (args.format != 'vector' or (previous.get('import_key') is not None and previous.get('import_key') != key)) and previous.get('import_key') != key and (work/'pgdata').exists():
        # Never delete an unrelated directory merely because the user points --workdir there.
        if not (work/'pgdata'/'PG_VERSION').exists() or not (work/'.osm2mbtiles-workspace').exists():
            raise PipelineError('Unsafe workspace: refusing to remove unowned PostgreSQL cluster')
        shutil.rmtree(work/'pgdata')
        (work/'vector-state.json').unlink(missing_ok=True)
    job_key = _job_key(pbf_digest,style,bbox,lo,hi,args.buffer,args.metatile,args.encoding)
    out.parent.mkdir(parents=True,exist_ok=True)
    partial = out.with_name(out.name+'.partial')
    if args.force and partial.exists():
        partial.unlink()
    raster = args.format in ('raster','both')
    vector = args.format in ('vector','both')
    raster_out = out.with_name(out.stem+'.raster.mbtiles') if args.format == 'both' else out
    vector_out = out.with_name(out.stem+'.vector.mbtiles') if args.format == 'both' else out
    job = dict(bbox=bbox,minzoom=lo,maxzoom=hi,mapfile=str(style/'project.xml'),
               partial=str(raster_out.with_name(raster_out.name+'.partial')),
               name=raster_out.stem,job_key=job_key,buffer=args.buffer,
               metatile=args.metatile,processes=args.processes,encoding=args.encoding,
               runtime=str(runtime.root),fonts=str(style/'fonts'))
    vector_dir = Path(__file__).resolve().parents[2]/'vector'
    if vector and not (vector_dir/'vector-flex.lua').is_file():
        raise PipelineError(f'Missing vector flex source: {vector_dir}')
    shared_key = hashlib.sha256((key + file_digest(vector_dir/'vector-flex.lua') +
        file_digest(vector_dir/'post-import.sql') + '|shared-v1|' + args.vector_import_profile).encode()).hexdigest() if raster and vector else None
    shared_state = work/'shared-import-state.json'
    shared_cached = json.loads(shared_state.read_text()) if shared_state.is_file() else {}
    with LocalDatabase(runtime,work) as db:
        if raster and vector and (shared_cached.get('key') != shared_key or previous.get('import_key') != key):
            print('[SHARED] Importing OSM once for Carto PNG and detailed PBF...',flush=True)
            db.import_shared_extract(pbf,style,vector_dir,args.vector_import_profile)
            db.prepare_external_data(style)
            shared_state.write_text(json.dumps({'key':shared_key}),encoding='utf-8')
            state_file.write_text(json.dumps({'import_key':key}),encoding='utf-8')
            (work/'vector-state.json').write_text(json.dumps({'import_key':hashlib.sha256((pbf_digest+file_digest(vector_dir/'vector-flex.lua')+file_digest(vector_dir/'post-import.sql')+'|import-profile='+args.vector_import_profile).encode()).hexdigest()}),encoding='utf-8')
        if raster and not vector:
            if previous.get('import_key') != key:
                print('Importing PBF into private PostGIS for raster...',flush=True)
                db.import_extract(pbf,style)
                db.prepare_external_data(style)
                state_file.write_text(json.dumps({'import_key':key},indent=2),encoding='utf-8')
                # Carto import drops gis. Invalidate any prior vector tables.
                (work/'vector-state.json').unlink(missing_ok=True)
                shared_state.unlink(missing_ok=True)
            else:
                print('Reusing prior raster import.',flush=True)
        if vector and not raster:
            vkey = hashlib.sha256((pbf_digest+file_digest(vector_dir/'vector-flex.lua')+
                     file_digest(vector_dir/'post-import.sql')+
                     '|import-profile='+args.vector_import_profile).encode()).hexdigest()
            vstate = work/'vector-state.json'
            cached = json.loads(vstate.read_text()) if vstate.is_file() else {}
            if cached.get('import_key') != vkey:
                print('[VECTOR] Importing full OSM tags into private vector tables...',flush=True)
                db.import_vector_extract(pbf,vector_dir,args.vector_import_profile)
                vstate.write_text(json.dumps({'import_key':vkey},indent=2),encoding='utf-8')
            else:
                print('[VECTOR] Reusing detailed vector import.',flush=True)
        if raster and vector:
            vkey = hashlib.sha256((pbf_digest+file_digest(vector_dir/'vector-flex.lua')+file_digest(vector_dir/'post-import.sql')+'|import-profile='+args.vector_import_profile).encode()).hexdigest()
        if raster:
            worker = _worker_path()
            if not worker.is_file():
                raise PipelineError('Bundled renderer worker missing')
            if args.force and Path(job['partial']).exists():
                Path(job['partial']).unlink()
            job_path = work/'render-job.json'
            job_path.write_text(json.dumps(job,indent=2),encoding='utf-8')
            print('Rendering metatiles into PNG MBTiles...',flush=True)
            run([runtime.python,worker,job_path],env=db.env,cwd=style)
            os.replace(job['partial'],raster_out)
            print(f'DONE Raster: {raster_out} ({raster_out.stat().st_size:,} bytes)',flush=True)
        if vector:
            # An optional on-disk property cache moves JSONB conversion from each
            # metatile query into one preparation pass over the imported OSM rows.
            if args.vector_query_mode == 'cached':
                cachekey = hashlib.sha256((vkey+'|cache-v1|'+file_digest(Path(__file__).with_name('vector.py'))).encode()).hexdigest()
                cache_state = work/'vector-cache-state.json'
                cache_meta = json.loads(cache_state.read_text()) if cache_state.is_file() else {}
                if cache_meta.get('key') != cachekey:
                    print('[VECTOR] Preparing per-object PostGIS property cache...',flush=True)
                    prep_env = db.env.copy()
                    prep_env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
                    run([runtime.python,'-m','osm2mbtiles.vector_cache','prepare'],env=prep_env)
                    cache_state.write_text(json.dumps({'key':cachekey}),encoding='utf-8')
                else:
                    print('[VECTOR] Reusing PostGIS property cache.',flush=True)
            # Invoke using the Mapnik-enabled system Python whose psycopg2 is
            # already installed by the WSL setup; vector renderer doesn't use Mapnik.
            vector_script = Path(__file__).with_name('vector_worker.py')
            vector_job = work/'vector-job.json'
            vector_job.write_text(json.dumps({
                'pbf':str(pbf),'output':str(vector_out),'bbox':bbox,'minzoom':lo,
                'maxzoom':hi,'processes':args.processes,
                'tiler':args.vector_tiler,'batch_side':args.vector_batch_side},indent=2),encoding='utf-8')
            vector_env=db.env.copy()
            vector_env['OSM2MBTILES_VECTOR_QUERY_MODE'] = args.vector_query_mode
            run([runtime.python,vector_script,vector_job],env=vector_env)
            print(f'DONE Vector: {vector_out} ({vector_out.stat().st_size:,} bytes)',flush=True)


def main(argv=None):
    args,parser=arguments(argv)
    started = time.perf_counter() if args.action == 'convert' else None
    try:
        if args.action == 'doctor':
            runtime=get_runtime(args.runtime)
            runtime.validate()
            print(f'Runtime layout OK: {runtime.root}')
        elif args.action == 'convert':
            convert(args)
        elif args.action == 'verify-vector':
            from .vector import verify as verify_vector
            print(json.dumps(verify_vector(args.file),indent=2))
        elif args.action == 'verify':
            print(json.dumps(inspect(args.file),indent=2))
        elif args.action == 'compare':
            result=compare(args.reference,args.candidate,pixels=args.pixels)
            print(json.dumps(result,indent=2))
            return 0 if result['equal'] else 3
        else:
            parser.print_help()
            return 0
        return 0
    except (PipelineError,PBFError,ValueError, OSError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        return 2
    finally:
        if started is not None:
            elapsed = time.perf_counter() - started
            h, remainder = divmod(int(elapsed), 3600)
            m, sec = divmod(remainder, 60)
            print(f'[TIME] Total convert elapsed: {elapsed:.3f}s ({h:02d}:{m:02d}:{sec:02d})',
                  flush=True)

if __name__ == '__main__':
    sys.exit(main())