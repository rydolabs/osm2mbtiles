# Vector MVT/PBF layer and attribute reference

[README](../README.md) · [한국어 README](../README.ko.md) · [Vector source implementation](../src/osm2mbtiles/vector.py)

This page describes the **default PostGIS vector exporter** (`convert --tile-type vector --vector-tiler postgis --vector-query-mode projected`) and the corresponding persistent vector incremental pipeline. It is derived from the repository's `LAYERS`, `ZOOM_FILTER`, `LOW_FIELDS`, `ALL_FIELDS`, `_properties` and `metadata` definitions. It is **not** an OpenMapTiles/Mapbox Streets schema, and no off-the-shelf style compatibility is promised.

## Quick contract

- **Input**: OSM `.osm.pbf` (original OSM objects). **Output**: MBTiles SQLite archive of **gzip-compressed Mapbox Vector Tile (MVT/PBF)** protobuf payloads; the file is not an original OSM PBF.
- **Tile coordinates**: tile creation uses XYZ; the `tiles.tile_row` column is TMS, so `tile_row = (1 << z) - 1 - y`. Layer geometries use MVT extent **4096** and buffer **64** units.
- **Layers**: 19 logical names. `poi` and `housenumber` aggregate point nodes and derived polygon label points **into a single MVT layer each**, without duplicate layer names.
- **Per-feature properties**: `osm_id` is a generated **string**, and `class` is derived from layer-specific tag priorities. All other exported keys come from a fixed, **zoom-dependent allowlist** of original OSM tag strings. Keys not in the allowlist are not exported.
- **Sparsity**: keys not present on the source object are omitted, not emitted as empty placeholders. A layer's documented zoom is the **minimum eligible zoom**, not a guarantee that any particular tile contains that layer. `class` can also be absent when none of its source tags exists.
- **Not the original OSM schema**: the MBTiles `metadata.json.vector_layers[].fields` advertises the available field schema (with type `String`), **not** which keys are actually present on every feature at every zoom.

## Layer catalog

Every row comes from the current `LAYERS` declaration. "Class priority" means the **first available** OSM tag value in the ordered list becomes the `class` property; it is not a separate classification vocabulary. All included tags retain their original text values.

| Layer ID | Min z | Expected MVT geometry | Source and inclusion rule | `class` tag priority |
|---|---:|---|---|---|
| `water` | 5 | Polygon | Areas with `natural=water`, a `water` tag, or `landuse=reservoir` | `water` → `natural` |
| `waterway` | 8 | Line | Ways with a `waterway` tag | `waterway` |
| `landcover` | 7 | Polygon | Areas with `landcover` or selected `natural` values, including `rock` | `natural` → `landcover` |
| `landuse` | 9 | Polygon | Areas with a `landuse` tag | `landuse` |
| `boundary` | 3 | Line | Administrative boundary relations (`boundary=administrative`) | `boundary` |
| `boundary_legal` | 12 | Line | Ways with `boundary=legal`, distinct from administrative relation boundaries | `boundary` |
| `road` | 6 | Line | Highway ways except footways/cycleways/paths/steps/pedestrian/bridleways | `highway` |
| `pathway` | 12 | Line | `highway=footway/cycleway/path/steps/pedestrian/bridleway` ways | `highway` |
| `railway` | 8 | Line | Ways with a `railway` tag | `railway` |
| `aeroway` | 10 | Line | Ways with an `aeroway` tag | `aeroway` |
| `aeroway_area` | 11 | Polygon | Areas with an `aeroway` tag | `aeroway` |
| `building` | 13 | Polygon | Areas with `building` other than `no` | `building` |
| `building_part` | 14 | Polygon | Areas with a `building:part` tag | `building:part` |
| `leisure` | 10 | Polygon | Areas with a `leisure` tag | `leisure` |
| `place` | 5 | Point | Nodes with a `place` tag | `place` |
| `poi` | 12 | Point | Tagged nodes **and area label points** with `amenity`, `shop`, `tourism`, `office`, `craft`, `historic`, or `leisure` | `amenity` → `shop` → `tourism` → `office` → `leisure` |
| `transport` | 11 | Point | Nodes with `public_transport`, relevant `railway` stations/stops, or `highway=bus_stop` | `public_transport` → `railway` |
| `power` | 12 | Line | Ways with a `power` tag | `power` |
| `housenumber` | 15 | Point | Addressed nodes **and area label points** with `addr:housenumber` | `addr:housenumber` |

