-- Only application-owned vector_* tables are affected.
CREATE INDEX IF NOT EXISTS vector_points_geom_idx ON vector_points USING GIST (geom);
CREATE INDEX IF NOT EXISTS vector_lines_geom_idx ON vector_lines USING GIST (geom);
CREATE INDEX IF NOT EXISTS vector_areas_geom_idx ON vector_areas USING GIST (geom);
CREATE INDEX IF NOT EXISTS vector_relations_geom_idx ON vector_relations USING GIST (geom);
-- Keep polygon POIs as points and preserve their complete tags as JSONB.
-- This also handles tagged buildings with a name, address or business use.
DROP TABLE IF EXISTS vector_label_points;
CREATE TABLE vector_label_points AS
 SELECT area_id AS osm_id, tags,
        ST_PointOnSurface(geom)::geometry(Point,3857) AS geom
 FROM vector_areas
 WHERE tags ?| ARRAY['amenity','shop','tourism','office','craft','place',
                     'addr:housenumber','name','public_transport','leisure']
   AND NOT ST_IsEmpty(geom);
CREATE INDEX vector_label_points_geom_idx ON vector_label_points USING GIST (geom);
ANALYZE vector_points;
ANALYZE vector_lines;
ANALYZE vector_areas;
ANALYZE vector_relations;
ANALYZE vector_label_points;