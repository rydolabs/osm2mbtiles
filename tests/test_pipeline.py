import io
import os
import sqlite3
import sys
import textwrap
from pathlib import Path
from PIL import Image
from osm2mbtiles.cli import main
from .test_pbf import sample_pbf

BIN_SCRIPT='''#!/usr/bin/env python3
import os,sys
from pathlib import Path
with open(os.environ['OSM_FAKE_LOG'],'a') as f:f.write(Path(sys.argv[0]).name+' '+ ' '.join(sys.argv[1:])+'\\n')
if Path(sys.argv[0]).name=='initdb':
    p=Path(sys.argv[sys.argv.index('-D')+1]);p.mkdir(parents=True,exist_ok=True);(p/'PG_VERSION').write_text('17')
'''

FAKE_MAPNIK='''
import struct, zlib
def chunk(tag,payload):
    return struct.pack('>I',len(payload))+tag+payload+struct.pack('>I',zlib.crc32(tag+payload)&0xffffffff)
def png(w,h,r,g,b):
    rows=(b'\\x00'+bytes((r,g,b))*w)*h
    return b'\\x89PNG\\r\\n\\x1a\\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',w,h,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b'')
class Map:
    def __init__(self,w,h):
        self.srs='+proj=merc +datum=WGS84';self.buffer_size=0
    def resize(self,w,h):pass
    def zoom_to_box(self,bbox):pass
class Box2d:
    def __init__(self,*a):pass
class Image:
    def __init__(self,w,h):pass
    def view(self,x,y,w,h):return View(x,y,w,h)
class View:
    def __init__(self,x,y,w,h):self.x,self.y,self.w,self.h=x,y,w,h
    def tostring(self,fmt):
        return png(self.w,self.h,self.x%255,self.y%255,129)
def render(mp,img):pass
def load_map(mp,path,strict):pass
'''

def make_runtime(path):
    for directory in ('bin','python/bin','styles/osm-carto/scripts'):
        (path/directory).mkdir(parents=True,exist_ok=True)
    for name in ('initdb','pg_ctl','createdb','psql','osm2pgsql'):
        f=path/'bin'/name;f.write_text(BIN_SCRIPT);f.chmod(0o755)
    (path/'python/bin/python3').symlink_to(sys.executable)
    style=path/'styles/osm-carto'
    for name in ('project.xml','openstreetmap-carto-flex.lua','indexes.sql','functions.sql','common-values.sql'):
        (style/name).write_text('synthetic test fixture')
    for name in ('get-external-data.py','get-fonts.py'):
        (style/'scripts'/name).write_text('print("fake asset preparation")')
    return path

def test_full_mocked_pipeline(tmp_path,monkeypatch):
    runtime=make_runtime(tmp_path/'runtime')
    pbf=sample_pbf(tmp_path/'region.pbf',(-.1,-.1,.1,.1))
    fake_module=tmp_path/'fake_python';fake_module.mkdir()
    (fake_module/'mapnik.py').write_text(FAKE_MAPNIK)
    log=tmp_path/'commands.log'
    monkeypatch.setenv('OSM_FAKE_LOG',str(log))
    monkeypatch.setenv('PYTHONPATH',str(fake_module))
    out=tmp_path/'region.mbtiles'
    work=tmp_path/'persistent-workdir'
    args=[str(pbf),'-z','0-2','-o',str(out),'--runtime',str(runtime),'--workdir',str(work)]
    assert main(args)==0
    assert out.is_file()
    with sqlite3.connect(out) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert conn.execute('SELECT COUNT(*) FROM tiles').fetchone()[0]==9
        assert conn.execute("SELECT value FROM metadata WHERE name='format'").fetchone()[0]=='png'
        tiles=conn.execute('SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles').fetchall()
        assert {(z,x,y) for z,x,y,_ in tiles if z==2}=={(2,x,y) for x in (1,2) for y in (1,2)}
        for _,_,_,blob in tiles:
            assert Image.open(io.BytesIO(blob)).size==(256,256)
    recorded=log.read_text()
    assert recorded.count('initdb ')==1
    assert recorded.count('osm2pgsql ')==1
    assert recorded.count('pg_ctl ')==2
    assert not out.with_name(out.name+'.partial').exists()
    # The same PBF and DB should skip the import, but generate an additional final file.
    other=tmp_path/'second.mbtiles'
    args[args.index(str(out))]=str(other)
    assert main(args)==0
    assert log.read_text().count('osm2pgsql ')==1
    assert other.exists()


def test_dry_run_requires_no_runtime(tmp_path,capsys):
    path=sample_pbf(tmp_path/'pbf.osm.pbf',(-.1,-.1,.1,.1))
    assert main([str(path),'-z','0-2','-o',str(tmp_path/'out.mbtiles'),'--dry-run'])==0
    assert 'tiles=9' in capsys.readouterr().out


def test_existing_output_guard(tmp_path,monkeypatch,capsys):
    pbf=sample_pbf(tmp_path/'pbf.osm.pbf',(-.1,-.1,.1,.1))
    out=tmp_path/'out.mbtiles';out.write_bytes(b'keep')
    assert main([str(pbf),'-z','0','-o',str(out)])==2
    assert out.read_bytes()==b'keep'
