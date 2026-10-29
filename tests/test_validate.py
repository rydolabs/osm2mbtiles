import sqlite3
from pathlib import Path
import pytest
from osm2mbtiles.validate import inspect,compare,ValidationError
from osm2mbtiles.cli import main

def make_mbtiles(path, tiles):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE metadata(name TEXT PRIMARY KEY,value TEXT)')
        db.execute('INSERT INTO metadata VALUES (?,?)',('format','png'))
        db.execute('CREATE TABLE tiles(zoom_level INT,tile_column INT,tile_row INT,tile_data BLOB)')
        db.executemany('INSERT INTO tiles VALUES(?,?,?,?)',tiles)
    return path

PNG=b'\x89PNG\r\n\x1a\n'+b'dummy-payload'

def test_verify_and_compare(tmp_path,capsys):
    a=make_mbtiles(tmp_path/'a.mbtiles',[(0,0,0,PNG)])
    b=make_mbtiles(tmp_path/'b.mbtiles',[(0,0,0,PNG)])
    assert inspect(a)['tiles']==1
    assert compare(a,b)['equal'] is True
    assert main(['verify',str(a)])==0
    assert main(['compare',str(a),str(b)])==0
    with sqlite3.connect(b) as db:
        db.execute('INSERT INTO tiles VALUES (?,?,?,?)',(1,0,0,PNG))
    assert main(['compare',str(a),str(b)])==3
    assert compare(a,b)['missing_in_first']==1

def test_bad_data(tmp_path):
    a=make_mbtiles(tmp_path/'bad.mbtiles',[(0,0,0,b'not-png')])
    with pytest.raises(ValidationError,match='PNG'):
        inspect(a)

def test_pixel_comparison_ignores_png_compression(tmp_path):
    from PIL import Image
    import io
    image=Image.new('RGBA',(256,256),(100,0,50,255))
    a_png=io.BytesIO();b_png=io.BytesIO()
    image.save(a_png,'PNG',compress_level=0)
    image.save(b_png,'PNG',compress_level=9)
    assert a_png.getvalue()!=b_png.getvalue()
    a=make_mbtiles(tmp_path/'raw.mbtiles',[(0,0,0,a_png.getvalue())])
    b=make_mbtiles(tmp_path/'compressed.mbtiles',[(0,0,0,b_png.getvalue())])
    assert not compare(a,b)['equal']
    assert compare(a,b,pixels=True)['equal']
    changed=Image.new('RGBA',(256,256),(101,0,50,255))
    bb=io.BytesIO();changed.save(bb,'PNG')
    with sqlite3.connect(b) as conn:
        conn.execute('UPDATE tiles SET tile_data=?',(bb.getvalue(),))
    assert not compare(a,b,pixels=True)['equal']
