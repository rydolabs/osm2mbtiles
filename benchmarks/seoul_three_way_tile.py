#!/usr/bin/env python3
"""Per-layer and per-feature analysis of identical Seoul z/x/y from three MBTiles."""
import collections
import gzip
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
import mapbox_vector_tile

Z, X, Y = 14, 13970, 6344
def analyze(path):
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        meta = dict(db.execute('SELECT name,value FROM metadata'))
        ntiles = db.execute('SELECT count(*) FROM tiles').fetchone()[0]
        data = db.execute('SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?',
                          (Z,X,(1<<Z)-1-Y)).fetchone()
        if data is None:
            raise RuntimeError(f'{path}: no data at z={Z}/x={X}/y={Y} using MBTiles TMS indexing')
    blob=data[0]
    raw=gzip.decompress(blob) if blob[:2]==b'\x1f\x8b' else blob
    layers=mapbox_vector_tile.decode(raw)
    result={'mbtiles_tiles':ntiles,'format':meta.get('format'),
            'tile_bytes':len(blob),'raw_bytes':len(raw),
            'raw_sha256':hashlib.sha256(raw).hexdigest(),
            'layers_count':len(layers),'features_count':0,'layers':{}}
    for name,layer in sorted(layers.items()):
        fs=layer['features']
        attr=collections.Counter(k for f in fs for k in f.get('properties',{}))
        types=collections.Counter(f.get('geometry',{}).get('type','unknown') for f in fs)
        samples=[]
        for f in fs[:3]:
            props=f.get('properties',{})
            samples.append({'id':str(f.get('id','')), 'type':f.get('geometry',{}).get('type'),
                            'class':props.get('class'), 'name':props.get('name'),
                            'property_keys':sorted(props)[:15]})
        result['layers'][name]={'features':len(fs),'geometry_types':dict(types),
                                'unique_attribute_keys':len(attr),
                                'top_attribute_keys':[k for k,_ in attr.most_common(16)],
                                'samples':samples}
        result['features_count']+=len(fs)
    assert result['features_count']>0, (path, result)
    return result

def main():
    dest=Path(sys.argv[1])
    engines={}
    for pair in sys.argv[2:]:
        name,file=pair.split('=',1)
        engines[name]=analyze(Path(file))
    report={'tile':{'z':Z,'x':X,'y':Y,'location':'Seoul City Hall',
                     'longitude':126.978,'latitude':37.5665},
            'engines':engines,
            'comparability':'Identical OSM PBF and z/x/y; distinct layer profiles, zoom filters, generalization and tags. Counts are NOT strict feature parity.'}
    dest.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    lines=['# Seoul City Hall: identical tile, 3 engines','',
           'XYZ: **14/13970/6344**, coordinate **126.978 E, 37.5665 N**. '
           'The same Seoul OSM PBF is supplied to each engine.','',
           '| Engine | Layer count | Feature count | Tile byte count |',
           '|---|---:|---:|---:|']
    for name,r in engines.items():
        lines.append(f"| {name} | {r['layers_count']} | {r['features_count']} | {r['tile_bytes']} |")
    lines += ['', '| Engine | Layer | Features | Key examples |','|---|---|---:|---|']
    for name,r in engines.items():
        for layer,info in r['layers'].items():
            lines.append(f"| {name} | {layer} | {info['features']} | {', '.join(info['top_attribute_keys'][:6])} |")
    lines+=['','**Interpretation:** profiles differ; these differences are not strict parity or proof of missing OSM data. '
             'The user-uploaded aaa.bin has no embedded z/x/y and refers to Kintex in Goyang, not Seoul City Hall.']
    (dest.parent/'summary.md').write_text('\n'.join(lines)+'\n')
    print('THREE_WAY_SEOUL_SAME_TILE',json.dumps({k:{'layers':v['layers_count'],'features':v['features_count']} for k,v in engines.items()}),flush=True)
if __name__=='__main__':
    main()
