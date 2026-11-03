# Live conversion progress

The Windows WSL one-click launcher and the Linux CLI write progress directly to
the **same terminal**, flushing output promptly. No additional UI app is needed.

## What is measured

| Prefix | When it appears | What it means |
|---|---|---|
| `[STAGE] 1/6` ... `6/6` | WSL launcher | Named workflow stages **not** an end-to-end percentage |
| `[IMPORT]` / `[POSTGIS]` | Long-running native tools | Heartbeat every 15 seconds; elapsed time only, not guessed OSM object progress |
| `[RASTER]` | Mapnik renderer | Exact completed XYZ tiles / planned XYZ tiles, observed new tile throughput and ETA |
| `[VECTOR]` | Default PostGIS MVT exporter | Completed XYZ queries / planned XYZ tiles, throughput and ETA |
| `[VECTOR-BATCH]` | Experimental batch exporter | Completed XYZ tiles across finished SQL microbatches, throughput and ETA |
| `[TIME]` | After conversion | Real end-to-end elapsed time, including verification and Windows copy when run from WSL |

**Example output (illustration, not a benchmark):**

```text
[STAGE] 1/6 | Inspect WSL resources and isolated cache | elapsed=00:00
[STAGE] 5/6 | Calculate tile plan, import OSM PBF and render MBTiles | elapsed=02:11
[IMPORT] osm2pgsql still running | elapsed=00:15 (no measured per-object progress)
[RASTER] Mapnik render start | 10,000 planned tiles, 0 already cached | zoom=12-15
[RASTER]  36.0% | 3,600/10,000 tiles | 120.0 tile/s | ETA 00:53 | metatiles=56 z14
[VECTOR]  70.0% | 7,000/10,000 tiles | nonempty=6,500 | 300.0 tile/s | ETA 00:10
[STAGE] 6/6 | Verify MBTiles and byte-check Windows copies | elapsed=04:12
[TIME] WSL conversion and copy finished | elapsed=04:20
```

### Interpretation and limitations

- These percentages describe **tile processing only**, never total time from PBF to completed MBTiles. OSM import, SQL postprocessing, file copy and Carto external assets do not have a reliable total-task denominator, so they are shown as named stages and quiet-command heartbeats.
- The throughput is an **observed average** (completed tiles divided by elapsed time); ETA is an extrapolation, **not a promise**. It can jump with zoom, geometry density, disk performance or a long Mapnik metatile.
- When a Raster partial MBTiles exists, cached tiles count toward the denominator, but the throughput numerator counts **only newly rendered tiles**. A resumed zero-work run may display `ETA --:--`.
- Raster prints its exact progress after each completed metatile (at integer-percentage changes or at least every 5 seconds when a metatile finishes). A single long-running Mapnik call can temporarily stop numeric progress; the native worker heartbeat continues every 15 seconds.
- Default Vector SQL progress reports after returned tiles. Batch progress reports after returned SQL microbatches, never before.
- All displays use `print(..., flush=True)` and retain the original export/verify logic; no changes to geometry generation, PostGIS queries, raster pixels, MBTiles data, or database schemas are intended.

See [README](../README.md), [한국어 README](../README.ko.md), and
[Windows+WSL2 release test](WINDOWS-WSL2-RELEASE-TEST.md) (on release
candidate PR #32, not necessarily present on main).
