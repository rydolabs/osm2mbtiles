"""Packaged native-tool detection and safely scoped temporary PostgreSQL cluster."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

class PipelineError(RuntimeError):
    pass

def executable(path: Path) -> Path:
    if path.exists() and path.is_file():
        return path
    if os.name == 'nt' and path.with_suffix('.exe').is_file():
        return path.with_suffix('.exe')
    raise PipelineError(f"Missing bundled executable: {path}. Provide a prepared native --runtime bundle.")

@dataclass(frozen=True)
class Runtime:
    root: Path

    def bin(self, tool: str) -> Path:
        return executable(self.root / 'bin' / tool)

    @property
    def python(self) -> Path:
        # mapnik Python bindings must match this interpreter; host Python isn't assumed compatible.
        for path in (self.root / 'python' / 'bin' / 'python3',
                     self.root / 'python' / 'python.exe'):
            if path.exists():
                return path
        raise PipelineError('Missing runtime/python interpreter with Mapnik bindings')

    @property
    def style(self) -> Path:
        return self.root / 'styles' / 'osm-carto'

    def validate(self) -> None:
        missing = []
        for name in ('initdb', 'pg_ctl', 'createdb', 'psql', 'osm2pgsql'):
            try:
                self.bin(name)
            except PipelineError:
                missing.append(name)
        try:
            self.python
        except PipelineError:
            missing.append('python-with-mapnik')
        for name in ('project.xml','openstreetmap-carto-flex.lua','indexes.sql',
                     'functions.sql','common-values.sql'):
            if not (self.style / name).is_file():
                missing.append(f'osm-carto/{name}')
        if missing:
            raise PipelineError('Native runtime incomplete: ' + ', '.join(missing))

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        bins = str(self.root / 'bin')
        env['PATH'] = bins + os.pathsep + env.get('PATH','')
        if os.name != 'nt':
            libs = str(self.root/'lib')
            env['LD_LIBRARY_PATH'] = libs + os.pathsep + env.get('LD_LIBRARY_PATH','')
        # Paths are bundle-owned, not system-global locations.
        for key,folder in [('GDAL_DATA',self.root/'share'/'gdal'),
                           ('PROJ_DATA',self.root/'share'/'proj'),
                           ('FONTCONFIG_PATH',self.root/'etc'/'fonts')]:
            if folder.is_dir():
                env[key]=str(folder)
        return env

def run(cmd: list[str | Path], env: dict | None = None, cwd: Path | None = None) -> None:
    argv = [str(v) for v in cmd]
    try:
        subprocess.run(argv, env=env, cwd=str(cwd) if cwd else None, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PipelineError(f"Step failed ({Path(argv[0]).name}); see output above") from exc

def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda: f.read(8*1024*1024), b''):
            h.update(part)
    return h.hexdigest()

class LocalDatabase:
    """Ephemeral process. No global service installation and no exposed network on Unix."""
    def __init__(self, runtime: Runtime, work: Path):
        self.runtime = runtime
        self.work = work
        self.data = work / 'pgdata'
        self.sock = work / 'socket'  # replaced by a short private /tmp path at runtime
        self.secret_file = work / '.pgpass-secret'
        self.log = work / 'postgres.log'
        self.started = False
        self.port = 0
        self.env = runtime.env()

    def _setup(self):
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != 'nt':
            self.work.chmod(0o700)
        if not self.secret_file.exists():
            self.secret_file.write_text(secrets.token_urlsafe(36)+'\n', encoding='utf-8')
        if os.name != 'nt':
            self.secret_file.chmod(0o600)
        self.env.update({
            'PGUSER':'osm2mbtiles', 'PGPASSWORD':self.secret_file.read_text().strip(),
            'PGDATABASE':'gis',
        })
        if not (self.data/'PG_VERSION').exists():
            run([self.runtime.bin('initdb'), '-D', self.data,
                 '-U', 'osm2mbtiles', '--auth-local=scram-sha-256',
                 '--auth-host=scram-sha-256', f'--pwfile={self.secret_file}'], self.env)

    def __enter__(self):
        self._setup()
        # A short Unix socket path avoids the sockaddr_un path-length limit when
        # the user's --workdir is deeply nested. A private 0700 dir guards access.
        if os.name != 'nt':
            self.sock = Path(tempfile.mkdtemp(prefix='osm2mbtiles-pg-'))
        try:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0))
                self.port = sock.getsockname()[1]
            self.env['PGPORT'] = str(self.port)
            if os.name == 'nt':
                self.env['PGHOST'] = '127.0.0.1'
                options = f'-c listen_addresses=127.0.0.1 -p {self.port} -c jit=off'
            else:
                self.env['PGHOST'] = str(self.sock)
                options = (f"-c listen_addresses='' -c unix_socket_directories='{self.sock}' "
                           f'-p {self.port} -c jit=off')
            # Conservative settings for WSL: OSM import/index postprocessing can
            # run out of memory when PostgreSQL spawns parallel workers.
            # Keep fsync enabled; a DB import can be safely restarted on failure.
            options += (' -c shared_buffers=64MB -c work_mem=4MB'
                        ' -c maintenance_work_mem=64MB'
                        ' -c max_parallel_maintenance_workers=0'
                        ' -c max_parallel_workers_per_gather=0')
            run([self.runtime.bin('pg_ctl'), '-D', self.data, '-l', self.log,
                 '-w', '-o', options, 'start'], self.env)
            self.started = True
            return self
        except BaseException:
            if os.name != 'nt':
                shutil.rmtree(self.sock,ignore_errors=True)
            raise

    def __exit__(self, error_type, error, traceback):
        try:
            if self.started:
                try:
                    run([self.runtime.bin('pg_ctl'), '-D', self.data,
                         '-m', 'fast', '-w', 'stop'], self.env)
                except PipelineError as stop_error:
                    # PostgreSQL may already have been killed by the OOM killer.
                    # Never hide the real import failure with a secondary
                    # 'pg_ctl could not send stop signal' exception.
                    if error_type is None:
                        raise
                    print(f'[WARN] PostgreSQL already stopped or stop failed '
                          f'after prior error: {stop_error}', file=sys.stderr)
        finally:
            self.started = False
            if os.name != 'nt':
                shutil.rmtree(self.sock,ignore_errors=True)

    def psql(self, *args: str, database: str = 'gis') -> None:
        run([self.runtime.bin('psql'), '-X', '-v', 'ON_ERROR_STOP=1', '-d', database, *args], self.env)

    def import_extract(self, pbf: Path, style: Path) -> None:
        # A failed/aborted import may have left an incomplete 'gis' database.
        # The cluster is application-owned: reset only that database on retry.
        self.psql('-c', 'DROP DATABASE IF EXISTS gis;', database='postgres')
        run([self.runtime.bin('createdb'), 'gis'], self.env)
        self.psql('-c', 'CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS hstore;')
        # The default middle mode retains all temporary nodes/ways in RAM.
        # South Korea can exceed WSL's memory allowance even when the PBF is
        # relatively small. Slim/drop stores temporary middle data in PostGIS
        # and removes it afterward; index creation is serial to cap peak RAM.
        # Tradeoff: more disk writes and possibly longer imports, but fewer
        # abrupt PostgreSQL OOM terminations.
        import_cmd = [self.runtime.bin('osm2pgsql'), '--slim', '--drop',
                      '--cache=256', '--number-processes=1',
                      '--disable-parallel-indexing', '-O', 'flex', '-S',
                      style/'openstreetmap-carto-flex.lua', '-d', 'gis', pbf]
        try:
            run(import_cmd, self.env, cwd=style)
        except PipelineError:
            self.print_database_diagnostics()
            raise
        # These are required by OSM Carto 6.x; order matches upstream setup requirements.
        for sql in ('indexes.sql','functions.sql','common-values.sql'):
            self.psql('-f', str(style/sql))

    def import_shared_extract(self, pbf: Path, style: Path, vector_dir: Path, profile: str = 'safe') -> None:
        """One osm2pgsql pass populates Carto raster and detailed vector tables.

        The combined Lua includes the unmodified Carto flex callbacks and then
        dispatches to the independent vector callbacks. It is never used for
        raster-only imports; output parity must be verified on real extracts.
        """
        if profile not in ('safe', 'fast'):
            raise PipelineError('Vector import profile must be safe or fast')
        carto = (style/'openstreetmap-carto-flex.lua').read_text(encoding='utf-8')
        vector = (vector_dir/'vector-flex.lua').read_text(encoding='utf-8')
        if profile == 'fast':
            if vector.count('\n})\n') < 4:
                raise PipelineError('Unexpected vector flex definitions; refusing unsafe style rewrite')
            vector = vector.replace('\n})\n', '\n}, { cluster = "no" })\n', 4)
        # Lua local scope is deliberately isolated, and callbacks are saved
        # before the second script overwrites their global names.
        combined = (carto + '\n'
            + 'local carto_node, carto_way, carto_relation = '
              'osm2pgsql.process_node, osm2pgsql.process_way, osm2pgsql.process_relation\n'
            + 'do\n' + vector + '\nend\n'
            + 'local vector_node, vector_way, vector_relation = '
              'osm2pgsql.process_node, osm2pgsql.process_way, osm2pgsql.process_relation\n'
            + 'function osm2pgsql.process_node(o)\n'
              '  if carto_node then carto_node(o) end\n'
              '  if vector_node then vector_node(o) end\nend\n'
            + 'function osm2pgsql.process_way(o)\n'
              '  if carto_way then carto_way(o) end\n'
              '  if vector_way then vector_way(o) end\nend\n'
            + 'function osm2pgsql.process_relation(o)\n'
              '  if carto_relation then carto_relation(o) end\n'
              '  if vector_relation then vector_relation(o) end\nend\n')
        self.psql('-c', 'DROP DATABASE IF EXISTS gis;', database='postgres')
        run([self.runtime.bin('createdb'), 'gis'], self.env)
        self.psql('-c', 'CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS hstore;')
        script = self.work/'combined-carto-vector-flex.lua'
        script.write_text(combined, encoding='utf-8')
        command = [self.runtime.bin('osm2pgsql'), '--slim', '--drop', '--cache=256']
        if profile == 'fast':
            command += ['--number-processes=4']
        else:
            command += ['--number-processes=1', '--disable-parallel-indexing']
        command += ['-O', 'flex', '-S', script, '-d', 'gis', pbf]
        try:
            run(command, self.env, cwd=style)
            for sql in ('indexes.sql', 'functions.sql', 'common-values.sql'):
                self.psql('-f', str(style/sql))
            self.psql('-f', str(vector_dir/'post-import.sql'))
        except PipelineError:
            self.print_database_diagnostics()
            raise

    def ensure_gis_database(self) -> None:
        """Create only our private gis database if absent; preserve Carto tables."""
        probe = subprocess.run(
            [str(self.runtime.bin('psql')), '-X', '-At', '-d', 'postgres',
             '-c', "SELECT 1 FROM pg_database WHERE datname='gis'"],
            env=self.env, capture_output=True, text=True, check=True)
        if probe.stdout.strip() != '1':
            run([self.runtime.bin('createdb'), 'gis'], self.env)
        self.psql('-c', 'CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS hstore;')

    def import_vector_extract(self, pbf: Path, vector_dir: Path, profile: str = "safe") -> None:
        """Independent all-tag vector import into vector_* tables.

        Raster tables in gis are preserved. Drops only own vector tables.
        """
        self.ensure_gis_database()
        self.psql('-c', 'DROP TABLE IF EXISTS vector_label_points, vector_points, vector_lines, vector_areas, vector_relations CASCADE;')
        if profile not in ('safe', 'fast'):
            raise PipelineError('Vector import profile must be safe or fast')
        style = vector_dir / 'vector-flex.lua'
        command = [self.runtime.bin('osm2pgsql'), '--slim', '--drop',
                   '--cache=256']
        if profile == 'fast':
            # Benchmarked on Seoul 2026-10-28. Only opt in on sufficiently
            # provisioned machines: parallel indexers can increase PostgreSQL
            # server memory pressure beyond the importer process RSS.
            source = style.read_text(encoding='utf-8')
            if source.count('\n})\n') < 4:
                raise PipelineError('Unexpected vector flex table definitions; refusing unsafe style rewrite')
            style = self.work / 'vector-fast-flex.lua'
            style.write_text(source.replace('\n})\n',
                             '\n}, { cluster = "no" })\n', 4), encoding='utf-8')
            command += ['--number-processes=4']
        else:
            command += ['--number-processes=1', '--disable-parallel-indexing']
        command += ['-O', 'flex', '-S', style, '-d', 'gis', pbf]
        try:
            run(command, self.env, cwd=vector_dir)
            self.psql('-f', str(vector_dir/'post-import.sql'))
        except PipelineError:
            self.print_database_diagnostics()
            raise

    def print_database_diagnostics(self) -> None:
        """Report original PostgreSQL crash details, not just the cleanup failure."""
        print(f'[DIAG] PostgreSQL log: {self.log}', file=sys.stderr)
        try:
            if self.log.exists():
                lines = self.log.read_text(encoding='utf-8', errors='replace').splitlines()
                print('[DIAG] Last PostgreSQL log lines:', file=sys.stderr)
                for line in lines[-70:]:
                    print(line, file=sys.stderr)
        except OSError as exc:
            print(f'[DIAG] Could not read PostgreSQL log: {exc}', file=sys.stderr)
        print('[DIAG] Check WSL memory and disk availability; see WINDOWS-ONECLICK.md.',
              file=sys.stderr)

    def prepare_external_data(self, style: Path) -> None:
        """Install bundled style assets or execute upstream scripts on first use."""
        # Upstream installer imports external GIS tables into each new database.
        # It must run even when cached asset files are already present.
        data_script = style/'scripts'/'get-external-data.py'
        font_script = style/'scripts'/'get-fonts.py'
        if not data_script.is_file() or not font_script.is_file():
            raise PipelineError('OSM Carto assets missing; bundle get-external-data.py and get-fonts.py scripts')
        run([self.runtime.python, data_script, '--cache', '--no-update'], self.env, cwd=style)
        font_marker = style/'.osm2mbtiles-fonts-ready'
        if not font_marker.exists():
            run([self.runtime.python, font_script], self.env, cwd=style)
            font_marker.write_text('complete\n',encoding='utf-8')