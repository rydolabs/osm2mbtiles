"""Vector tile schema and MBTiles validation without external PostGIS."""
import gzip
import json
import sqlite3
from pathlib import Path
from osm2mbtiles.vector import (LAYERS, LAYER_NAMES, metadata, layer_specs,
                                  query_for_zoom, xyz_tasks, create_db, verify)
from osm2mbtiles.cli import main
from .test_pbf import sample_pbf


def test_schema_layer_coverage():
    expected = {'road','pathway','railway','building','building_part',
                'water','waterway','landuse','landcover','poi','place',
                'boundary','housenumber','leisure','aeroway','transport'}
    assert expected.issubset(LAYER_NAMES)
    assert len(LAYER_NAMES) >= 16
    assert len(LAYERS) > len(LAYER_NAMES) # point + polygon POIs


def test_zoom_filters_are_monotonic():
    for z in range(0,16):
        specs = list(layer_specs(z))
        if z < 13: assert not any(n == 'building' for n,*_ in specs)
        if z >= 13: assert any(n == 'building' for n,*_ in specs)
        assert len(specs) >= len(list(layer_specs(z-1))) if z>0 else True
    assert any(n == 'road' for n,*_ in layer_specs(6))
    assert not any(n == 'pathway' for n,*_ in layer_specs(11))
    assert any(n == 'pathway' for n,*_ in layer_specs(12))


def test_sql_uses_index_and_preserves_attributes():
    sql = query_for_zoom(15)
    assert 'ST_TileEnvelope(%s, %s, %s)' in sql
    assert 't.geom && b.search' in sql
    assert 'ST_AsMVTGeom(' in sql and 'ST_AsMVT(q,' in sql
    for item in ("'maxspeed'", "'building:levels'", "'name:ko'", "'opening_hours'", "'surface'", "'osm_id'"):
        assert item in sql
    assert sql.count('FROM vector_') >= 16


def test_low_zoom_attribute_budget():
    small = query_for_zoom(8)
    assert "'maxspeed'" not in small
    assert "'name:ko'" in small
    assert "'road'" in small
    assert "'building'" not in small


def test_xyz_tasks_match_counts():
    bounds=(127.0,36.9,127.1,37.1)
    tiles=list(xyz_tasks(bounds,0,9))
    assert len(tiles) == len(set(tiles))
    assert all(0<=x<2**z and 0<=y<2**z for z,x,y in tiles)


def test_metadata_and_empty_mbtiles(tmp_path):
    m=metadata('monaco', (7.4,43.7,7.5,43.8),8,15)
    assert m['format']=='pbf'
    obj=json.loads(m['json'])
    assert len(obj['vector_layers'])>=16
    assert obj['vector_layers'][0]['id']=='water'
    assert obj['vector_layers'][0]['fields']['name:ko']=='String'
    p=tmp_path/'test.mbtiles'
    with sqlite3.connect(p) as conn:
        create_db(conn,m)
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert conn.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    info=verify(p)
    assert info['format']=='pbf'
    assert info['tiles']==0


def test_vector_cli_dry_run(tmp_path,capsys):
    pbf=sample_pbf(tmp_path/'test.osm.pbf',(-.1,-.1,.1,.1))
    assert main([str(pbf),'-z','0-4','-o',str(tmp_path/'map.mbtiles'),
                 '--format','vector','--dry-run'])==0
    assert 'tiles=' in capsys.readouterr().out


def test_vector_verifier_rejects_wrong_format(tmp_path):
    p=tmp_path/'bad.mbtiles'
    with sqlite3.connect(p) as conn:
        create_db(conn,metadata('test',(0,0,1,1),0,1))
        conn.execute("UPDATE metadata SET value='png' WHERE name='format'")
        conn.commit()
    try:
        verify(p)
        assert False, 'must reject PNG metadata'
    except ValueError as exc:
        assert 'Not a vector' in str(exc)


def test_postgres_jsonb_function_arg_limit():
    # PostgreSQL rejects jsonb_build_object calls above 100 args.
    detailed = query_for_zoom(15)
    for fragment in detailed.split('jsonb_build_object(')[1:]:
        arguments = fragment.split(')',1)[0]
        assert arguments.count("t.tags->>") <= 49
    assert " || jsonb_strip_nulls(jsonb_build_object(" in detailed


def test_no_duplicate_named_mvt_layers():
    sql=query_for_zoom(15)
    for name in ('poi','housenumber'):
        assert sql.count(f"ST_AsMVT(q, '{name}'") == 1
    assert ' UNION ALL ' in sql


