# osm2mbtiles

**Convert OpenStreetMap `.osm.pbf` into PNG raster MBTiles, MVT/PBF vector MBTiles, or both.**

[한국어 문서 / Korean README](README.ko.md) · [Benchmark methodology](docs/BENCHMARKS.md) · [Manual tests](tests/README.md)

osm2mbtiles is a **Docker-free, source-based prototype** using local PostgreSQL/PostGIS, osm2pgsql, and Mapnik/OpenStreetMap Carto. Windows users run the native tools inside **WSL2 (Ubuntu/Debian)**; no Windows-native Mapnik/PostGIS bundle is distributed.

> **Current maturity:** Project tests, an actual Mapnik PNG smoke render, a small real Berlin-style-independent Seoul vector run, and a synthetic `.osc.gz` update/full-rebuild check have passed CI. A complete Windows + WSL PNG-and-vector conversion across a city has not yet been certified. System dependencies, styles, fonts and external map assets must be available. This is not a prepackaged standalone executable.

## Output formats

| Mode | Output | Details |
|---|---|---|
| `--tile-type raster` (default) | `.mbtiles` containing PNG tiles | Mapnik and OSM Carto raster rendering; `png` or `png256` |
| `--tile-type vector` | `.mbtiles` containing gzip-compressed MVT/PBF tiles | PostGIS `ST_AsMVT` with detailed layers and feature attributes |
| `--tile-type both` | `<name>.raster.mbtiles` and `<name>.vector.mbtiles` | Both formats from a shared OSM import path |

The input `.osm.pbf` is **raw OSM data**; MVT/PBF inside vector MBTiles is **a different tile format**. Rendering vector tiles requires an appropriate client-side style (e.g. MapLibre).

## Windows (WSL2): quick start

Prerequisites: Windows 10/11, working **WSL2 Ubuntu/Debian**, PowerShell, internet access for first-time package/style setup, disk space and RAM.

```bat
REM Opens an existing OSM PBF file picker (or uses the sole local .osm.pbf)
run.bat

REM Choose an existing PBF and export MVT/PBF or both tile formats
run.bat vector 12-14
run.bat both 12-14

REM Optional legacy region download; explicit, not the default
run.bat south-korea

REM Manual Seoul-specific smoke test (not the main launcher)
tests\test-seoul.bat
tests\test-seoul.bat 12-15 vector

REM Windows/WSL cache maintenance
cleanup.bat
```

Drag a local `*.osm.pbf` file onto `run.bat` to select a different country or region; the application is **not limited to South Korea**. The selected extract may not contain an OSM `HeaderBBox`; set `OSM2MBTILES_BBOX` or pass `--bbox` explicitly in the Python CLI. `tests/test-seoul.bat` supplies a tested explicit bbox.

## Python / Linux / WSL (developer workflow)

The package requires a prepared native **PostgreSQL/PostGIS + osm2pgsql + Mapnik/OSM Carto runtime** for actual conversion. `pip install` alone does **not** install those native dependencies or styles.

```bash
python3 -m pip install -e '.[test]'
python3 -m pytest -q

# Example: Vector MVT/PBF from a frozen Geofabrik Berlin extract
# Download once from https://download.geofabrik.de/europe/germany/berlin.html
python3 -m osm2mbtiles convert berlin-latest.osm.pbf \
  --bbox 13.20,52.45,13.60,52.60 --zoom 12-16 \
  --tile-type vector --vector-tiler batch \
  --runtime ./runtime --output berlin.mbtiles --processes 4

python3 -m osm2mbtiles verify-vector berlin.mbtiles

# Both output formats; requires the full Mapnik/OSM Carto runtime
python3 -m osm2mbtiles convert berlin-latest.osm.pbf \
  --bbox 13.20,52.45,13.60,52.60 --zoom 12-14 \
  --tile-type both --raster-format png --runtime ./runtime \
  --output berlin.mbtiles

python3 -m osm2mbtiles verify berlin.raster.mbtiles
python3 -m osm2mbtiles verify-vector berlin.vector.mbtiles

# Estimate tile count without importing data
python3 -m osm2mbtiles convert berlin-latest.osm.pbf \
  --bbox 13.20,52.45,13.60,52.60 -z 12-16 \
  -o berlin.mbtiles --tile-type vector --dry-run
```

