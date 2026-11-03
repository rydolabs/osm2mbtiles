"""Locate vector flex/SQL assets in source checkouts and installed wheels.

The top-level vector/ directory is canonical in a checkout. Release wheels
carry a build-time copy of precisely these two source assets inside the
osm2mbtiles package; an isolated install must never depend on the source tree.
"""
from pathlib import Path


VECTOR_ASSETS = ("vector-flex.lua", "post-import.sql")


def vector_assets_dir() -> Path:
    module = Path(__file__).resolve()
    candidates = (
        module.parents[2] / "vector",          # source checkout
        module.parent / "_vector_assets",      # installed wheel / copied files
    )
    for candidate in candidates:
        if all((candidate / name).is_file() for name in VECTOR_ASSETS):
            return candidate
    raise FileNotFoundError(
        "Vector flex/SQL assets missing. Install a wheel with bundled vector "
        "resources or run from a complete source checkout (vector/)."
    )