def test_high_zoom_attributes_include_detailed_road_and_building_fields():
    sql = query_for_zoom(15)
    for field in ('maxspeed','lanes','surface','building:levels',
                  'addr:housenumber','opening_hours','name:ko'):
        assert f"'{field}'" in sql
    # One MVT layer per logical name, including multi-source labels.
    for name in LAYER_NAMES:
        assert sql.count(f"ST_AsMVT(q, '{name}'") == 1


def test_precomputed_query_same_source_filters_and_layers():
    base=query_for_zoom(15)
    cache=query_for_zoom(15, cached=True)
    assert base.count('ST_AsMVT(q,') == cache.count('ST_AsMVT(q,')
    assert base.count('t.geom && b.search') == cache.count('t.geom && b.search')
    assert 'FROM fast_vector_lines t' in cache
    assert 'FROM fast_vector_areas t' in cache
    assert 't.props_high' in cache
    assert "'maxspeed'" in base
    assert "t.tags->>'maxspeed'" not in cache


def test_cache_query_zoom_low_props():
    cache=query_for_zoom(8,cached=True)
    assert 't.props_low' in cache
    assert 't.props_high' not in cache
    assert 'FROM fast_vector_lines t' in cache



def test_projected_query_preserves_filtering_and_attributes():
    for z in (8,12,15):
        baseline=query_for_zoom(z)
        projected=query_for_zoom(z,projected=True)
        assert baseline.count('ST_AsMVT(q,') == projected.count('ST_AsMVT(q,')
        assert baseline.count('FROM vector_') == projected.count('FROM vector_')
        assert baseline.count('t.geom && b.search') == projected.count('t.geom && b.search')
        assert 'jsonb_each(t.tags)' in projected
        assert 'jsonb_object_agg' in projected
        assert "'name:ko'" in projected
        if z==15: assert "'opening_hours'" in projected


def test_vector_query_default_is_projected():
    from osm2mbtiles.cli import arguments
    args,_=arguments(['test.osm.pbf','-o','test.mbtiles','--format','vector'])
    assert args.vector_query_mode == 'projected'
    args,_=arguments(['test.osm.pbf','-o','test.mbtiles','--format','vector',
                       '--vector-query-mode','baseline'])
    assert args.vector_query_mode == 'baseline'


def test_import_profile_config_and_lua_style(monkeypatch, tmp_path):
    from osm2mbtiles.runtime import LocalDatabase, Runtime, PipelineError
    vector = tmp_path/'vector'
    vector.mkdir()
    (vector/'vector-flex.lua').write_text(('local a = define({\n})\n')*4)
    (vector/'post-import.sql').write_text('SELECT 1;')
    commands = []
    monkeypatch.setattr('osm2mbtiles.runtime.run', lambda cmd, env=None, cwd=None: commands.append([str(v) for v in cmd]))
    db = LocalDatabase(Runtime(tmp_path/'runtime'),tmp_path/'work')
    db.work.mkdir()
    monkeypatch.setattr(db,'ensure_gis_database',lambda:None)
    monkeypatch.setattr(db,'psql',lambda *args,**kwargs:None)
    monkeypatch.setattr(Runtime,'bin',lambda self,name: tmp_path/name)
    db.import_vector_extract(tmp_path/'map.pbf',vector,'safe')
    assert '--number-processes=1' in commands[0]
    assert '--disable-parallel-indexing' in commands[0]
    commands.clear()
    db.import_vector_extract(tmp_path/'map.pbf',vector,'fast')
    assert '--number-processes=4' in commands[0]
    assert '--disable-parallel-indexing' not in commands[0]
    assert 'cluster = "no"' in (db.work/'vector-fast-flex.lua').read_text()
    assert 'cluster' not in (vector/'vector-flex.lua').read_text()
    import pytest
    with pytest.raises(PipelineError):
        db.import_vector_extract(tmp_path/'map.pbf',vector,'invalid')

def test_rock_landcover_and_legal_boundary_layer():
    from osm2mbtiles.vector import layer_specs, query_for_zoom, LAYER_NAMES
    layers = {name: (table, predicate) for name, table, _id, predicate in layer_specs(14)}
    assert "rock" in layers["landcover"][1]
    assert layers["boundary"][0] == "vector_relations"
    assert "administrative" in layers["boundary"][1]
    assert layers["boundary_legal"][0] == "vector_lines"
    assert "legal" in layers["boundary_legal"][1]
    assert "boundary_legal" in LAYER_NAMES
    sql = query_for_zoom(14)
    assert "ST_AsMVT(q, 'boundary_legal'" in sql
    assert "FROM vector_lines t" in sql
