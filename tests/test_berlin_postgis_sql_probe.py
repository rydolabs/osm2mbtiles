"""Test byte-parity guardrails and sample selection for frozen Berlin SQL probe."""
import importlib.util
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location(
    "berlin_postgis_sql_probe",ROOT/"benchmarks/berlin_postgis_sql_probe.py")
probe=importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize("zoom",range(12,17))
def test_server_prepare_uses_identical_source_sql_with_six_typed_params(zoom):
    raw=probe.vector.query_for_zoom(zoom,projected=True)
    prepared=probe.prepared_sql(zoom)
    assert raw.count("%s")==6
    assert prepared.count("%s")==0
    restored=prepared
    for index in range(1,7):
        assert "$"+str(index) in restored
        restored=restored.replace("$"+str(index),"%s")
    assert restored==raw


def test_frozen_berlin_sample_is_stratified_across_zooms():
    tasks=probe.stratified_tasks()
    assert len(tasks)>500
    assert len(tasks)==len(set(tasks))
    assert {z for z,_,_ in tasks}==set(probe.PILOT_BY_ZOOM)
    assert probe.vector.tile_count(probe.BBOX,probe.LO,probe.HI)==16943
    assert probe.PBF_SHA=="51d3845df2e4b1334ebec7cf6516e41e71735ce2fc1c0975c52d57f58f1952ad"


def test_prepared_query_refuses_unexpected_source_placeholder_count(monkeypatch):
    monkeypatch.setattr(probe.vector,"query_for_zoom",lambda z,projected: "SELECT %s")
    with pytest.raises(RuntimeError,match="placeholder"):
        probe.prepared_sql(12)
