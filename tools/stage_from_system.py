#!/usr/bin/env python3
"""DEVELOPER-ONLY: assemble a local runtime layout from installed native packages.

This makes symlinks into existing host packages, NOT a portable distribution.
End-user releases must vendor binaries and shared libraries for their target OS.
"""
import argparse
import shutil
import sys
import glob
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('--style-src',required=True,type=Path,help='Pinned OSM Carto checkout with compiled project.xml')
p.add_argument('--dest',default='runtime',type=Path)
p.add_argument('--mapnik-python',default=sys.executable,type=Path)
a=p.parse_args()
r=a.dest.resolve()
(r/'bin').mkdir(parents=True,exist_ok=True)
for tool in ['initdb','pg_ctl','createdb','psql','osm2pgsql','ogr2ogr']:
    command=shutil.which(tool)
    if not command and tool in ('initdb', 'pg_ctl'):
        candidates=glob.glob('/usr/lib/postgresql/*/bin/' + tool)
        if candidates:
            command=sorted(candidates)[-1]
    if not command:
        raise SystemExit(f'Install {tool} in developer environment before staging')
    target=r/'bin'/tool
    target.unlink(missing_ok=True)
    target.symlink_to(command)
(r/'python'/'bin').mkdir(parents=True,exist_ok=True)
exe=r/'python'/'bin'/'python3'
exe.unlink(missing_ok=True)
exe.symlink_to(a.mapnik_python.resolve())
style=r/'styles'/'osm-carto'
if style.exists():
    shutil.rmtree(style)
shutil.copytree(a.style_src,style,ignore=shutil.ignore_patterns('.git','node_modules'))
if not (style/'project.xml').exists():
    raise SystemExit('project.xml missing: compile style with carto -a 3.0.22 project.mml > project.xml')
print(f'Created development-only runtime links in {r}')
print('WARNING: runtime relies on pre-installed system libraries and is not redistributable.')