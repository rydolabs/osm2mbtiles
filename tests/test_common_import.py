"""Combined OSM Carto + detailed vector flex import regression tests."""
from pathlib import Path
from unittest.mock import Mock

import pytest
from osm2mbtiles.runtime import LocalDatabase, Runtime


def test_combined_flex_chains_callbacks_without_mutating_original(tmp_path):
    carto = tmp_path / 'openstreetmap-carto-flex.lua'
    vector = tmp_path / 'vector-flex.lua'
    carto.write_text('-- upstream source')
    vector.write_text('-- own vector source')
    s = LocalDatabase.combined_flex_source(carto, vector)
    assert 'dofile(' in s
    assert s.index(str(carto)) < s.index(str(vector))
    for kind in ('node', 'way', 'relation'):
        assert f'osm2pgsql.process_{kind} = function(object)' in s
        assert f'vector_{kind}(object); carto_{kind}(object)' in s
    assert 'Carto flex callbacks missing' in s


def test_combined_flex_escapes_non_ascii_and_quotes(tmp_path):
    carto = tmp_path / '한글"carto.lua'
    vector = tmp_path / 'vector.lua'
    text = LocalDatabase.combined_flex_source(carto, vector)
    assert str(carto) not in text  # must escape double quotes and non-ASCII
    assert '\\u' in text


def test_common_import_one_osm2pgsql_and_both_post_imports(tmp_path, monkeypatch):
    from osm2mbtiles import runtime as mod
    carto = tmp_path / 'style'; vector = tmp_path / 'vector'; carto.mkdir(); vector.mkdir()
    (carto/'openstreetmap-carto-flex.lua').write_text('-- mock')
    (vector/'vector-flex.lua').write_text('-- mock')
    commands = []
    monkeypatch.setattr(mod, 'run', lambda cmd, env=None, cwd=None: commands.append([str(x) for x in cmd]))
    db = LocalDatabase(Runtime(tmp_path),tmp_path/'work')
    db.psql = Mock()
    monkeypatch.setattr(db.runtime, 'bin', lambda name: Path('/test/bin')/name) if False else None
    # Use a mock runtime with valid path lookup and no PostgreSQL execution.
    class FakeRuntime:
        def bin(self, name): return Path('/test/bin')/name
        def env(self): return {}
    db = LocalDatabase(FakeRuntime(),tmp_path/'work')
    db.work.mkdir(exist_ok=True)
    db.psql = Mock()
    db.import_both_extract(tmp_path/'mock.osm.pbf',carto,vector,'safe')
    imports = [c for c in commands if 'osm2pgsql' in c[0]]
    assert len(imports)==1
    assert '--number-processes=1' in imports[0]
    assert '--disable-parallel-indexing' in imports[0]
    assert (tmp_path/'work/combined-carto-vector-flex.lua').is_file()
    assert [call.args[1] for call in db.psql.call_args_list if call.args[0]=='-f'] == [
        str(carto/'indexes.sql'),str(carto/'functions.sql'),
        str(carto/'common-values.sql'),str(vector/'post-import.sql')]


def test_combined_fast_keeps_original_carto_cluster_policy(tmp_path, monkeypatch):
    from osm2mbtiles import runtime as mod
    calls=[]
    monkeypatch.setattr(mod,'run',lambda cmd,env=None,cwd=None: calls.append([str(x) for x in cmd]))
    style=tmp_path/'style'; style.mkdir(); (style/'openstreetmap-carto-flex.lua').touch()
    vector=tmp_path/'vector';vector.mkdir();(vector/'vector-flex.lua').touch()
    class FakeRuntime:
        def bin(self,name):return Path('/test/bin')/name
        def env(self):return {}
    db=LocalDatabase(FakeRuntime(),tmp_path/'work');db.work.mkdir();db.psql=Mock()
    db.import_both_extract(tmp_path/'input.pbf',style,vector,'fast')
    cmd=next(c for c in calls if c[0].endswith('osm2pgsql'))
    assert '--number-processes=4' in cmd
    assert '--disable-parallel-indexing' not in cmd
    assert 'cluster = "no"' not in (tmp_path/'work/combined-carto-vector-flex.lua').read_text()


def test_both_cli_uses_one_import_and_reuses_common_state(tmp_path, monkeypatch):
    import json
    from .test_pipeline import make_runtime, sample_pbf, FAKE_MAPNIK
    from osm2mbtiles import cli
    from osm2mbtiles.runtime import LocalDatabase

    runtime = make_runtime(tmp_path/'runtime')
    pbf = sample_pbf(tmp_path/'source.pbf',(7.40,43.71,7.46,43.75))
    fake = tmp_path/'fake_python';fake.mkdir()
    (fake/'mapnik.py').write_text(FAKE_MAPNIK)
    monkeypatch.setenv('PYTHONPATH',str(fake))
    monkeypatch.setenv('OSM_FAKE_LOG',str(tmp_path/'fake-commands.log'))
    calls={'both':0,'raster':0,'vector':0}
    def common(db,pbf,style,vector,profile):calls['both']+=1
    def raster(db,pbf,style):calls['raster']+=1
    def vector(db,pbf,style,profile):calls['vector']+=1
    monkeypatch.setattr(LocalDatabase,'import_both_extract',common)
    monkeypatch.setattr(LocalDatabase,'import_extract',raster)
    monkeypatch.setattr(LocalDatabase,'import_vector_extract',vector)
    monkeypatch.setattr(LocalDatabase,'prepare_external_data',lambda *a:None)
    orig=cli.run
    def intercept(cmd,env=None,cwd=None):
        if len(cmd)>1 and str(cmd[1]).endswith('vector_worker.py'):
            job=json.loads(Path(cmd[2]).read_text());Path(job['output']).write_bytes(b'fake-vector')
            return
        return orig(cmd,env=env,cwd=cwd)
    monkeypatch.setattr(cli,'run',intercept)
    work=tmp_path/'work'
    for suffix in ('a','b'):
        output=tmp_path/f'{suffix}.mbtiles'
        assert cli.main([str(pbf),'-z','0','-o',str(output),'--format','both',
                         '--runtime',str(runtime),'--workdir',str(work)])==0
        assert (tmp_path/f'{suffix}.raster.mbtiles').exists()
        assert (tmp_path/f'{suffix}.vector.mbtiles').read_bytes()==b'fake-vector'
    assert calls=={'both':1,'raster':0,'vector':0}
    assert (work/'combined-import-state.json').is_file()