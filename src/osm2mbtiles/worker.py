"""Standalone Mapnik raster renderer; executed by the bundled Python interpreter.

Deliberately stdlib-only except mapnik. A bundled CPython/mapnik need not share the CLI's
Python ABI. Run: runtime/python/bin/python3 path/to/worker.py job.json
"""
from __future__ import annotations
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

TILE = 256
HALF = 20037508.342789244
MAXLAT = 85.0511287798066

def tile_xy(lon, lat, z):
    n = 1 << z
    lat = max(-MAXLAT, min(MAXLAT, lat))
    return (max(0,min(n-1, math.floor((lon+180)/360*n))),
            max(0,min(n-1, math.floor((1-math.asinh(math.tan(math.radians(lat)))/math.pi)*n/2))))

def bounds(bbox,z):
    w,s,e,n = bbox
    x0,y0 = tile_xy(w,n,z)
    x1,y1 = tile_xy(math.nextafter(e,-math.inf), math.nextafter(s,math.inf),z)
    return min(x0,x1),max(x0,x1),min(y0,y1),max(y0,y1)

def tasks(job):
    size = int(job.get('metatile',8))
    for z in range(job['minzoom'],job['maxzoom']+1):
        xmin,xmax,ymin,ymax = bounds(job['bbox'],z)
        for my in range((ymin//size)*size,ymax+1,size):
            for mx in range((xmin//size)*size,xmax+1,size):
                yield (z,mx,my,min(size,(1<<z)-mx),min(size,(1<<z)-my),
                       max(mx,xmin),min(mx+size-1,xmax),max(my,ymin),min(my+size-1,ymax))

def create_db(db,job):
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER, tile_column INTEGER,
           tile_row INTEGER, tile_data BLOB NOT NULL,
           PRIMARY KEY(zoom_level,tile_column,tile_row));
    ''')
    saved = db.execute("SELECT value FROM metadata WHERE name='osm2mbtiles_job_key'").fetchone()
    if saved is not None and saved[0] != job['job_key']:
        raise ValueError('Partial MBTiles belongs to a different job; delete it or use matching inputs')
    w,s,e,n = job['bbox']
    meta = dict(name=job['name'],type='baselayer',format='png',version='1.3',
                minzoom=str(job['minzoom']),maxzoom=str(job['maxzoom']),
                bounds=f'{w},{s},{e},{n}',center=f'{(w+e)/2},{(s+n)/2},{job["minzoom"]}',
                attribution='© OpenStreetMap contributors',osm2mbtiles_job_key=job['job_key'])
    db.executemany('INSERT OR REPLACE INTO metadata(name,value) VALUES(?,?)',meta.items())
    db.commit()

def render(job):
    import mapnik
    runtime = Path(job['runtime'])
    fonts = Path(job.get('fonts',str(runtime/'fonts')))
    if fonts.is_dir() and hasattr(mapnik,'register_fonts'):
        # Python mapnik.register_fonts(path, valid_extensions=[...]) already
        # recurses into subdirectories; the second argument is NOT a Boolean.
        # Passing True breaks Python Mapnik 3.x (TypeError: bool not iterable).
        mapnik.register_fonts(str(fonts))
    plugins = runtime/'lib'/'mapnik'/'input'
    if plugins.is_dir() and hasattr(mapnik,'register_datasources'):
        mapnik.register_datasources(str(plugins))
    path = Path(job['partial'])
    path.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path) as db:
        create_db(db,job)
        mp = mapnik.Map(TILE,TILE)
        mapnik.load_map(mp,job['mapfile'],True)
        if '3857' not in mp.srs and '+proj=merc' not in mp.srs:
            raise ValueError(f'Expected Web Mercator style; got {mp.srs}')
        if mp.buffer_size == 0:
            mp.buffer_size = int(job['buffer'])
        completed = db.execute('SELECT COUNT(*) FROM tiles').fetchone()[0]
        count = 0
        for z,mx,my,mw,mh,xmin,xmax,ymin,ymax in tasks(job):
            coords = [(x,y,(1<<z)-1-y) for y in range(ymin,ymax+1)
                      for x in range(xmin,xmax+1)]
            missing = [p for p in coords if db.execute(
                'SELECT 1 FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?',
                (z,p[0],p[2])).fetchone() is None]
            if not missing:
                continue
            mp.resize(mw*TILE,mh*TILE)
            step = 2*HALF/(1<<z)
            bbox = mapnik.Box2d(-HALF+mx*step,HALF-(my+mh)*step,
                                -HALF+(mx+mw)*step,HALF-my*step)
            mp.zoom_to_box(bbox)
            im = mapnik.Image(mw*TILE,mh*TILE)
            mapnik.render(mp,im)
            for x,y,tms_y in missing:
                png = im.view((x-mx)*TILE,(y-my)*TILE,TILE,TILE).tostring('png')
                db.execute('INSERT INTO tiles VALUES (?,?,?,?)',(z,x,tms_y,png))
            count += 1
            completed += len(missing)
            if count % 16 == 0:
                db.commit()
                print(f'METATILES={count} TILES={completed}',flush=True)
        db.commit()
        # Complete database is checkpointed and switched back to single-file journal mode.
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db.execute('PRAGMA journal_mode=DELETE')
        result = db.execute('PRAGMA integrity_check').fetchone()[0]
        if result != 'ok':
            raise RuntimeError('MBTiles integrity_check failed: '+result)
        print(f'COMPLETE TILES={completed}',flush=True)

if __name__ == '__main__':
    try:
        with open(sys.argv[1],encoding='utf-8') as f:
            render(json.load(f))
    except Exception as exc:
        print(f'ERROR: {exc}',file=sys.stderr,flush=True)
        raise
