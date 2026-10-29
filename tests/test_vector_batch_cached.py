"""Prepared JSONB property caches for PostGIS Batch SQL (no live DB)."""
from osm2mbtiles import vector_batch as v


def test_cached_batch_retains_layer_geometry_and_tag_predicates():
    for z in (8, 12, 14, 16):
        before = v.batch_query(z, 4)
        cached = v.batch_query(z, 4, cached=True)
        assert before.count("%s::integer") == cached.count("%s::integer") == 8
        assert before.count("ST_AsMVTGeom(") == cached.count("ST_AsMVTGeom(")
        assert before.count("ST_AsMVT(q,") == cached.count("ST_AsMVT(q,")
        assert before.count("t.geom && r.geom") == cached.count("t.geom && r.geom")
        assert before.count("s.geom && b.search") == cached.count("s.geom && b.search")
        assert "FROM fast_vector_lines t" in cached
        assert "FROM fast_vector_areas t" in cached
        assert "jsonb_each(t.tags)" in before
        assert "jsonb_each(t.tags)" not in cached
        if z >= 13:
            assert "t.props_high" in cached and "t.props_low" not in cached
        else:
            assert "t.props_low" in cached and "t.props_high" not in cached
        for layer in ("road", "water", "boundary"):
            assert before.count(f"ST_AsMVT(q, '{layer}'") == cached.count(f"ST_AsMVT(q, '{layer}'")


def test_prepared_statement_cache_is_separate_for_attribute_mode(monkeypatch):
    class Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, query, params=None):
            self.statements.append(query)

    c = Cursor()
    monkeypatch.setattr(v, "_queries", {})
    monkeypatch.delenv("OSM2MBTILES_VECTOR_QUERY_MODE", raising=False)
    name1, execute1 = v._prepare_batch(c, 14, 4)
    assert name1 == "mvt_z14_n4"
    monkeypatch.setenv("OSM2MBTILES_VECTOR_QUERY_MODE", "cached")
    name2, execute2 = v._prepare_batch(c, 14, 4)
    assert name2 == "mvt_cached_z14_n4" and execute1 != execute2
    assert len(c.statements) == 2
    assert "FROM fast_vector_lines t" not in c.statements[0]
    assert "FROM fast_vector_lines t" in c.statements[1]
    assert "t.props_high" in c.statements[1]
    assert v._prepare_batch(c, 14, 4) == (name2, execute2)
    monkeypatch.setenv("OSM2MBTILES_VECTOR_QUERY_MODE", "projected")
    assert v._prepare_batch(c, 14, 4) == (name1, execute1)
    assert len(c.statements) == 2
