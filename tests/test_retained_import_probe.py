"""Safety tests for *benchmark-only* initial importer flag experiments."""
import importlib.util
from pathlib import Path
import pytest

path=Path(__file__).resolve().parents[1]/"benchmarks"/"retained_import_probe.py"
spec=importlib.util.spec_from_file_location("retained_import_probe",path)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

ORIG=["osm2pgsql","--slim","--cache=256","--number-processes=1",
      "--disable-parallel-indexing","-O","flex","-S","style.lua","-d",
      "gis","data.osm.pbf"]


def test_baseline_identical_flags():
    assert m.safe_import_variant(ORIG,"baseline")==ORIG


@pytest.mark.parametrize("variant,old,new",[
    ("cache512","--cache=256","--cache=512"),
    ("workers2","--number-processes=1","--number-processes=2")
])
def test_only_one_flag_changes_and_slim_retained(variant,old,new):
    result=m.safe_import_variant(ORIG,variant)
    assert result==[new if f==old else f for f in ORIG]
    assert "--slim" in result and "--drop" not in result
    assert "--disable-parallel-indexing" in result
    assert ORIG[2]=="--cache=256"


@pytest.mark.parametrize("bad",[
    ["osm2pgsql","--append",*ORIG[1:]],
    ["osm2pgsql","--drop",*ORIG[1:]],
    ["osm2pgsql","--slim","--cache=256","--number-processes=1","data.pbf"],
    ["psql","--slim","--cache=256","--number-processes=1",
     "--disable-parallel-indexing"],
])
def test_refuses_unsupported_import_commands(bad):
    with pytest.raises(ValueError):
        m.safe_import_variant(bad,"workers2")


def test_refuses_unknown_flag():
    with pytest.raises(ValueError):
        m.safe_import_variant(ORIG,"turbo")
