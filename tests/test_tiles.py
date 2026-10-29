import pytest
from osm2mbtiles.tiles import lonlat_to_xyz,xyz_range,tile_count,metatiles,metatile_bounds,WEBMERCATOR_HALF

def test_world_bounds():
    assert lonlat_to_xyz(-180,85,0)==(0,0)
    assert lonlat_to_xyz(180,-85,0)==(0,0)
    assert xyz_range((-180,-85,180,85),0)==(0,0,0,0)
    assert tile_count((-180,-85,180,85),2,2)==16

def test_meridian_crossing():
    assert xyz_range((-.1,-.1,.1,.1),1)==(0,1,0,1)
    assert tile_count((-.1,-.1,.1,.1),0,2)==9

def test_metatile_coverage_exact():
    bbox=(126.6,33,130.9,38.7)
    for z in (0,1,3,8,12):
        xs,xend,ys,yend=xyz_range(bbox,z)
        tiles=set()
        for t in metatiles(bbox,z,z,8):
            assert t.x%8==0 and t.y%8==0
            assert 1<=t.width<=8 and 1<=t.height<=8
            for y in range(t.y_min,t.y_max+1):
                for x in range(t.x_min,t.x_max+1):
                    assert (x,y) not in tiles
                    tiles.add((x,y))
        assert tiles=={(x,y) for y in range(ys,yend+1) for x in range(xs,xend+1)}

def test_metatile_projection():
    t=next(metatiles((-180,-85,180,85),0,0))
    assert metatile_bounds(t)==pytest.approx((-WEBMERCATOR_HALF,-WEBMERCATOR_HALF,WEBMERCATOR_HALF,WEBMERCATOR_HALF))

def test_invalid():
    with pytest.raises(ValueError):
        xyz_range((10,0,5,9),5)
