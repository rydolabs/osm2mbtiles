"""Unit tests for EXPLAIN-guided, narrow feature CTE opt-in."""
import pytest

from osm2mbtiles import vector_batch as v


@pytest.mark.parametrize("z,count", [(8,1),(12,4),(13,4),(14,4),(15,2),(16,4)])
def test_hot_cte_changes_only_road_z12_and_building_z14(z,count):
    original = v.batch_query(z,count)
    hot = v.batch_query(z,count,inline_hot_features=True)
    assert original.count("%s::integer") == hot.count("%s::integer") == 2*count
    if z in (12,14):
        assert hot.count("_feat AS NOT MATERIALIZED") == 1
        assert hot.count("_feat AS MATERIALIZED") == original.count("_feat AS MATERIALIZED") - 1
        assert hot.replace("_feat AS NOT MATERIALIZED", "_feat AS MATERIALIZED") == original
        assert ("road" if z == 12 else "building") in hot
    else:
        assert original == hot


def test_prepare_query_cache_keys_distinguish_hot_and_default(monkeypatch):
    class Cursor:
        statements = []
        def execute(self, statement, params=None):
            self.statements.append(statement)

    cursor = Cursor()
    monkeypatch.setattr(v, "_queries", {})
    monkeypatch.delenv("OSM2MBTILES_VECTOR_BATCH_INLINE_HOT", raising=False)
    name0, cmd0 = v._prepare_batch(cursor,14,4)
    assert name0 == "mvt_z14_n4"
    monkeypatch.setenv("OSM2MBTILES_VECTOR_BATCH_INLINE_HOT","1")
    name1, cmd1 = v._prepare_batch(cursor,14,4)
    assert name1 == "mvt_hot_z14_n4"
    assert len(cursor.statements) == 2
    assert "_feat AS NOT MATERIALIZED" not in cursor.statements[0]
    assert "_feat AS NOT MATERIALIZED" in cursor.statements[1]
    v._prepare_batch(cursor,14,4)
    assert len(cursor.statements) == 2
    assert cmd0 != cmd1


@pytest.mark.parametrize("invalid",["yes","true","2","1;drop table tiles","-1"])
def test_reject_invalid_hot_cte_settings(monkeypatch, invalid):
    monkeypatch.setattr(v, "_queries", {})
    monkeypatch.setenv("OSM2MBTILES_VECTOR_BATCH_INLINE_HOT",invalid)
    with pytest.raises(ValueError,match="inline-hot"):
        v._prepare_batch(None,14,4)
