# Output stability and conservative PostGIS change policy

## Product priorities

1. **Correctness first:** a fresh full vector build and a persistent-DB OSM
   diff update must produce equivalent tiles after the same logical OSM state.
2. **No unnecessary change to output bytes:** for an unchanged dataset,
   candidate refactors must not change any tile's compressed MVT bytes
   (not just its decoded geometry), coordinate set or metadata.
3. **Do not rewrite unaffected tiles:** an OSM diff must not change tile bytes
   outside the explicitly expired XYZ tiles.
4. **Safe recovery:** missing archives, crash/retry of a pending flush and
   accidental duplicate diffs must fail closed or be idempotently recoverable.
5. **Only then performance:** index/SQL query-plan improvements without output
   changes are candidates; do not ship changes requiring different MVT order,
   feature schemas, encoders or a second cache for a marginal speed gain.

## Regression gates

- `python -m pytest -q`: the PostGIS Batch worker now rejects any missing
  **or duplicate** SQL XYZ rows before gzip/SQLite output. Existing writer tests
  keep checking deterministic gzip, transactions and complete XYZ coverage.
- `.github/workflows/incremental.yml`: real osm2pgsql `--append` with
  synthetic node/way create, edit, delete, movement and derived polygon
  labels, compared with a fresh independent reference database.
- Incremental CI must compare **every gzip tile_data BLOB byte-for-byte**
  between released `main` and this PR, on the **same imported PostGIS DB**,
  both before and after a real OSM diff. It also verifies full decoded feature
  parity against a freshly imported independent DB, unchanged bytes for tiles
  outside the expired set, and no byte changes after replaying an empty flush.
- The independent full rebuild vs incrementally updated database currently
  has a *known* byte discrepancy: in the real synthetic fixture, 1 of 19
  nonempty final tiles differed despite exact decoded feature/geometry/tag
  parity. Record this as an independent cross-DB byte diagnostic (with
  compressed and uncompressed checks), not a new blocker for an otherwise
  output-preserving validation-only PR. Do not alter feature order or accept
  changed output to hide the discrepancy. Investigate separately.
- Compare content of the `tiles` and `metadata` tables; do not require
  identical *whole SQLite file* hashes, because SQLite page ordering,
  free-lists and transactions need not match while stored tile data does.
- For changes to the MVT SQL generator, run an A/B against the prior
  released commit and compare every tile blob (and metadata) for **the same
  frozen OSM input and the same imported database**. The workflow fetches the frozen production baseline
  `8968573f29508b0934d69bc3b0f955e9d11fa591` explicitly and records
  that commit hash. It must not fetch the *moving* `main` branch, which would
  turn the test into a self-comparison after merge. A new-to-new semantic
  parity test alone is not sufficient to establish old-vs-new byte stability.
- The small synthetic incremental suite is an essential CI gate but does not
  establish correctness for nationwide OSM relations or production
  replication. Record its scope honestly.

## Architectural scope

- Keep existing PostGIS schema, layer SQL, MVT encoder, feature ordering,
  gzip implementation, slim tables and SQLite writer.
- Prefer safety checks, SQL index analysis and redundant-I/O elimination
  that pass the exact byte and incremental gates.
- Do not merge experimental grouped MVT or precomputed-property PRs while
  their compressed tile parity gates fail.
- Changing `src/osm2mbtiles/vector.py` or
  `src/osm2mbtiles/vector_incremental.py` changes the incremental profile
  fingerprint on existing persistent databases; plan compatibility and
  migration explicitly before touching those modules.
- Incremental OSM updates are currently **experimental vector-only**
  functionality. Raster incremental rerendering, automated replication,
  checkpointed production recovery and nation-scale parity remain separate
  work.

## Stop criterion

Stop speed tuning when repeated tests show no meaningful net gain **with
identical output**, or when implementation/risk outweighs the expected
benefit. Position osm2mbtiles as an OSM Carto Raster + Vector MBTiles builder
with an automated PostGIS workflow and a developing **incremental maintenance**
capability—not as the fastest cold PBF converter.
