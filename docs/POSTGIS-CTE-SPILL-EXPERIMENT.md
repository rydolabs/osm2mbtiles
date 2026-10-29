# PostGIS MVT feature CTE spill: Tokyo A-B-B-A experiment

**Decision: keep the original MATERIALIZED CTE. Experimental inline mode preserves every tile but is 13.5% slower.**

## Background and hypothesis

The previously tested spatial-shard scheduler produced byte-identical results but no speedup, and about 3 GiB of PostgreSQL temporary data written for each Tokyo vector export. This experiment changes one SQL optimizer hint within the existing 2×2 microbatch query:

- Baseline: layer_src AS MATERIALIZED; layer_feat AS MATERIALIZED.
- Experimental: layer_src AS MATERIALIZED; layer_feat AS NOT MATERIALIZED.
- Feature predicates, geometry transformations, ST_AsMVT/MVT buffer, prepared SQL, gzip and SQLite writer remain unchanged.
- Experimental env selector: OSM2MBTILES_VECTOR_BATCH_FEATURE_CTE=inline. Omitted/default: materialized.

This tests whether inlining the per-feature tile CTE allows the PostgreSQL optimizer to push tile constraints down, reducing intermediate tuplestore I/O. The SQL hint may lead to more repeated processing instead: it is a performance hypothesis, not a guaranteed optimization.

## Controlled real benchmark (2026-10-29)

Source: entire Geofabrik Kanto PBF SHA256
1af62f945ecb25a1c52db30967d7d2db1c6aaea5a17f1dbb1d53dbe55691040e

Input/output constraints: Tokyo bbox 139.55,35.55,139.95,35.85; zoom 12–16; 6,863 candidate XYZ grid cells; 6,355 nonempty vector tiles. Same single imported PostgreSQL/PostGIS DB, 4 workers, 2×2 SQL batches, shared_buffers=96MB, work_mem=8MB, track_io_timing=on. Baseline source: main branch, candidate: separate experimental branch; only tile-export stage is timed.

GitHub Actions run:
https://github.com/rydolabs/osm2mbtiles/actions/runs/40865724895

Execution order A-B-B-A (two measurements each):

| Run | Feature CTE | Tile export seconds | PostgreSQL temp MiB written | Shared blocks read |
|---|---|---:|---:|---:|
| A1 | MATERIALIZED | 123.40 | 3,023.4 | 2,197,781 |
| B1 | NOT MATERIALIZED | 138.01 | 1,600.7 | 1,722,082 |
| B2 | NOT MATERIALIZED | 139.54 | 1,600.7 | 1,734,491 |
| A2 | MATERIALIZED | 121.10 | 3,023.4 | 1,763,157 |

- Baseline median: **122.25s**.
- Inlined feature CTE median: **138.775s**, **13.52% slower**.
- PostgreSQL temp write volume reduced **47.06%**, from 3023.4 MiB to 1600.7 MiB.
- Disk reads also declined in the inline runs versus first baseline, but cache warming and execution order are confounders. Disk throughput and physical page-cache misses have not been separately measured.

## Highest-priority output correctness

For each of B1, B2 and A2 compared with A1:
- **All 6,355 compressed MVT tile_data byte sequences identical.**
- **0 mismatched XYZ/TMS coordinates or rows.**
- **All MBTiles metadata key/value pairs identical.**
- **Every output passed PRAGMA integrity_check.**

The .mbtiles *physical file* sizes may differ due to SQLite insertion order. Actual stored content and coordinates are identical; this is a stronger comparison than matching only tile counts or decoded layer counts.

## Representative SQL EXPLAIN (ANALYZE, BUFFERS) after timed exports

One batch per zoom; these are not aggregated benchmark averages:

| Zoom | CTE mode | SQL execution ms | Temp written blocks | Temp read blocks |
|---|---|---:|---:|---:|
| 12 | MATERIALIZED | 2,261.4 | 2,887 | 8,661 |
| 12 | NOT MATERIALIZED | 2,270.9 | 1,419 | 4,257 |
| 14 | MATERIALIZED | 719.5 | 2,040 | 6,120 |
| 14 | NOT MATERIALIZED | 885.8 | 991 | 2,973 |
| 16 | MATERIALIZED | 67.7 | 0 | 0 |
| 16 | NOT MATERIALIZED | 80.3 | 0 | 0 |

The hypothesis about fewer temp spills is supported, while the speed hypothesis is disproven in this workload. One plausible contributor is repeated inlined expression evaluation or a different execution plan, but individual plan nodes were not captured: do not claim a root cause from top-level BUFFERS alone.

## Decision and next experiment

**Do not change the production SQL default.** Keep the optimization behind the experimental environment variable only; leave this PR draft and unmerged.

A better next experiment is to **retain SQL shape** and try carefully scoped PostgreSQL work_mem (e.g. 8MiB -> 16MiB for render workers) or selectively materialize only large candidate CTEs. Time, full MVT content parity, Postgres temp bytes and peak aggregate memory must be measured together; four connections and multiple operators multiply the work_mem budget.

The GitHub Actions artifact named tokyo-postgis-cte-spill-abba contains exact parity JSON, per-run database counters, times, logs, input SHA256 and representative query plan summaries. The hosted runner and two repeats per mode do not establish statistical significance for small differences, though +13.52% is not a promising first result.
