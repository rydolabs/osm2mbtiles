"""Pure contracts for benchmark-only projected-property caching."""
import pytest

from benchmarks.berlin_inline_attr_cache_ab import (
    BBOX, HI, LO, TABLES, PBF_SHA, build_sql, candidate_sql, xyz_samples,
)
from osm2mbtiles import vector


def test_frozen_berlin_dataset():
    assert vector.tile_count(BBOX, LO, HI) == 16943
    assert len(PBF_SHA) == 64
    assert len(xyz_samples()) == 25
    assert len(set(xyz_samples())) == 25
    assert TABLES == vector.CACHE_TABLES


@pytest.mark.parametrize("zoom", range(LO, HI + 1))
def test_only_property_projection_changed(zoom):
    base=vector.query_for_zoom(zoom, projected=True)
    new=candidate_sql(zoom, projected=True)
    assert new.count("%s") == base.count("%s") == 6
    for token in ("ST_AsMVTGeom(", "ST_AsMVT(", "UNION ALL",
                  "t.geom && b.search"):
        assert new.count(token) == base.count(token)
    assert "jsonb_object_agg" in base
    assert "jsonb_object_agg" not in new
    assert "fast_vector_" not in new
    assert "bench_props_low" in new if zoom <= 12 else "bench_props_high" in new


def test_cache_builder_matches_exact_projected_field_allowlist():
    q=build_sql("vector_areas")
    assert "UPDATE vector_areas t" in q
    assert "jsonb_object_agg(p.key,p.value)" in q
    assert "jsonb_each(t.tags)" in q
    assert "bench_props_low" in q
    assert "bench_props_high" in q
    assert "'building:part'" in q
    assert "'name:ko'" in q
    with pytest.raises(ValueError, match="Unsafe"):
        build_sql("vector_areas; DROP TABLE vector_points")


def test_no_other_query_modes():
    with pytest.raises(ValueError, match="projected"):
        candidate_sql(12)
    with pytest.raises(ValueError, match="projected"):
        candidate_sql(12, cached=True, projected=True)
