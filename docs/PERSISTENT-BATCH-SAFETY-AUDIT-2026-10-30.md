# Frozen-main persistent DB Batch audit — 2026-10-30

**Decision: DO NOT switch persistent incremental initialization to Batch as-is.**
This is a **benchmark-only** experiment: no production code, vector SQL, Lua style,
gzip options, incremental synchronization protocol, or default CLI behavior changed.

## Reproducible baseline

- Frozen production main SHA: `4b7f84ce8bf7a8d4227c1e7f557e9a4056c3db7a`.
- Experiment: `benchmarks/persistent_batch_probe.py` and workflow
  `.github/workflows/persistent-batch-safe-profiling.yml`. The workflow runs
  `git diff --exit-code "$MAIN_SHA" HEAD -- src/ vector/`.
- Monaco and Kanto both use the unchanged production
  `vector_incremental.init`, `vector_incremental.append`,
  `vector_incremental.flush_tiles`, and `vector_batch.export_batch`.
  A benchmark-only temporary Python wrapper captures timings.
- Kanto input: the **whole Geofabrik Kanto PBF** with SHA256
  `1af62f945ecb25a1c52db30967d7d2db1c6aaea5a17f1dbb1d53dbe55691040e`.
  **Output only** central Tokyo bbox `139.745,35.675,139.77,35.70`,
  zoom 12–16; 75 nonempty tiles. **NOT full Kanto or the earlier Tokyo 6,355-tile output**.
- Synthetic `.osc.gz`: one created hospital node, not a real OSM replication batch.
  One disposable Ubuntu 24.04 runner per run, PostgreSQL 16/PostGIS 3.4,
  osm2pgsql 1.11, 4 render workers, Batch 2×2, `projected` vector query mode.
- Independent engine data profiles and independent PostgreSQL imports are **not**
  compared here. The point of this experiment is **same-DB** legacy-vs-Batch.

## Kanto: two independent experiment runs

| Metric | First Kanto run | Semantic diagnostic repeat |
|---|---:|---:|
| Persistent DB initial import (`osm2pgsql --slim`, retains middle) | 432.978 s | 623.373 s |
| Derived `post-import.sql` (initial) | 5.904 s | 17.668 s |
| Original per-tile full export | 11.885 s | 16.330 s |
| **Persistent initialization overall** | **451.004 s** | **657.406 s** |
| Independent Batch full export on the same DB | 3.491 s | 4.598 s |
| Initial gzip byte mismatches / tiles | **44 / 75** | **44 / 75** |
| After OSC append+flush gzip byte mismatches / tiles | **50 / 75** | **50 / 75** |
| Initial decoded semantic parity | not separately captured | **PASS** |
| Updated decoded semantic parity | not separately captured | **PASS** |
| Append + full derived SQL refresh | 4.438 s | 3.923 s |
| Dirty-tile flush | 10.202 s | 13.456 s |

The two initializations have significant runner variation. They must NOT be
combined with the earlier `--slim --drop`, 4-process 261.13 s Kanto import
or 118.85 s **full Tokyo** Batch export into a single measured workflow.

**Semantic parity in the diagnostic repeat:** all 75 XYZ coordinates and
**602,370 initial decoded features** match, including layer, feature ID,
geometry/type, properties and duplicate count. After one hospital node
change, both have **602,375** decoded features with 0 differing tiles.
Metadata matches except the expected archive filename. Byte differences are
not a demonstrated missing-feature bug but violate the pinned stable baseline's
**compressed tile byte compatibility** requirement.

**Incremental safety in both Kanto runs:** 5 expired XYZ, 5 actual changed gzip
tiles, no changed gzip bytes outside expired coordinates, hospital visible in z16,
empty flush idempotent, duplicate OSC replay rejected, SQLite integrity passes.
These are synthetic-node checks and do not replace the separate full-rebuild CI
for relations/ways or genuine replication streams.

## Monaco independent smoke

- Input PBF SHA256:
  `67edcf8da5d0ae0d82c51055a7b2ec1b374faf55ebd51d45339bcebca708beac`.
- Initial persistent DB init (includes legacy exporter): **1.803 s**.
  Separate Batch exporter: **0.491 s**.
- 67 nonempty z12–16 tiles; **50 gzip mismatches** both before and
  after a synthetic OSC.
- All decoded initial and updated features match exactly; 5 dirty,
  5 actually changed, unexpired gzip preserved, duplicate diff rejected.

## Read-only query-plan diagnostics (Kanto semantic diagnostic run)

| Representative 2×2 Batch SQL | Actual execution | Shared buffers hit/read (8KiB blocks) | Temp blocks read/write |
|---|---:|---:|---:|
| z12 | 2,134.24 ms | 42,186 / 97,393 | **15,549 / 5,183** |
| z14 | 582.35 ms | 18,747 / 1,330 | 0 / 0 |
| z16 | 50.28 ms | 4,142 / 0 | 0 / 0 |

These are **individual diagnostic queries**, not averages or export duration
at each zoom. The z12 query actually spilled intermediates to temporary disk
under the diagnostic runner's 8 MiB `work_mem`. Existing Batch SQL references
`vector_lines` and `vector_areas` repeatedly for different vector layers.
This warrants further analysis but does **not** justify raising `work_mem`,
adding property caches, or changing query/feature ordering in production.
Any change would need matched, independent 3+ runs, memory accounting and
the strict gzip parity gate.

## Existing compatibility tests

The final experiment-code revision passed:
- Project CI.
- Real Python Mapnik PNG smoke.
- Existing real PostGIS `.osc` → independent full-rebuild decoded parity CI.
- Unit tests for strict gzip/XYZ comparison and EXPLAIN summary.
- Frozen-main production-source equality check in the profiling workflow.

**The diagnostic workflow is marked successful** when all semantic and
incremental correctness checks pass, even if `status=BYTE_PARITY_BLOCKED`.
That status is a deliberate **negative adoption result**, not a release-ready
performance optimization. The original byte-equality gate in the first run
failed, correctly refusing to treat mismatches as a pass.

## Decision and safe follow-up

1. **Retain frozen `main`, original incremental initialization, and current
   opt-in Batch unchanged.** Do not merge a Batch-init conversion; gzip payloads
   are not equivalent. Do not weaken output-stability checks.
2. This experiment establishes where time goes. With this small output window,
   retained PostgreSQL import dominates the initial setup, while Batch export
   reduces a smaller component.
3. Any follow-up should focus on **independently measured** importer settings
   (memory, worker count, indexing) with conservative defaults, or on read-only
   Batch SQL plan analysis; neither optimization has been accepted from this
   experiment. Do not introduce property cache tables, global SQL rewrites, or
   automatic diff retries.

## Actions and evidence

- First dual-region run (original strict byte gate, failure on mismatch):
  https://github.com/rydolabs/osm2mbtiles/actions/runs/41006634900
- Final Monaco semantic diagnosis:
  https://github.com/rydolabs/osm2mbtiles/actions/runs/41006844945
- Kanto semantic diagnostic repeat (PASS checks, **BYTE_PARITY_BLOCKED** result):
  https://github.com/rydolabs/osm2mbtiles/actions/runs/41006824547
- Original stable baseline:
  [POSTGIS-STABLE-BASELINE.md](POSTGIS-STABLE-BASELINE.md)
