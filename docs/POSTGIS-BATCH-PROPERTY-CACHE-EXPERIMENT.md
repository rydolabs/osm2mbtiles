# PostGIS Batch attribute-cache experiment: Tilemaker 3.x ideas

**Status:** opt-in experiment only. Do not merge into main before real Tokyo byte
parity and meaningful end-to-end speedup. Main Batch SQL is unchanged.

## Hypothesis and scope

Tilemaker 3.2 reports less allocation churn in tile generation and larger
AttributeStore lookup caches (upstream:
https://github.com/systemed/tilemaker/blob/v3.2.0/CHANGELOG.md).
Our PostGIS Batch SQL repeatedly projects sparse OSM JSONB tags per tile
microbatch. Existing `vector_cache.prepare_cache` builds `fast_vector_*`
tables with geometry, tags, `props_low` and `props_high`, but Batch SQL did
not use these tables even with `--vector-query-mode cached`.

The opt-in cached Batch SQL reads `fast_vector_*` and projects class/OSM ID
plus precomputed props. All filtering on original tags, ST_AsMVTGeom,
ST_AsMVT, 2x2 tile envelopes, compression, and SQLite writing remain the
same. Regular Batch SQL, Mapnik PNG and incremental generation are unchanged.

Cached fast tables are an additional PostGIS copy, not a Tilemaker-like
compact AttributeStore. Creating them may cost more CPU, memory or disk than
it saves; this is a hypothesis, not a claim of faster performance.

## Activation

The CLI has a pre-existing cache preparation path:

```bash
python3 -m osm2mbtiles convert kanto.osm.pbf \
  --bbox 139.55,35.55,139.95,35.85 --zoom 12-16 \
  --tile-type vector --vector-tiler batch \
  --vector-query-mode cached --processes 4 --output tokyo.mbtiles
```

For direct worker access to an already imported dedicated PostGIS DB:

```bash
python3 -m osm2mbtiles.vector_cache prepare
OSM2MBTILES_VECTOR_QUERY_MODE=cached \
  python3 -m osm2mbtiles.vector_worker job.json
```

The direct worker assumes that fast tables already exist. Cached tables are
not automatically maintained after `.osc.gz` updates. Rebuild cache after
any import change; do not use cached Batch SQL for live incremental updates.

## Tokyo A–B–B–A validation gate

Workflow `.github/workflows/tokyo-cached-batch-abba.yml`: frozen same-run
Geofabrik Kanto PBF (record SHA-256); one PostGIS import; 4 workers; bbox
`139.55,35.55,139.95,35.85`, z12–16, 2x2 SQL microbatches.

A1 projected Batch; build cache ONCE (time + RSS recorded);
B1 cached Batch; B2 cached Batch; A2 projected Batch.

All four MBTiles must pass SQLite integrity and compare every stored
gzip-compressed tile_data, coordinate and metadata field exactly. A changed
physical row order in the extra cache tables might change MVT byte order.
If parity fails, diagnose whether content or just encoding order changed
before proposing another change; do not silently accept changed output.
No speed or parity claims are made until the workflow passes.

Report cache build time/disk separately AND add it to warm tile time for
cold PBF-to-MBTiles comparisons. Note A–B–B–A has two repetitions per mode
and cache creation between A1 and B1 biases page-cache temperature.

For a fair Planetiler/Tilemaker 3.2.0 comparison, pin the input, versions,
profiles and feature counts; compare full cold PBF-to-MBTiles time, not
PostGIS warm tile time alone. Prior PR #4 used Tilemaker 2.4 and ended in
failure, so it is not a confirmed 3-engine ranking.
