# Test entry points

This folder contains Python unit tests, compatibility tests, and **optional manual tests**. The project root contains only main-user entry points such as `run.bat`; `test-seoul.bat` is intentionally **not** a main launcher.

## Automated

```bash
python3 -m pip install -e '.[test]'
python3 -m pytest -q
```

CI separately validates native Python Mapnik PNG tiles and a synthetic gzip OSM change file (`.osc.gz`) against a fresh MVT rebuild.

## Windows manual Seoul end-to-end

On Windows with WSL2 Ubuntu/Debian installed, from the **repository root**:

```bat
tests\test-seoul.bat
tests\test-seoul.bat 12-15 vector
```

The default case requests `both` (Mapnik PNG + MVT/PBF vector) at z12–14, uses the `seoul-non-military.osm.pbf` extract and a known bbox, and keeps the download in the root `test-output/` cache. It delegates to `tools/run_windows.ps1`, ensuring the regular user conversion path is exercised.

This is a **regional manual test**, not the project's supported geography, an official global performance benchmark, or a default download when using `run.bat`. For internationally reproducible benchmark regions, see [../docs/BENCHMARKS.md](../docs/BENCHMARKS.md).
