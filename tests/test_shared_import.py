from pathlib import Path
from osm2mbtiles.runtime import LocalDatabase, Runtime

def test_shared_import_single_osm2pgsql_and_both_postprocess(monkeypatch,tmp_path):
    style=tmp_path/'style'; vector=tmp_path/'vector'; style.mkdir();vector.mkdir()
    (style/'openstreetmap-carto-flex.lua').write_text('function osm2pgsql.process_node(o) end\n')
    for name in ('indexes.sql','functions.sql','common-values.sql'):(style/name).write_text('SELECT 1;')
    (vector/'vector-flex.lua').write_text(('local x=osm2pgsql.define_node_table("test", {\n})\n')*4)
    (vector/'post-import.sql').write_text('SELECT 1;')
    db=LocalDatabase(Runtime(tmp_path/'runtime'),tmp_path/'work');db.work.mkdir()
    commands=[]; sql=[]
    monkeypatch.setattr('osm2mbtiles.runtime.run',lambda cmd,env=None,cwd=None:commands.append([str(x) for x in cmd]))
    monkeypatch.setattr(db,'psql',lambda *a,**k:sql.append(a))
    monkeypatch.setattr(Runtime,'bin',lambda self,name:tmp_path/name)
    db.import_shared_extract(tmp_path/'input.pbf',style,vector,'safe')
    assert len([x for x in commands if x[0].endswith('osm2pgsql')])==1
    assert len([x for x in commands if x[0].endswith('createdb')])==1
    assert sum(a[0]=='-f' for a in sql)==4
    combined=(db.work/'combined-carto-vector-flex.lua').read_text()
    assert 'carto_node(o)' in combined and 'vector_node(o)' in combined
    assert '--disable-parallel-indexing' in commands[-1]
    commands.clear()
    db.import_shared_extract(tmp_path/'input.pbf',style,vector,'fast')
    assert '--number-processes=4' in commands[-1]
    assert 'cluster = "no"' in (db.work/'combined-carto-vector-flex.lua').read_text()