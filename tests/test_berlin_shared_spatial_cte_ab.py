"""Pure contracts for experimental same-DB Berlin CTE query generation."""
import pytest

from benchmarks.berlin_shared_spatial_cte_ab import (
    LO, HI, BBOX, PBF_SHA, query_shared_candidates, select_xyz,
)
from osm2mbtiles import vector


def test_pinned_dataset_and_sample_grid():
    assert LO == 12 and HI == 16
    assert BBOX == (13.088, 52.338, 13.761, 52.676)
    assert PBF_SHA == "51d3845df2e4b1334ebec7cf6516e41e71735ce2fc1c0975c52d57f58f1952ad"
    assert vector.tile_count(BBOX, LO, HI) == 16943
    xyz = select_xyz()
    assert len(xyz) == 25
    assert len(set(xyz)) == 25
    assert set(z for z, _, _ in xyz) == set(range(LO, HI + 1))


@pytest.mark.parametrize("zoom", range(LO, HI + 1))
def test_shared_candidate_query_preserves_layers_and_sql_parameters(zoom):
    base = vector.query_for_zoom(zoom, projected=True)
    candidate = query_shared_candidates(zoom, projected=True)
    assert base.count("%s") == candidate.count("%s") == 6
    assert candidate.count(" AS MATERIALIZED") >= 3
    assert candidate.count("ST_AsMVT(") == base.count("ST_AsMVT(")
    assert candidate.count("ST_AsMVTGeom(") == base.count("ST_AsMVTGeom(")
    assert candidate.count("UNION ALL") == base.count("UNION ALL")
    assert candidate.count("jsonb_object_agg") == base.count("jsonb_object_agg")
    assert "ST_TileEnvelope(%s, %s, %s)" in candidate
    assert "WHERE t.geom && b.search" in candidate
    # CTEs are a data-source substitution, not a different MVT encoder.
    assert "ST_AsMVT(q," in candidate


def test_no_unsupported_modes():
    with pytest.raises(ValueError, match="projected"):
        query_shared_candidates(12)
    with pytest.raises(ValueError, match="projected"):
        query_shared_candidates(12, cached=True, projected=True)
