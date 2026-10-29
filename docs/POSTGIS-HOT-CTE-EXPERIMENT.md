# Tokyo PostGIS hot-layer CTE optimization — measured result

## Decision
**Do not merge** into `main`. The experiment preserves exact output but is **3.46% slower** in Tokyo. The current materialized SQL remains the default and this PR stays Draft.

## Implementation
- Default `layer_feat AS MATERIALIZED` is unchanged.
- `OSM2MBTILES_VECTOR_BATCH_INLINE_HOT=1` conditionally switches **only z12 road (l6_feat)** and **z14 building (l11_feat)** to `NOT MATERIALIZED` based on prior EXPLAIN observations.
- All other layers, source geometry CTEs, 2×2 SQL batch inputs, gzip bytes and single SQLite writer remain unchanged.
- Regression tests verify the SQL differs only in those two CTE hints; full project CI: **100 tests passed**. Windows script check, real Mapnik raster smoke, and OSM diff/full rebuild parity also passed.

## Controlled real-data result
- [Successful complete A–B–B–A benchmark](https://github.com/rydolabs/osm2mbtiles/actions/runs/40873257913)
- Same Geofabrik Kanto PBF, one imported PostGIS database, 4 workers, Tokyo bbox `139.55,35.55,139.95,35.85`, z12–16, existing 2×2 SQL batches.
- Input import, download, environment setup excluded from timings.

| Run | CTE mode | Tile export | PostgreSQL temporary data written |
|---|---|---:|---:|
| A1 | Original MATERIALIZED | 85.54 s | 3023.43 MiB |
| B1 | z12 road + z14 building NOT MATERIALIZED | 88.10 s | 2368.29 MiB |
| B2 | z12 road + z14 building NOT MATERIALIZED | 88.16 s | 2368.29 MiB |
| A2 | Original MATERIALIZED | 84.83 s | 3023.43 MiB |

- **Median baseline 85.185s**, optimized **88.13s**, **3.46% slower**.
- PostgreSQL temp writes fell approximately **21.7%**, without corresponding latency benefit.
- Two runs per mode on a hosted runner are insufficient for statistically stable small differences. Do not directly compare absolute timings with runs executed on a separate hosted VM/date.

## Highest priority: exact output parity
- All four outputs have **6,355 nonempty MVT tiles**.
- Compared with A1, each of B1, B2 and A2 has **zero mismatched compressed tile_data bytes**, exact matching tile coordinate sets, and exact metadata values.
- Every resulting SQLite MBTiles database passed `PRAGMA integrity_check`.
- SQLite physical file lengths may differ because of row insertion order, even when logical MBTiles content is byte-identical.

## Diagnosis
Selected expensive feature CTEs are responsible for significant materialized temp data, but removing materialization causes more work elsewhere, or less advantageous query plans. Top-level EXPLAIN samples show temp block reductions but cannot prove the precise repeated-expression mechanism.

This builds on prior tests:
- Z11 spatial locality: +1.68% slower, exact parity PASS (PR #6).
- Inline **all** feature CTEs: +13.52% slower, ~47% fewer temp writes, exact parity PASS (PR #7).
- Session-only work_mem 16MB: -0.58% faster (likely noise), 28% fewer temp writes, exact parity PASS (PR #8).

**Next technically justified task:** obtain per-node EXPLAIN with instrumentation/CPU metrics and reduce duplicated geometry/JSONB property calculation, rather than further optimizing disk I/O alone. Set measurable acceptance criteria and retain original output parity.
