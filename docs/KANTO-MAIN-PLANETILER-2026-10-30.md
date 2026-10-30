# Frozen main Kanto/Tokyo vs Planetiler — 2026-10-30

**Result:** measured and validated, [GitHub Actions run 40977042045](https://github.com/rydolabs/osm2mbtiles/actions/runs/40977042045).  
**Production baseline:** `4b7f84ce8bf7a8d4227c1e7f557e9a4056c3db7a` (exact same `src/` and `vector/` files).  
**Setup:** Ubuntu 24.04 GitHub-hosted VM, PostgreSQL 16/PostGIS, osm2pgsql, Planetiler v0.10.2, Java 25; four workers, 2×2 Batch side, SQL query mode `projected`.  
**Data:** Geofabrik full Kanto PBF (`kanto-latest.osm.pbf` downloaded once), input SHA256 `1af62f945ecb25a1c52db30967d7d2db1c6aaea5a17f1dbb1d53dbe55691040e`.  
**Output selection:** Tokyo bbox `139.55,35.55,139.95,35.85`, z12–16. **This is NOT a full-Kanto tile extent test.**  
**Planetiler profile:** historical comparison YAML SHA256 `b5d62b194f89359102ff099631f2842bdda92ad30fc671be65bd6c83e8ee8765`; not semantically equal to PostGIS SQL.

## Timings

| Metric | osm2mbtiles current main PostGIS Batch | Planetiler v0.10.2 |
|---|---:|---:|
| Full Kanto PostGIS DB init, import, index, post-import | **261.13s** | N/A |
| Tile-only / full build, round 1 | 120.51s warm Batch | 91.86s full PBF→MBTiles |
| Round 2 | 118.85s warm Batch | 94.18s full |
| Round 3 | 118.54s warm Batch | 89.85s full |
| **Median (three consecutive runs)** | **118.85s**, warm tile stage ONLY | **91.86s**, entire PBF-to-MBTiles |
| **Cold construction component sum** | **379.98s** (261.13 + 118.85) | **91.86s** |
| Measured min–max | 118.54–120.51s | 89.85–94.18s |
| Nonempty output tiles | 6,355 | 6,359 |
| First run MBTiles bytes | 385,482,752 (367.6 MiB) | 233,701,376 (222.9 MiB) |
| gzip compressed tile BLOB count | 6,355 | 6,359 |
| Output gzip MVT payload fingerprint stable over 3 repeats | **Yes** | **Yes** |

**Numerical interpretation:** Current PostGIS one-time creation component sum is approximately **4.14×** Planetiler's full-build median. This ratio is a timing observation for the stated **non-equivalent output profiles and PBF-to-Tokyo selection**, not an equal-workload rendering speed ranking or a general claim about other regions.

Import/setup includes Postgres cluster creation; source download and package installs are excluded. For PostGIS, import and tile export are separate timed commands; 379.98s is a sum, NOT a single uninterrupted command wall time.

Because the full Kanto extract is imported into PostGIS although output covers only central Tokyo, the preparation stage has more reusable data than this one export needs.

## Correctness and repeatability

- Each run: SQLite `PRAGMA integrity_check=ok`, vector format `pbf`, every XYZ within the requested bbox+zoom, nonempty tile data and successful sample MVT decoding.
- For each engine **separately**, 3 runs had an identical fingerprint computed from every ordered tile coordinate and compressed MVT `tile_data` byte string. Different SQLite file sizes across PostGIS repeats are due to file layout; compressed payload fingerprints were identical.
- Cross-engine MVT byte parity was **not asserted**: layer/tag profiles and tile counts differ. PostGIS and Planetiler output sizes differ substantially.
- Alternating order was Batch→Planetiler, Planetiler→Batch, Batch→Planetiler. Three sequential repeats on one VM are more informative than one run, but they are not independently provisioned machines/cold-cache trials.

## Implications for incremental updates

The above is the initial-generation benchmark. **No `.osc.gz` update or PostGIS `vector_incremental.flush_tiles` timing was measured in this Kanto run.** The current `vector_incremental.init/flush_tiles` still uses the original tile-by-tile SQL rather than the optimized Batch path. Consequently, it would be **incorrect** to compare `379.98s + arbitrary Batch tile timing` with `2 × 91.86s` and claim incremental advantage. A separate full Kanto persistent PostGIS initial + real diff/dirty flush versus full Planetiler rebuild benchmark with a pinned OSM change stream and equivalent state is needed.

This measured workload does not invalidate the project's long-term PostGIS value proposition (persistent data, incremental vector update support, OSM Carto PNG integration). It shows that **speed on a cold one-off Tokyo export is not the current architectural advantage.**

## Reproducible sources

- [Actions run, logs and ZIP evidence](https://github.com/rydolabs/osm2mbtiles/actions/runs/40977042045)
- `benchmarks/kanto_main_compare.py`: runs, timing, metadata/XYZ verification and report generation
- `benchmarks/kanto-planetiler.yml`: historical profile
- `.github/workflows/kanto-current-main-vs-planetiler.yml`: real environment and exact source SHA assertion
- Prior source baseline: [PR #5 (Batch optimization)](https://github.com/rydolabs/osm2mbtiles/pull/5)
