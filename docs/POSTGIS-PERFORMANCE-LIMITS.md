# Architecture decision: PostGIS limits for one-shot PBF → MVT MBTiles

**Status:** Proposed technical conclusion (2026-10-30); no production backend change.  
**Decision scope:** Offline *cold* bulk creation versus *warm/repeated* PostGIS tile exports and OSM diff updates.

## Summary

**Do not infer that "PostGIS is always slow."** This project's current *PBF → osm2pgsql flex → PostgreSQL/PostGIS tables and indexes → tile-specific SQL → MVT → gzip → MBTiles* pipeline has unavoidable database staging and maintenance costs **if we choose that architecture**. A direct PBF tiler can avoid persistent PostGIS import and secondary-table preparation for a one-off export. This is a meaningful structural disadvantage when the primary goal is the shortest possible cold, one-off bulk build.

PostGIS is still useful when the imported database is reused, queried independently, or retained for `.osc.gz` updates. Its MVT functions, spatial indexes, SQL and concurrency offer considerable optimization opportunities. The current results **do not establish an immutable performance ceiling for PostGIS** or an apples-to-apples Planetiler/Tilemaker ranking.

### Architecture trade-off

```text
Existing cold route:
PBF → osm2pgsql parse/import → PostGIS indexes/tables → per-tile or 2x2 SQL
    → ST_AsMVTGeom / ST_AsMVT → worker gzip → SQLite MBTiles

Direct bulk route (e.g., Planetiler / Tilemaker):
PBF → OSM feature processing → compact/spatial intermediate representation
    → tiles grouped by tile ID/region → encode/compress → MBTiles
```

**Some costs are universal:** OSM geometry reconstruction, filtering, clipping, MVT encoding, compression, and output writes. Differences are where, how often, and in which representation the data is processed. Planetiler itself uses OSM input passes and external feature sorting; a direct tiler is not "zero preprocessing."

## Evidence already measured (Tokyo bbox `139.55,35.55,139.95,35.85`, z12–16)

### Original three-engine pilot — PR #4

