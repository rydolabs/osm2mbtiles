"""PostGIS feature CTE inlining A/B tests (before real-data exact parity)."""
import pytest
from osm2mbtiles import vector_batch as v


@pytest.mark.parametrize("zoom,count", [(3,1),(12,4),(14,3),(16,4)])
def test_inline_only_changes_feature_cte_materialization_hint(zoom, count):
    before = v.batch_query(zoom, count)
    after = v.batch_query(zoom, count, inline_features=True)
    assert before != after
    assert before.replace("_feat AS MATERIALIZED", "_feat AS NOT MATERIALIZED") == after
    assert "_src AS MATERIALIZED" in after
    assert "_feat AS NOT MATERIALIZED" in after
    assert "ST_AsMVTGeom" in before and "ST_AsMVTGeom" in after
    assert before.count("%s::integer") == after.count("%s::integer") == 2 * count


def test_prepare_mode_switch_has_independent_sql_cache(monkeypatch):
    class Cursor:
        statements = []
        def execute(self, sql, params=None):
            self.statements.append(sql)

    cursor = Cursor()
    monkeypatch.setattr(v, "_queries", {})
    monkeypatch.setenv("OSM2MBTILES_VECTOR_BATCH_FEATURE_CTE", "materialized")
    base_name, base_exec = v._prepare_batch(cursor, 14, 4)
    monkeypatch.setenv("OSM2MBTILES_VECTOR_BATCH_FEATURE_CTE", "inline")
    name, command = v._prepare_batch(cursor, 14, 4)
    assert name == "mvt_inline_z14_n4"
    assert command.startswith("EXECUTE mvt_inline_z14_n4 (")
    assert base_name == "mvt_z14_n4" and base_exec.startswith("EXECUTE mvt_z14_n4")
    assert len(cursor.statements) == 2
    assert "_feat AS MATERIALIZED" in cursor.statements[0]
    assert "_feat AS NOT MATERIALIZED" in cursor.statements[1]
    v._prepare_batch(cursor, 14, 4)
    assert len(cursor.statements) == 2


def test_unknown_cte_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("OSM2MBTILES_VECTOR_BATCH_FEATURE_CTE", "unknown")
    monkeypatch.setattr(v, "_queries", {})
    with pytest.raises(ValueError, match="Invalid"):
        v._prepare_batch(None, 14, 4)
