"""Pure Web Mercator tile geometry and metatile scheduling."""
from __future__ import annotations
from dataclasses import dataclass
from math import asinh, atan, degrees, floor, log, pi, radians, sinh, tan
from typing import Iterator

MAX_LAT = 85.0511287798066
WEBMERCATOR_HALF = 20037508.342789244

@dataclass(frozen=True)
class Task:
    z: int
    x: int
    y: int
    width: int
    height: int
    x_min: int
    x_max: int
    y_min: int
    y_max: int

    def requested_tiles(self) -> int:
        return (self.x_max - self.x_min + 1) * (self.y_max - self.y_min + 1)

def validate_bbox(bbox):
    if len(bbox) != 4 or not all(isinstance(v, (int, float)) for v in bbox):
        raise ValueError("bbox must have four numeric values")
    w, s, e, n = bbox
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ValueError("bbox must satisfy -180 <= west < east <= 180, -90 <= south < north <= 90")
    return tuple(float(v) for v in bbox)

def lonlat_to_xyz(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 1 << z
    lat = min(MAX_LAT, max(-MAX_LAT, lat))
    xx = (lon + 180) / 360 * n
    yy = (1 - asinh(tan(radians(lat))) / pi) / 2 * n
    return min(n-1, max(0, floor(xx))), min(n-1, max(0, floor(yy)))

def xyz_range(bbox, z: int) -> tuple[int, int, int, int]:
    """Inclusive tile bounds, treating east/south edges as exclusive."""
    w, s, e, n = validate_bbox(bbox)
    if z < 0 or z > 22:
        raise ValueError('zoom must be between 0 and 22')
    # Neighboring exact grid edges should not add a tile beyond requested bbox.
    from math import nextafter
    x0, y0 = lonlat_to_xyz(w, n, z)
    x1, y1 = lonlat_to_xyz(nextafter(e, -float('inf')), nextafter(s, float('inf')), z)
    return min(x0,x1), max(x0,x1), min(y0,y1), max(y0,y1)

def tile_count(bbox, minzoom: int, maxzoom: int) -> int:
    return sum((xx1-xx0+1)*(yy1-yy0+1) for z in range(minzoom, maxzoom+1)
               for xx0,xx1,yy0,yy1 in [xyz_range(bbox,z)])

def metatiles(bbox, minzoom: int, maxzoom: int, size: int = 8) -> Iterator[Task]:
    if size < 1 or size > 32:
        raise ValueError('metatile size must be 1..32')
    for z in range(minzoom, maxzoom+1):
        xmin,xmax,ymin,ymax = xyz_range(bbox,z)
        for my in range((ymin // size)*size, ymax+1, size):
            for mx in range((xmin // size)*size, xmax+1, size):
                yield Task(z,mx,my,min(size,(1<<z)-mx),min(size,(1<<z)-my),
                           max(xmin,mx),min(xmax,mx+size-1),max(ymin,my),min(ymax,my+size-1))

def metatile_bounds(task: Task) -> tuple[float, float, float, float]:
    """Mapnik EPSG:3857 projected (west,south,east,north)."""
    n = 1 << task.z
    step = 2 * WEBMERCATOR_HALF / n
    left = -WEBMERCATOR_HALF + task.x*step
    right = left + task.width*step
    top = WEBMERCATOR_HALF - task.y*step
    bottom = top - task.height*step
    return left,bottom,right,top
