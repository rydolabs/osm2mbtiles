# Performance benchmarks: osm2mbtiles vs Planetiler vs Tilemaker

[Main README](../README.md) · [Korean historical measurements](BENCHMARKS.ko.md)

## Recommended internationally reproducible dataset: Berlin

Use the official [Geofabrik Berlin extract](https://download.geofabrik.de/europe/germany/berlin.html) as input to all three engines. Freeze and checksum **one local copy** of the extract before running any comparison; `-latest` changes over time.

| Tier | Input | bbox (W,S,E,N) | Zoom | Candidate XYZ cells | Purpose |
|---|---|---|---|---:|---|
| Smoke | Geofabrik Monaco | `7.41,43.72,7.44,43.75` | z12–16 | 87 | Feature and configuration smoke; too small for speed claims |
| Berlin centre | Geofabrik Berlin | `13.35,52.49,13.45,52.54` | z12–16 | 432 | Fast setup and geometry parity |
| **Berlin main** | Geofabrik Berlin | **`13.20,52.45,13.60,52.60`** | **z12–16** | **4,573** | **Primary three-engine benchmark** |
| **Berlin stress** | Geofabrik Berlin | `13.088,52.338,13.761,52.676` | z12–16 | **16,943** | **Default for internal MVT export optimization**; includes cells beyond the city extract |
| Seoul historical | independent Seoul non-military PBF | `126.75,37.40,127.20,37.72` | z12–16 | 8,418 | Historical regression; not the primary public benchmark |

All counts are exact *requested Web Mercator XYZ grid positions*, computed with `src/osm2mbtiles/tiles.py::tile_count`; they are **not the number of nonempty MBTiles records**. Geofabrik extracts follow region polygons; a rectangular bbox may contain cells with no source data. The Berlin main bbox selects a central urban rectangle, **not** the Berlin municipal boundary.

### Recorded default: Berlin vector-export performance baseline (2026-11-02)

For **internal osm2mbtiles vector tile-export optimization**, use **Berlin stress** by default. The **Berlin main** profile remains the primary *three-engine* comparison region; the two tracks have different purposes.

- Frozen PBF: [Geofabrik Berlin 2026-10-01](https://download.geofabrik.de/europe/germany/berlin-261001.osm.pbf), 99,604,748 bytes, SHA-256 `51d3845df2e4b1334ebec7cf6516e41e71735ce2fc1c0975c52d57f58f1952ad`.
- Map coverage: bbox `13.088,52.338,13.761,52.676`, zoom 12–16, **16,943 requested XYZ cells / 12,047 nonempty tiles**, 229,882,912 compressed tile payload bytes (MBTiles file 237,486,080 bytes).
- Frozen production `a29c9b9fd18ee4e85a8e9b08cb21ea51ab004024`, PostgreSQL 16.15 / PostGIS 3.4 / osm2pgsql 1.11.0; `--slim --drop --cache=384 --number-processes=1 --disable-parallel-indexing`, 4 tile workers, `projected` SQL, gzip level 6 and mtime 0.
- Initial PBF import **plus** `vector/post-import.sql`: **51 seconds** in **one run**, rounded to whole seconds; measured separately, **not** a cold end-to-end total.
- **Production legacy warm PostGIS tile export**: **125.644 / 124.870 / 124.938 seconds** (three repetitions), **124.938s median**. This is the canonical reference for future *warm tile-export* comparisons.
- Worker-side gzip: 125.347s median (**0.33% slower**), worker-side gzip plus SQLite batching: 124.974s median (**0.03% slower**). Both modes matched every stored gzip tile byte and XYZ coordinate against production legacy **within each same-DB trial**; **not worth adopting** from this evidence.

The machine-readable frozen baseline is [`benchmarks/baselines/berlin-stress-vector-postgis-20261102.json`](../benchmarks/baselines/berlin-stress-vector-postgis-20261102.json). Raw evidence and exact command execution are in [GitHub Actions run #41391824503](https://github.com/rydolabs/osm2mbtiles/actions/runs/41391824503) (artifact `vector-io-berlin-stress-evidence`). The Berlin benchmarking workflow is [`vector-export-io-profile.yml`](../.github/workflows/vector-export-io-profile.yml); default region configuration is [`regions.json`](../benchmarks/regions.json).

For a later run with the **same exact PBF SHA, bbox, zoom, worker count, tile count and gzip payload size**, compare its generated `export-io-profile.json` against the saved baseline:

```bash
python3 benchmarks/compare_berlin_baseline.py \
  --result /path/to/export-io-profile.json \
  --profile legacy \
  --output /path/to/berlin-comparison.json
```

The comparator fails closed if the workload or output size/count changes and reports median and percentage difference. **It never approves production adoption** on historical elapsed times: GitHub runners can change CPU/storage performance. A speed claim needs three or more paired **same-runner** measurements, independent source/runner version records and full initial PBF-to-MBTiles timing if the goal is end-to-end throughput. Changes to SQL, MVT order or gzip must additionally prove **every XYZ and compressed tile blob matches the frozen baseline exporter on the same imported PostGIS database**. The historical comparison's within-run byte check does **not** establish cross-revision byte identity.

### How to publish comparable results

1. Run **same unmodified PBF snapshot**, record byte size, SHA-256, download URL and timestamp. Use exact source filenames from [benchmarks/regions.json](../benchmarks/regions.json).
2. Pin the versions/commits of osm2mbtiles, Planetiler, Tilemaker, osm2pgsql, PostgreSQL/PostGIS, Java, Lua/JSON/YAML profiles and codec settings. The existing `benchmarks/seoul-planetiler.yml` is a historical **non-equivalent** comparison profile; its broad `natural` rule inflated `water`, so do not use it unchanged to claim data or quality parity.
3. Align **identical area, zoom range, bbox, tile coordinate system, source and thread budget**. Check actual generated XYZ coordinates for each engine. Do not confuse the total cells requested with populated tiles. Config can cause a tool to emit tiles outside the chosen bbox; detect this.
4. Report two **separate** performance tracks:
   - **Cold, end-to-end:** start at reading the already-downloaded PBF and finish at closing the final MBTiles. Include osm2mbtiles's PostGIS initialization, import, materialization and SQLite writes.
   - **Warm/update:** reuse PostGIS and measure only tile generation. This track is **not** directly compared to Planetiler/Tilemaker's cold end-to-end times.
5. On the same hardware and storage configuration, perform **at least 3 independent measured runs per engine** (5 preferred), alternate execution order, and publish median + spread. Record disk cold/warm page-cache behavior; do not claim a universal ranking from a hosted runner with variable CPU shares.
6. Collect elapsed time, CPU time, maximum resident memory, temporary disk peak, final MBTiles size, number of populated tiles, tiles per second, and sampled decoded features/geometry. **Faster with fewer layers** is not a like-for-like correctness comparison.
7. PNG/Mapnik and MVT/PBF must be measured separately; Planetiler and Tilemaker are **vector benchmarks** here. For comparable vector profiles, either align the feature-selection rules or clearly label **native-profile** outputs as different workloads.
8. If adding a North American or Asian dataset, freeze an official regional extract and rerun the same protocol. Berlin is a practical *primary sample*, not a comprehensive global representativeness claim.

### Publishable table template (UNMEASURED)

| Engine | Cold PBF→MBTiles median | Warm tile-only | Peak RSS | Nonempty tiles | File size | Profile |
|---|---:|---:|---:|---:|---:|---|
| osm2mbtiles | **TBD** | TBD | TBD | TBD | TBD | declared detailed vector |
| Planetiler | **TBD** | N/A | TBD | TBD | TBD | pinned comparison profile |
| Tilemaker | **TBD** | N/A | TBD | TBD | TBD | pinned comparison profile |

Do not publish performance ratios until the Berlin runs have completed and output differences are documented.

## Historical measurements (Seoul, 2026-10)

The measurements below were **performed on a Seoul extract, not Berlin**. They demonstrate how misleading tile-only vs PBF-to-MBTiles comparisons can be.

| Workload | Engine | Median | Timed interval |
|---|---|---:|---|
| One Seoul City Hall z14 tile | osm2mbtiles PostGIS projected | 1.775 s | **Warm tile stage only**; separate PostGIS import 12.600 s |
| Same one tile | Planetiler v0.10.2 | 10.355 s | PBF → MBTiles |
| Same one tile | Tilemaker v2.4 | 5.910 s | PBF → MBTiles |
| Seoul 58-tile area z12–15 | osm2mbtiles PostGIS projected | 12.526 s | Warm tile stage only |
| Same 58-tile area | osm2mbtiles Batch SQL | **1.903 s** | Warm tile stage only |
| Same 58-tile area | Planetiler v0.10.2 | 13.137 s | PBF → MBTiles |

The 58-tile PostGIS import took 12.510 s separately. Differences in data profile, feature selection, start-up, and runtime invalidate raw cross-engine speed ratios. **These historical figures are not evidence of Berlin performance.**

Evidence: [one-tile generation benchmark](https://github.com/nssang00/chatgpt-write-test/actions/runs/40826487496), [Seoul natural/boundary checks](https://github.com/nssang00/chatgpt-write-test/actions/runs/40829971952).
