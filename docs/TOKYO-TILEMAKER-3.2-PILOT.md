# Tokyo Pilot: Tilemaker 3.2 and basezoom failure diagnosis

Original [three-engine run](https://github.com/rydolabs/osm2mbtiles/actions/runs/40850574178)
did not establish Tilemaker speed: the v2.4 command exited immediately with:
`maxzoom must be the same or smaller than basezoom`. Settings were
`maxzoom=16`, `basezoom=14`.

This experiment pins Tilemaker v3.2.0 source (Git SHA recorded), builds it
independently from timed conversion, downloads the corresponding v3.2.0
OpenMapTiles-like Lua/JSON resources, fixes `basezoom=maxzoom=16`, and
extends the native OMT z14 layer caps to z16 to avoid measuring less work.
This deliberate profile change is documented and is NOT proof of equivalent
feature schemas across all three engines. Planetiler stays pinned to v0.10.2
to isolate the Tilemaker version change.

From that failed **single-run** hosted benchmark:
- PostGIS initialization + Kanto PBF import: 259.35 seconds.
- PostGIS tile stage (Tokyo z12-16): 124.36 seconds.
- Planetiler PBF-to-MBTiles: 89.97 seconds.
- Tilemaker v2.4: no timing (invalid settings).

PostGIS component sum 383.71 sec includes import; the tile-only comparison
against Planetiler is not like-for-like. All 3 pipelines must pass SQLite,
XYZ bounds, nonempty tile and sampled feature validations before publication.
A fair ranking requires >=3 repeats with equal feature profiles, memory and
disk reporting, and a defined cold start. Do not merge benchmark results
into main README until those conditions are met.
