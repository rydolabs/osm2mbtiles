# Tokyo Z11 spatial-locality scheduler: strict parity and measured result

**Status: content parity PASS; no performance benefit on Tokyo z12–16.**

## Goal and scope

Inspired by the supplied PostGIS MVT agent brief and Tilemaker 3.x locality work, this experiment groups **unchanged 2×2 SQL microbatches** by Z11 geographic parent; overloaded parent regions split into Z12/Z13/... groups based on number of SQL batches. Unlike a density-driven scheduler, the current splitting proxy is SQL batch count. Each group executes on one Pool worker with a reused PostgreSQL connection and prepared SQL. The SQL, geometry, output attributes, gzip bytes and SQLite writer are unchanged.

The existing default stays scanline. This change is an experimental opt-in using CLI flags --vector-tiler batch --vector-batch-side 2 --vector-spatial-shard-zoom 11 --vector-spatial-max-batches 32. The Tokyo workload comprises 1,761 original SQL microbatches, regrouped into 130 locality Pool tasks/shards, covering the same 6,863 requested XYZ positions.

## Actual GitHub Actions experiment, 2026-10-29

- Run: https://github.com/rydolabs/osm2mbtiles/actions/runs/40863363180
- Same entire Geofabrik Kanto PBF, SHA256: 1af62f945ecb25a1c52db30967d7d2db1c6aaea5a17f1dbb1d53dbe55691040e
- Tokyo bbox: 139.55,35.55,139.95,35.85; z12–16
- Both versions use one imported PostGIS DB, 4 workers, 2×2 SQL microbatches, PostgreSQL shared_buffers 96MB / work_mem 8MB, track_io_timing=on
- Baseline main SHA: 8968573f29508b0934d69bc3b0f955e9d11fa591
- Candidate SHA: ad8bebd7f08c0f5b3c28fa24d26ceb7bda7de920
- A–B–B–A order; tile-only times exclude PBF download, osm2pgsql import, setup and sample EXPLAIN

| Run | Scheduler | Tile-only time | PostgreSQL shared blocks hit | Shared blocks read | Temp data |
|---|---|---:|---:|---:|---:|
| A1 | Scanline | 70.63 s | 7,856,276 | 1,906,233 | 3,023 MiB |
| B1 | Z11 locality | 70.67 s | 7,845,738 | 2,083,258 | 3,023 MiB |
| B2 | Z11 locality | 68.16 s | 7,619,223 | 1,827,622 | 3,023 MiB |
| A2 | Scanline | 65.91 s | 7,678,168 | 1,768,677 | 3,023 MiB |

**Median scanline: 68.27 s. Median Z11 locality: 69.415 s. New scheduler: +1.68% slower.** The two repeats per implementation on a hosted VM are insufficient to establish a stable regression; no measured speedup or reduction in disk reads was observed.

## Exact output equivalence (highest priority)

Every run generated **6,355 nonempty MVT tiles**. Against baseline A1, each of B1, B2, and A2 independently passed:
- Identical full metadata key-value mapping.
- Identical complete XYZ/TMS tile coordinate rows.
- **0 mismatched compressed tile_data byte sequences** over all 6,355 tiles.
- SQLite PRAGMA integrity_check = ok.

SQLite physical file byte size may differ because insertion order influences page layout. Actual metadata and each stored tile payload are **byte-for-byte identical**. Parity is mandatory; never accept changed content as an optimization.

## PostGIS diagnosis and decision

PostgreSQL's database-level stats report **about 3,023 MiB of temporary data written per tile stage**, essentially unchanged by Z11 scheduling. Representative, non-generalizable EXPLAIN (ANALYZE, BUFFERS) samples:
- z14 one 2×2 batch: 432.893 ms, 2,040 temporary blocks written, 6,120 temporary blocks read, 2,136 shared blocks read.
- z16 one 2×2 batch: 34.620 ms, zero temporary blocks and zero shared blocks read.

Inspect the MATERIALIZED CTE and its temporary materialization/correlation to layer density, then consider **targeted** work_mem or CTE/query-plan changes before further scheduling work. Raising work_mem blindly can multiply memory consumption across four worker connections and plan nodes.

**Do not enable Z11 scheduler by default.** Keep it opt-in for experiments in other geographies; no speed benefit has been demonstrated in Tokyo. Data source, timings, PostgreSQL stats, source SHAs, and explain plans are in the Actions artifact named tokyo-z11-spatial-abba-byte-parity. Follow the project's benchmark methodology for additional repeated cold/warm runs.