Additional flags: `--vector-tiler postgis|batch`, `--vector-query-mode projected|baseline|cached`, `--raster-format png|png256`, `--workdir` to retain a private database, and `--force` to replace existing output. `--format` and `--encoding` are compatibility aliases.

## Reproducible, international benchmarks

The recommended **primary** vector comparison is **Berlin, Germany**, using the official [Geofabrik Berlin PBF](https://download.geofabrik.de/europe/germany/berlin.html) and **bbox `13.20,52.45,13.60,52.60`, zoom 12–16** (4,573 possible XYZ positions, not necessarily 4,573 populated tiles).

| Dataset | Scope | Purpose |
|---|---|---|
| **Berlin (Germany)** | z12–16, 4,573 XYZ positions | Primary `osm2mbtiles` vs `Planetiler` vs `Tilemaker` benchmark |
| **Monaco** | small Geofabrik extract | Fast setup/compatibility smoke test, **not** a speed ranking |
| **Seoul (South Korea)** | legacy single-tile/58-tile evidence | Regression checks and historical measurements |

**No Berlin/Monaco three-engine speed results have been measured yet.** Report the **full PBF-to-MBTiles elapsed time** for all three engines under the same host, pinned PBF checksum, bbox, zoom and worker budget. For osm2mbtiles, separately report DB import and **warm PostGIS tile-only** timings. Output schemas differ; report the number of emitted tiles and features along with time and peak memory. See [benchmark protocol and historical Seoul figures](docs/BENCHMARKS.md).

These comparisons concern **Vector MVT/PBF only**. PNG/Mapnik rendering is a separate workload and must not be described as faster or slower than Planetiler/Tilemaker from the vector timings.

## Vector layers and updates

Vector layers include `road`, `pathway`, `railway`, `building`, `building_part`, `poi`, `water`, `landcover`, `landuse`, `boundary`, and `boundary_legal`. The last two intentionally separate administrative relation lines from legal way boundaries. The `landcover` filter includes `natural=rock`; these refinements were validated with a [real Seoul tile in CI](https://github.com/rydolabs/osm2mbtiles/actions/runs/40842394686).

Experimental **vector-only** incremental updates support OSM `.osc.gz` changes with a specially initialized **slim PostGIS database** (ordinary `convert` databases are not append-ready):

```bash
# Requires a running PostGIS with appropriate PGHOST/PGPORT/PGUSER/PGDATABASE
python3 -m osm2mbtiles.vector_incremental init \
  --pbf input.osm.pbf --output updated.mbtiles \
  --bbox 13.20,52.45,13.60,52.60 --zoom 12-16

python3 -m osm2mbtiles.vector_incremental append \
  --diff changes.osc.gz --output updated.mbtiles

# Retry tile writes only after an already-applied diff
python3 -m osm2mbtiles.vector_incremental flush --output updated.mbtiles
```

OSM replication sequence management, automated diff downloads, large-scale production reliability and **raster PNG incremental updates** are not provided. See the [incremental parity CI](https://github.com/rydolabs/osm2mbtiles/actions/runs/40842767385).

## Repository layout

```text
src/osm2mbtiles/     Python implementation (CLI, PNG, MVT, incremental updates)
vector/              osm2pgsql flex Lua and PostGIS post-import SQL
tools/               Windows + WSL runtime and cache helper scripts
tests/               automated tests + optional Seoul manual smoke scripts
benchmarks/          comparable-region manifests and benchmark utilities
docs/                methodology, benchmark results and limitations
.github/workflows/   CI, native Mapnik smoke and data correctness checks
run.bat              Windows launcher for any local OSM PBF
```

`vector/` and `tools/` are intentionally top-level **in this source-checkout distribution**. Note that `pip` wheel-only installs currently do **not** bundle the top-level `vector/` assets; use a repository checkout for full conversion until these assets are packaged as Python resources.

## License and attribution

Project code: [MIT](LICENSE). OpenStreetMap data: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), subject to the ODbL and attribution requirements. Native packages, OSM Carto styles, third-party fonts and external map datasets retain their own licenses and are not shipped in this repository.
