"""One-pass grouped MVT aggregate SQL tests with exact legacy defaults."""
import pytest
from osm2mbtiles import vector_batch as v


@pytest.mark.parametrize("zoom,count",[(3,1),(12,4),(14,4),(16,4)])
def test_grouping_is_explicit_and_does_not_modify_legacy(zoom,count):
    legacy=v.batch_query(zoom,count)
    grouped=v.batch_query(zoom,count,grouped_aggregation=True)
    assert legacy.count("%s::integer")==grouped.count("%s::integer")==2*count
    assert "_mvt AS MATERIALIZED" not in legacy
    assert "_mvt AS MATERIALIZED" in grouped
    assert "GROUP BY f.x, f.y" in grouped
    assert "ROW(f.geom, f.properties)" in grouped
    assert "ST_AsMVT(q" in legacy
    assert "ST_AsMVT(ROW(" in grouped
    assert "ST_AsMVTGeom(s.geom" in legacy and "ST_AsMVTGeom(s.geom" in grouped
    assert "_src AS MATERIALIZED" in grouped and "_feat AS MATERIALIZED" in grouped


def test_grouped_prepared_sql_only_when_requested(monkeypatch):
    class Cursor:
        def __init__(self):
            self.statements=[]
        def execute(self, sql, params=None):
            self.statements.append(sql)
    cur=Cursor()
    monkeypatch.setattr(v,"_queries",{})
    monkeypatch.delenv("OSM2MBTILES_VECTOR_BATCH_GROUPED_MVT",raising=False)
    first,cmd=v._prepare_batch(cur,14,4)
    assert first=="mvt_z14_n4"
    monkeypatch.setenv("OSM2MBTILES_VECTOR_BATCH_GROUPED_MVT","1")
    second,cmd2=v._prepare_batch(cur,14,4)
    assert second=="mvt_grouped_z14_n4"
    assert cmd!=cmd2
    assert len(cur.statements)==2
    assert "ST_AsMVT(ROW(" not in cur.statements[0]
    assert "ST_AsMVT(ROW(" in cur.statements[1]
    v._prepare_batch(cur,14,4)
    assert len(cur.statements)==2


@pytest.mark.parametrize("invalid",["yes","2","-1","1;drop table",""])
def test_reject_unknown_grouped_sql_setting(monkeypatch,invalid):
    monkeypatch.setattr(v,"_queries",{})
    monkeypatch.setenv("OSM2MBTILES_VECTOR_BATCH_GROUPED_MVT",invalid)
    with pytest.raises(ValueError,match="grouped MVT"):
        v._prepare_batch(None,14,4)