[Original run 40850574178](https://github.com/rydolabs/osm2mbtiles/actions/runs/40850574178):
- PostGIS initialization + import of the **whole Kanto PBF**: **259.35 s**.
- PostGIS Batch tile export (post-import): **124.36 s**.
- Component sum: **383.71 s** (not a single timed end-to-end command).
- Planetiler 0.10.2 custom profile, PBF → MBTiles: **89.97 s**.
- Tilemaker 2.4: **no valid result**, because config `maxzoom=16` exceeded `basezoom=14`.

These are **one-run, non-equivalent layer/attribute profiles**, and not a fair algorithmic speed ratio. Specifically the PostGIS numbers include full-Kanto import to export a Tokyo rectangle. The original workflow uses different native/custom layer rules across engines; number and type of features differ.

The newer [PR #4 Tilemaker 3.2 pilot](https://github.com/rydolabs/osm2mbtiles/actions/runs/40959339731) also produced **no tile benchmark**: the Tilemaker source compiled, but its unsupported `--version` flag caused the verification step to fail. Tilemaker 3.2 does not offer a measured result here. Note that the Tilemaker CLI describes `--bbox` as a fallback *when the input PBF has no bbox header*, not necessarily a clip of an existing regional extract. Any future three-engine test must validate the *actual* generated geographic extent; a common command-line bbox is insufficient evidence.

### Latest prepared-property experiment — PR #11

[Run 40958937269](https://github.com/rydolabs/osm2mbtiles/actions/runs/40958937269) on **one imported Kanto database**, A–B–B–A order, 4 SQL workers, 2×2 SQL microbatches:

| Metric | Raw projected Batch | Prepared-property Batch |
|---|---:|---:|
| Tile export median (two runs each) | 72.31 s | 59.55 s |
| Export stage change | baseline | 17.64% faster |
| One-time property cache build | — | **182.93 s** |
| Cache build + one export | 72.31 s | **242.48 s** |
| Populated tiles | 6,355 | 6,355 |
| Compressed tile payload mismatches | 0 on baseline repeat | **5,142** versus baseline |
| Full byte parity | PASS (baseline repeat) | **FAIL** |

The cache requires large extra PostGIS table copies. Tile-stage improvements **are real measurements**, but two hosted-runner repeats are insufficient to guarantee stable performance. Cache creation cost means this variant is much slower for one cold tile export even *after* the main import, and output equivalence is unproven. The 5,142 differing compressed MVT payloads might reflect feature ordering, encoding, attribute values or geometry changes; the retained artifact has only hashes, not decoded semantic diffs.

At fixed measured timings and unchanged data, amortization would require **at least 15 complete exports** from one cache (`182.93 / (72.31 - 59.55) ≈ 14.3`); this is merely illustrative, excludes cache refresh/extra disk cost and cannot be assumed for incremental updates. Cache tables are **not automatically updated** by OSC append and must not be treated as a correct long-lived incremental cache.

Other earlier Tokyo experiments also did not yield a merge-worthy result:
- Spatial locality PR #6: **1.68% slower**, byte parity passed.
- Global feature CTE inline PR #7: **13.52% slower**, despite fewer temp writes.
- `work_mem` PR #8: **0.58% faster** (too small to establish an improvement).
- Selected road/building CTE PR #9: **3.46% slower**, byte parity passed.
- Grouped MVT PR #10: ~**1.22% faster** in one run set, but **4,006** tile payload mismatches. **Do not merge.**

Across those runs, PostgreSQL's temp materialization writes were often ~3 GiB per full tile export. That is a measured bottleneck candidate in **our SQL**, not proof that all PostGIS tilers must spill 3 GiB.

## Which limitations are architectural?

| Cost / behavior | Judgment | Reason |
|---|---|---|
| Import PBF into a persistent PostGIS DB before export | **Unavoidable under our current pipeline** | Direct bulk tilers are not forced to materialize an externally queryable SQL database and indexes; PostGIS reuse amortizes the cost |
| PostgreSQL row storage/indexing, query execution and DB ↔ worker transfer | **Architectural overhead, magnitude tunable** | Generic relational representation and process boundary have costs; prepared queries, co-location, compact materialization and batching can reduce them |
| Repeated per-layer/per-tile SQL access and feature-property creation | **Implementation choice, not inherent** | A different PostGIS tiler can precompute features, aggregate regionally, or maintain a tile-indexed intermediate |
| MVT geometry conversion and clipping | **Required by any tiler** | PostGIS ST_AsMVTGeom is one implementation; alternative engines must still do equivalent tile geometry work |
| Current multi-GiB temp CTE spill | **Observed SQL/query-plan issue, not intrinsic** | Inline/materialized and grouping experiments change the spill, but speed and parity did not improve reliably |
| SQLite writer, gzip and Python multiprocessing | **Our implementation, not PostGIS itself** | These stages lie outside the database and are independently optimizable |
| Preservation of comprehensive OSM tags, many layers and legal boundaries | **Product/output-profile choice** | A different profile can be faster while emitting less/different data; speed without quality/workload parity is misleading |
| Reuse, SQL ad hoc queries, and `.osc.gz` incremental update | **Important reason to retain PostGIS** | Persistent spatial DB allows efficient reuse and changed-feature updates; first import has a cost, but updates may justify it |

PostGIS documentation: [ST_AsMVTGeom](https://postgis.net/docs/ST_AsMVTGeom.html), [ST_AsMVT](https://postgis.net/docs/ST_AsMVT.html). Planetiler documentation: [ARCHITECTURE.md](https://github.com/onthegomap/planetiler/blob/main/ARCHITECTURE.md). Tilemaker architecture/perf: [RUNNING.md](https://github.com/systemed/tilemaker/blob/master/docs/RUNNING.md), [CHANGELOG.md](https://github.com/systemed/tilemaker/blob/master/CHANGELOG.md).

## Decision / stopping rule

1. **For cold one-off regional/country PBF → MVT MBTiles speed:** prefer evaluating a dedicated direct generator (Planetiler or Tilemaker 3.x) as the primary path. Do **not** keep tuning SQL solely to beat those engines unless a new, measured end-to-end win includes import, indexes, additional cache construction and equivalent output.
2. **For repeatable SQL-based exports and genuine OSC incremental updates:** retain the current PostGIS route as a separate backend with its own performance target. Do not claim its warm, pre-import time is equivalent to direct cold builds.
3. **For code changes:** leave `main` unchanged. PR #11 and #10 remain draft/experimental while exact gzip byte parity fails. If proposing semantic-only parity, first decode all affected tiles and compare layer/feature/attribute/geometry content under a separately defined acceptance policy.
4. **For future engine comparisons:** pin the PBF checksum, versions, hardware and output profiles; actually verify geographic extent, zoom range and feature counts; use ≥3 independent runs and separate cold vs warm tracks. Current Tilemaker 3.2 results remain **unknown**.
5. **For design evolution:** if both cold-speed and incremental updates are required, consider *two explicit backends*, direct bulk export plus a maintained PostGIS incremental pipeline, understanding that identical vector profiles and consistent updates between them would require extra engineering and verification. Do not assume archive bytes are interchangeable.

**Bottom line:** The database import and relational processing costs are a real trade-off in this architecture, not a physics-like upper bound on PostGIS performance. The project's one-shot bulk-generation goal and its update/reuse goal justify evaluating **different paths** rather than maximizing a single PostGIS implementation at all costs.
