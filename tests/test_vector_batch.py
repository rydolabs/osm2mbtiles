"""No-DB structural regression tests for region-batch SQL and XYZ coverage."""
from osm2mbtiles.vector import query_for_zoom, xyz_tasks
from osm2mbtiles.vector_batch import batch_query, microbatches


def test_microbatches_cover_exact_xyz_without_duplicates():
    bbox = (127.015, 37.485, 127.075, 37.535)
    baseline = list(xyz_tasks(bbox, 12, 15))
    for side in (1, 2, 4, 8):
        batch = [(z, x, y) for z, cells in microbatches(bbox, 12, 15, side)
                 for x, y in cells]
        assert len(batch) == len(set(batch))
        assert set(batch) == set(baseline)


def test_batch_sql_limits_inputs_and_projection():
    for z in (3, 8, 12, 13, 15):
        for count in (1, 4, 16):
            sql = batch_query(z, count)
            assert sql.count("%s::integer") == 2 * count
            assert "ST_TileEnvelope" in sql
            assert "ST_Envelope(ST_Collect(search))" in sql
            assert " AS pbf FROM b" in sql
            assert "FROM b ORDER BY b.y,b.x" in sql
            if z >= 12:
                assert "ST_AsMVTGeom(s.geom" in sql
                assert "ST_AsMVT(q" in sql
                assert "MATERIALIZED" in sql
                assert "SELECT f.geom, f.properties" in sql
            # Base MVT API must remain the same geometry envelope rules.
            old = query_for_zoom(z)
            assert "4096" in old and "4096" in sql
            assert "64.0 / 4096" in old and "64.0 / 4096" in sql


def test_invalid_batch_size_rejected():
    import pytest
    for n in (0, 65):
        with pytest.raises(ValueError):
            batch_query(15, n)
    for n in (0, 9):
        with pytest.raises(ValueError):
            list(microbatches((126, 37, 127, 38), 12, 12, side=n))


def test_cli_opt_in_and_default():
    from osm2mbtiles.cli import arguments
    base = ["convert", "sample.pbf", "-o", "sample.mbtiles", "--format", "vector"]
    a, _ = arguments(base)
    assert a.vector_tiler == "postgis"
    assert a.vector_batch_side == 2
    a, _ = arguments(base + ["--vector-tiler", "batch", "--vector-batch-side", "4"])
    assert a.vector_tiler == "batch"
    assert a.vector_batch_side == 4


def test_vector_worker_routes_explicit_batch_without_changing_default(monkeypatch):
    from osm2mbtiles import vector_worker
    from osm2mbtiles import vector_batch
    calls = []
    monkeypatch.setattr(vector_worker, "export",
                        lambda *args: calls.append(("original", args)))
    monkeypatch.setattr(vector_batch, "export_batch",
                        lambda *args, **kwargs: calls.append(("batch", args, kwargs)))
    job = {
        "pbf": "sample.pbf", "output": "sample.mbtiles",
        "bbox": [127.0, 37.0, 127.1, 37.1],
        "minzoom": 12, "maxzoom": 13, "processes": 4,
    }
    vector_worker.run_job(job)
    assert calls[-1][0] == "original"
    job["tiler"] = "batch"
    job["batch_side"] = 3
    vector_worker.run_job(job)
    assert calls[-1][0] == "batch" and calls[-1][2]["side"] == 3
