"""CLI backward compatibility, explicit content type and elapsed time contracts."""
from pathlib import Path

import pytest

from osm2mbtiles import cli


def parse(*extra):
    args, _ = cli.arguments([
        "convert", "input.osm.pbf", "-o", "output.mbtiles", *extra
    ])
    return args


def test_default_outputs_still_raster_png():
    args = parse()
    assert (args.format, args.tile_type, args.encoding, args.raster_format) == (
        "raster", "raster", "png", "png"
    )
    assert args.dry_run is False
    assert args.max_tiles == 2_000_000


@pytest.mark.parametrize("tile_type", ("raster", "vector", "both"))
def test_explicit_tile_type_and_legacy_format(tile_type):
    for flag in ("--tile-type", "--format"):
        args = parse(flag, tile_type)
        assert args.format == args.tile_type == tile_type


def test_both_with_lossless_png_and_legacy_encoding():
    new = parse("--tile-type", "both", "--raster-format", "png")
    legacy = parse("--format", "both", "--encoding", "png")
    assert new.format == legacy.format == "both"
    assert new.encoding == legacy.encoding == "png"
    assert parse("--tile-type", "raster", "--raster-format", "png256").encoding == "png256"


def test_agreeing_aliases_accepted():
    args = parse("--tile-type", "both", "--format", "both",
                 "--raster-format", "png256", "--encoding", "png256")
    assert args.format == "both" and args.encoding == "png256"


@pytest.mark.parametrize("options", (
    ("--tile-type", "both", "--format", "vector"),
    ("--tile-type", "raster", "--raster-format", "png", "--encoding", "png256"),
    ("--tile-type", "vector", "--raster-format", "png"),
    ("--format", "vector", "--raster-format", "png"),
    ("--tile-type", "both", "--format", "pbf"),
    ("--tile-type", "vector", "--raster-format", "pbf"),
))
def test_confusing_or_unsupported_options_rejected(options):
    with pytest.raises(SystemExit) as exc:
        parse(*options)
    assert exc.value.code == 2


def test_old_single_command_and_tile_limit():
    args, _ = cli.arguments(["seoul.osm.pbf", "--tile-type", "both",
                             "--dry-run", "--max-tiles", "550", "-o", "seoul.mbtiles"])
    assert args.action == "convert" and args.max_tiles == 550 and args.dry_run
    assert args.tile_type == "both"


def test_total_time_on_success_and_failure(capsys, monkeypatch):
    monkeypatch.setattr(cli, "convert", lambda args: None)
    assert cli.main(["convert", "sample.pbf", "-o", "out.mbtiles"]) == 0
    assert "[TIME] Total convert elapsed:" in capsys.readouterr().out

    def failed(args):
        raise cli.PipelineError("forced error")
    monkeypatch.setattr(cli, "convert", failed)
    assert cli.main(["convert", "sample.pbf", "-o", "out.mbtiles"]) == 2
    out = capsys.readouterr()
    assert "[TIME] Total convert elapsed:" in out.out
    assert "forced error" in out.err


def test_seoul_batch_and_download_wrapper_are_present():
    root = Path(__file__).resolve().parents[1]
    bat = (root / "test-seoul.bat").read_text(encoding="utf-8")
    runner = (root / "tools" / "test_seoul.ps1").read_text(encoding="utf-8")
    assert "test_seoul.ps1" in bat
    assert "seoul-non-military.osm.pbf" in runner
    assert "tiles.osm.kr/download/seoul-non-military.osm.pbf" in runner
    assert "'both'" in runner
    assert "run_windows.ps1" in runner
    assert "Seoul total elapsed" in runner
    assert "VERIFIED" in runner


def test_windows_wsl_forwards_new_options():
    root = Path(__file__).resolve().parents[1]
    ps = (root / "tools" / "run_windows.ps1").read_text(encoding="utf-8")
    sh = (root / "tools" / "run_wsl.sh").read_text(encoding="utf-8")
    for name in ("OSM2MBTILES_VECTOR_TILER", "OSM2MBTILES_RASTER_FORMAT"):
        assert name in ps
    for flag in ("--tile-type", "--raster-format", "--vector-tiler"):
        assert flag in sh
    assert 'if [[ "$FORMAT" != vector ]]' in sh


def test_explicit_bbox_dry_run_does_not_require_header_bbox(tmp_path, capsys):
    pbf = tmp_path / "seoul-no-header.osm.pbf"
    pbf.write_bytes(b"fixture without a real OSM PBF header")
    args, _ = cli.arguments([
        "convert", str(pbf), "--tile-type", "both",
        "--raster-format", "png", "--zoom", "12-14",
        "--bbox", "126.75,37.40,127.20,37.72", "--dry-run",
        "--output", str(tmp_path / "seoul.mbtiles")
    ])
    cli.convert(args)
    output = capsys.readouterr().out
    assert "zoom=12-14" in output
    assert "tiles=" in output


def test_seoul_launcher_has_explicit_bbox_and_wsl_forwards_it():
    root = Path(__file__).resolve().parents[1]
    seoul = (root / "tools" / "test_seoul.ps1").read_text(encoding="utf-8")
    windows = (root / "tools" / "run_windows.ps1").read_text(encoding="utf-8")
    linux = (root / "tools" / "run_wsl.sh").read_text(encoding="utf-8")
    assert "126.75,37.40,127.20,37.72" in seoul
    assert "OSM2MBTILES_BBOX" in windows
    assert 'BBOX="${13:-}"' in linux
    assert 'BBOX_ARGS=(--bbox "$BBOX")' in linux
    assert linux.count('"${BBOX_ARGS[@]}"') >= 2
    assert "Failed command at run_wsl.sh:" in linux
