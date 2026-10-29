# Stable PostGIS baseline — 2026-10-30

**Status:** Adopted baseline for subsequent OSM vector performance/correctness work.  
**Release code snapshot before this documentation:** `03bcde4199b1f7bb1f33a92899a690cbb649604a` (main after PR #13 and #14).  
**Frozen older byte reference:** `8968573f29508b0934d69bc3b0f955e9d11fa591` (main after PR #5).  
**Product priority:** correct PostGIS-backed incremental **vector** updates and stable tile output, not the fastest one-time cold PBF-to-MBTiles converter.

## What is now accepted in main

1. **PR #5 — PostGIS Batch performance improvement, already merged.** Prepared PostgreSQL queries, deterministic gzip (level 6, mtime 0) in workers, SQLite `executemany` batches of 256, commits after 1,024 nonempty tile writes. `batch` remains opt-in and default `postgis` remains unchanged.
2. **PR #13 — safety gate, merged.** The Batch worker rejects missing/duplicated XYZ rows before touching SQLite. Real osm2pgsql `--append` regression verifies independent full-rebuild **decoded** parity; unaffected tiles retain gzip bytes; empty flush is byte-idempotent. A separate same-database A/B compares every compressed MBTiles tile to the **pinned pre-safety baseline commit** before and after a diff.
3. **PR #14 — reproducible maintenance benchmark, merged.** Automated two-snapshot cumulative timing (PostGIS persistent init+append+flush versus Planetiler two full `generate-custom` builds). Real Monaco smoke and test fixtures, pinned tool version, input SHA, synthetic change and distinct profiles disclosed. Does **not** modify output generators or update logic.

**No experimental performance PR #6–#11 is part of this baseline.** No change to PostGIS vector feature schema, MVT encoder, MVT feature ordering, compression options or incremental database profile fingerprint was accepted.

## Frozen performance evidence (not interchangeable benchmarks)

| Experiment | Timed scope | Result | Verdict |
|---|---|---|---|
| [PR #5 / real Kanto-Tokyo](https://github.com/rydolabs/osm2mbtiles/actions/runs/40858593784) | Same imported Kanto DB, Tokyo bbox `139.55,35.55,139.95,35.85`, z12–16; Batch **tile export only**, one ordered pair | 125.14s → 119.24s (4.7% faster), 6,355/6,355 compressed MVT tile payloads identical | **Accepted**, but small one-pair warm-cache effects remain possible |
| [Original three-engine pilot](https://github.com/rydolabs/osm2mbtiles/actions/runs/40850574178) | Cold regional import vs Planetiler PBF-to-MBTiles; DIFFERENT profiles and timed stages | Kanto import/init 259.35s + Tokyo Batch tiles 124.36s = 383.71s component sum; Planetiler 89.97s | **Context only**, not like-for-like or current Batch tuning result |
| [PR #11 property cache](https://github.com/rydolabs/osm2mbtiles/actions/runs/40958937269) | Warm Tokyo Batch 2-run medians | 72.31s → 59.55s warm (17.64%), extra cache build 182.93s; **5,142/6,355 tile bytes changed** | **Rejected** despite warm speedup |
| PR #6 locality; PR #7 CTE inline; PR #8 `work_mem`; PR #9 hot CTE | Tokyo A–B–B–A | Respectively 1.68% slower, 13.52% slower, 0.58% faster, 3.46% slower | **Rejected**: regression or no reliable useful gain |
| PR #10 grouped MVT | Tokyo Batch | ~1.22% faster but 4,006 gzip tile mismatches | **Rejected**: output changed |
| [PR #14 Monaco](https://github.com/rydolabs/osm2mbtiles/actions/runs/40973051076) | **Initial + one OSC update** vs **two Planetiler full builds**, tiny region z12–16, one synthetic POI | 2.149 + 0.875 = **3.024s** PostGIS vs 6.494 + 4.643 = **11.137s** Planetiler; PostGIS independent full rebuild decoded parity PASS | **Smoke only**; profiles differ (40,164 vs 32,783 final features), one runner/one diff, **not Kanto evidence** |

The 383.71s Kanto figure is **not Batch tile time** and must never be presented as such. The two Tokyo tile-only datasets were measured in different workflows/runner/cache circumstances. There is no defensible automatic substitution of 119.24s for 124.36s in the old complete-run figure.

## Baseline correctness contract

- **Same frozen OSM input + same imported PostGIS DB, previous baseline vs new code:** all MVT `tiles.tile_data` gzip **bytes** and all XYZ rows must match, except an explicitly accepted, independently reviewed output-format change. Archive `metadata` must match except output filename.
- **Incremental update vs independent full reimport at the same final OSM state:** exact decoded layer, feature identity/geometry/attributes/multiplicities and tile coordinate sets. The synthetic real PostGIS test has **one known cross-DB gzip/underlying MVT mismatch out of 19 final tiles** despite decoded semantic parity. Do not silently call this a byte-for-byte pass, and do not rewrite feature order just to hide it.
- **Unexpired XYZ:** original gzip payload bytes must remain unchanged after `.osc.gz`. Empty flush must not rewrite data.
- **Failure safety:** an uncertain osm2pgsql append must never be retried automatically. Keep existing advisory lock, durable phase marker and repeatable flush behavior.
- **Vector vs raster:** existing vector-only experimental `.osc.gz` is tested; PNG incremental refresh and production replication feed are NOT certified.

The A/B reference in `.github/workflows/incremental.yml` is pinned to `8968573f29508b0934d69bc3b0f955e9d11fa591`; it must not use moving `origin/main`, which would silently compare a merged codebase against itself. Changing this baseline reference requires an explicit output compatibility decision, new fixture parity and documentation.

## Benchmark protocol after baseline

- Compare **PostGIS initial PBF import + first full export + all `.osc.gz` append/derived SQL/dirty flushes** with **Planetiler full PBF→MBTiles for each published OSM state**. Show a separate Planetiler PBF source-preparation time, and whether it is included.
- Report initial construction, append, derived-table SQL refresh, flush and two-snapshot cumulative timings separately. The current append implementation refreshes derived polygon labels over the database; do **not** assume all update costs scale only with the dirty-tile count.
- Pin PBF checksum, updates/sequence, zoom, bbox, layer/profile, tool versions, hardware and worker count. Validate both engines reflect the source diff. Show output counts and **different layer schemas** without implying output-equivalent throughput.
- Require at least three independent larger-region executions before publishing a speed advantage. Monaco is a workflow smoke and single synthetic change, not evidence of general superiority.
- Evaluate meaningful improvements only if compressed output stays the same and there is repeated, end-to-end net benefit. Reject marginal gains requiring new intermediate tables, complex migrations or uncertain update behavior.

## Future change acceptance

1. Start from the **frozen stable baseline**, not one of the experimental branches. Check CI, real Mapnik smoke and real PostGIS incremental full-rebuild parity.
2. Each proposed change has a concrete correctness hypothesis, output byte gate and measurement protocol. Avoid mixing multiple optimization ideas in a single PR.
3. If there is no repeated material gain (rough guide ≥5% under matched workloads), or if byte parity fails, **stop and keep this baseline**.
4. Maintain the established identity: **OSM Carto PNG + PostGIS MVT builder with incremental vector maintenance**. A direct PBF tiler is an optional separately evaluated tool, not a prerequisite or automatic replacement for the PostGIS architecture.

**Original reports are retained under their PRs/Actions; only accepted changes belong in main.**
