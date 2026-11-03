"""Setuptools build hook: bundle the canonical root vector SQL/Lua in wheels.

The source checkout retains vector/ in its original location. The build-time
copy inside the wheel is byte-for-byte, not a second hand-maintained source.
"""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py

FILES = ("vector-flex.lua", "post-import.sql")


class BuildPyWithVectorAssets(build_py):
    def run(self):
        super().run()
        root = Path(__file__).resolve().parent
        source = root / "vector"
        destination = Path(self.build_lib) / "osm2mbtiles" / "_vector_assets"
        destination.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            path = source / name
            if not path.is_file():
                raise RuntimeError(f"Missing release vector asset: {path}")
            shutil.copy2(path, destination / name)


setup(cmdclass={"build_py": BuildPyWithVectorAssets})