Geometry reflects the underlying vector source tables, and clipping may affect the final tile shape. Polygon label points are calculated from an area's `ST_PointOnSurface(geom)`, so a building or other tagged area can produce an additional `poi` or `housenumber` point.

### Zoom-dependent feature filtering

Apart from minimum zoom levels above, these conditions further reduce low-zoom feature density:

| Layer | Zoom | Additional restriction |
|---|---|---|
| `road` | z6–7 | `highway=motorway` or `trunk` |
| `road` | z8–9 | Also `primary` and `secondary` |
| `railway` | z8–9 | `railway=rail` or `subway` |
| `waterway` | z8–10 | `waterway=river` or `canal` |
| `place` | z5–6 | `place=country/state/city` |
| `place` | z7–9 | Also `place=town` |
| `poi` | z12–13 | Selected important `amenity` (hospital, university, school, townhall, police, fire_station) or `tourism` (museum, attraction) |
| `boundary` | z3–7 | Numeric `admin_level` up to 4 |

At higher zooms, the additional restriction shown for each row no longer applies; the main layer inclusion predicate still applies. A POI tagged *only* `craft` or `historic` can enter the `poi` layer at z14+, but may have **no `class`**, because those tags are not part of the POI class fallback.

## Feature attributes

### Always synthesized

| Property | Type | Meaning and caveats |
|---|---|---|
| `osm_id` | String | Synthetic value made by concatenating the **database ID column name** and its value, e.g. `node_id123` or `way_id456`. Derived area label points use an `osm_id`-prefixed source value. This is **not** the standard OSM numeric ID and is not the protobuf's numeric feature ID. |
| `class` | String, if available | Value of the first present tag listed in the layer's "Class priority" column. Can be missing; not necessarily the same as the layer name. |

No `name`, `highway`, `building`, or other original OSM tag is mandatory for **every** layer/feature. For example, road features normally have `highway`; a feature may lack `name`. All allowed OSM tag values are exported as **strings** (including numbers such as `maxspeed`, `building:levels`, `height` and `admin_level`).

### z0–12: low-zoom allowlist (20)

The exact allowed keys in `LOW_FIELDS` (plus generated `class` and `osm_id`):

<!-- schema-fields-low -->
```text
name, name:ko, name:en, name:ja, name:zh, ref,
alt_name, short_name, official_name, int_name, wikidata,
highway, natural, waterway, railway, landuse, place,
admin_level, boundary, population
```
<!-- /schema-fields-low -->

A z12 `poi` can contain `class=hospital` even though its original `amenity` key is not in this low-zoom allowlist. This is by design: `class` is synthesized separately from the selected tag keys. **Do not depend on `amenity` at z12.**

### z13+: detailed allowlist (74)

The exact allowed keys in `ALL_FIELDS` (plus generated `class` and `osm_id`):

<!-- schema-fields-high -->
```text
name, name:ko, name:en, name:ja, name:zh, ref,
alt_name, short_name, official_name, int_name, wikidata,
highway, foot, bicycle, horse, access, motor_vehicle,
oneway, maxspeed, lanes, surface, smoothness, width,
bridge, tunnel, layer, tracktype, service, lit,
building, building:part, building:levels, height,
min_height, roof:shape, roof:height, addr:housenumber,
addr:street, addr:city, addr:postcode, amenity, shop,
tourism, office, brand, operator, opening_hours,
phone, website, cuisine, landuse, landcover, natural,
water, waterway, intermittent, railway, aeroway,
power, public_transport, route, network, leisure,
boundary, admin_level, place, population, religion,
historic, man_made, barrier, wheelchair, ele, sport
```
<!-- /schema-fields-high -->

