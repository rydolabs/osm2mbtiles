-- Detailed OSM vector source for osm2mbtiles (osm2pgsql flex >= 1.8).
-- Separate from openstreetmap-carto-flex.lua: do not change raster semantics.
-- Preserve all tagged nodes/ways/relations as JSONB, excluding only obvious
-- editor/import provenance. Geometries are Web Mercator EPSG:3857.
local points = osm2pgsql.define_node_table('vector_points', {
  { column = 'tags', type = 'jsonb' },
  { column = 'geom', type = 'point', projection = 3857, not_null = true },
})
local lines = osm2pgsql.define_way_table('vector_lines', {
  { column = 'tags', type = 'jsonb' },
  { column = 'geom', type = 'linestring', projection = 3857, not_null = true },
})
local areas = osm2pgsql.define_area_table('vector_areas', {
  { column = 'tags', type = 'jsonb' },
  { column = 'geom', type = 'geometry', projection = 3857, not_null = true },
})
local relations = osm2pgsql.define_relation_table('vector_relations', {
  { column = 'tags', type = 'jsonb' },
  { column = 'geom', type = 'multilinestring', projection = 3857, not_null = true },
})
local polygon_keys = {
  'building', 'building:part', 'landuse', 'landcover', 'natural', 'leisure',
  'amenity', 'shop', 'tourism', 'historic', 'aeroway', 'water', 'wetland',
  'boundary', 'man_made', 'military', 'power', 'public_transport',
}
local function clean_tags(tags)
  -- Never mutate the shared osm2pgsql OSM object: the same object is also
  -- processed by upstream OSM Carto in the combined flex importer.
  local kept = {}
  for k, v in pairs(tags) do
    if k ~= 'source' and k ~= 'source:date' and k ~= 'created_by' and
       k ~= 'note' and k ~= 'fixme' then
      kept[k] = v
    end
  end
  if next(kept) == nil then return nil end
  return kept
end
local function is_area(tags)
  if tags.area == 'no' then return false end
  if tags.area == 'yes' then return true end
  -- Line-nature feature precedence; closed river/roads/rails generally remain lines.
  if tags.highway or tags.railway or tags.barrier or tags.waterway then
    return (tags.landuse or tags.building or tags.leisure or tags.amenity) ~= nil
  end
  for _, key in ipairs(polygon_keys) do
    if tags[key] ~= nil then
      -- natural=coastline/cliff/ridge/tree_row are linear, not polygons.
      if key == 'natural' and (tags.natural == 'coastline' or tags.natural == 'cliff'
          or tags.natural == 'ridge' or tags.natural == 'tree_row') then
        return false
      end
      return true
    end
  end
  return false
end
function osm2pgsql.process_node(object)
  local tags = clean_tags(object.tags)
  if not tags then return end
  points:insert({tags=tags, geom=object:as_point()})
end
function osm2pgsql.process_way(object)
  local tags = clean_tags(object.tags)
  if not tags then return end
  if object.is_closed and is_area(tags) then
    areas:insert({tags=tags, geom=object:as_polygon()})
  else
    lines:insert({tags=tags, geom=object:as_linestring()})
  end
end
function osm2pgsql.process_relation(object)
  local tags = clean_tags(object.tags)
  if not tags then return end
  if tags.type == 'multipolygon' or tags.type == 'boundary' then
    areas:insert({tags=tags, geom=object:as_multipolygon()})
  end
  if tags.type == 'route' or tags.type == 'route_master' or
     tags.type == 'boundary' then
    relations:insert({tags=tags, geom=object:as_multilinestring()})
  end
end