`craft` is an example of an OSM tag that can **select** a POI but is not in `ALL_FIELDS`; do not assume all tags from the input PBF are included. Unlisted arbitrary `name:xx` translations are not exported; only the explicitly allowed name language keys are.

## Use the layers in your map

A vector tile style must reference **these exact layer IDs**. For example, this is a **MapLibre GL style layer fragment**; it is *not* a complete style file:

```json
{
  "id": "osm2mbtiles-primary-roads",
  "type": "line",
  "source": "osm2mbtiles",
  "source-layer": "road",
  "filter": ["==", ["get", "class"], "primary"],
  "paint": { "line-color": "#d58b5c", "line-width": 2 }
}
```

Configure a MapLibre `vector` source named `osm2mbtiles` and a **separate MBTiles tile server** that serves the archive as XYZ `/{z}/{x}/{y}.pbf` with correct HTTP headers; this repository generates MBTiles but does **not** provide a built-in public tile server or a ready-to-use MapLibre style. Styling decisions and feature-label collision handling belong to the client.

For multi-zoom styling, `class` is usually the most convenient classifier. Tag-specific expressions like `["get","amenity"]` only work where that original tag is in the zoom allowlist (z13+) and is present on the feature.

## Inspect a real MBTiles archive

After creating `berlin.mbtiles` with the README's Berlin bbox and z14 included:

```bash
python3 -m pip install 'mapbox-vector-tile>=2,<3'
python3 - <<'PY'
import gzip
import json
import sqlite3
import mapbox_vector_tile

z, x, y = 14, 8802, 5373  # XYZ example; tile_row stored as TMS
with sqlite3.connect("berlin.mbtiles") as db:
    info = json.loads(db.execute(
        "SELECT value FROM metadata WHERE name='json'"
    ).fetchone()[0])
    print("Declared layers:", [v["id"] for v in info["vector_layers"]])
    row = db.execute(
        "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
        (z, x, (1 << z) - 1 - y),
    ).fetchone()
    if row is None:
        raise SystemExit("No nonempty tile at this XYZ")

tile = mapbox_vector_tile.decode(gzip.decompress(row[0]))
print("Layers actually in tile:", sorted(tile))
for layer in ("road", "building", "poi"):
    features = tile.get(layer, {}).get("features", [])
    print(layer, "features:", len(features))
    if features:
        print(json.dumps(features[0]["properties"], ensure_ascii=False, indent=2))
PY
```

A decoded illustrative **high-zoom** road property object might resemble this (the values are an **example**, not claimed from a particular Berlin feature):

```json
{
  "osm_id": "way_id456",
  "class": "primary",
  "highway": "primary",
  "name": "Example Street",
  "maxspeed": "50",
  "surface": "asphalt"
}
```

**Real-data evidence:** [Berlin z14 original and after synthetic café update](assets/readme-preview-evidence.json) proves one POI was added (`2875 → 2876`) while existing road/building feature counts remained the same in that sample tile. It is **not** a complete audit of all 19 layers at all zooms.

## Compatibility and maintenance

- `--vector-tiler postgis` with `--vector-query-mode projected` is the documented default. The experimental `batch` and `cached` paths are **not** alternative schema guarantees; strict gzip-byte parity failed in earlier optimization trials.
- The incremental updater stores a source/profile fingerprint. Changes to vector code/schema can require **reinitializing its persistent PostGIS database** before future diff appends; do not assume old databases accept new schema definitions.
- This document is backed by an automated source-to-document contract test (`tests/test_vector_schema_documentation.py`) that checks layer IDs/minimum zooms/class tag priorities and both allowlists. The **implementation** remains the ultimate reference if this documentation is outdated.
