"""Independent integrity and equality checks for raster MBTiles files."""
from __future__ import annotations
import io
import sqlite3
from pathlib import Path

class ValidationError(ValueError):
    pass

def inspect(path: Path) -> dict:
    if not path.is_file():
        raise ValidationError(f'MBTiles missing: {path}')
    try:
        with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as db:
            check=db.execute('PRAGMA integrity_check').fetchone()[0]
            if check != 'ok':
                raise ValidationError(f'SQLite integrity failure: {check}')
            metadata=dict(db.execute('SELECT name,value FROM metadata'))
            count=db.execute('SELECT COUNT(*) FROM tiles').fetchone()[0]
            if metadata.get('format')!='png':
                raise ValidationError(f'Expected raster PNG MBTiles, found: {metadata.get("format")}')
            invalid=db.execute('SELECT COUNT(*) FROM tiles WHERE length(tile_data)<8 OR substr(tile_data,1,8)!=?',
                              (b'\x89PNG\r\n\x1a\n',)).fetchone()[0]
            if invalid:
                raise ValidationError(f'{invalid} tiles lack PNG signature')
            invalid_xyz=db.execute('SELECT COUNT(*) FROM tiles WHERE zoom_level<0 OR zoom_level>22 OR tile_column<0 OR tile_row<0 OR tile_column >= (1<<zoom_level) OR tile_row >= (1<<zoom_level)').fetchone()[0]
            if invalid_xyz:
                raise ValidationError(f'{invalid_xyz} tiles have invalid tile coordinates')
            return dict(tiles=count,bytes=path.stat().st_size,minzoom=metadata.get('minzoom'),
                        maxzoom=metadata.get('maxzoom'),bounds=metadata.get('bounds'))
    except sqlite3.Error as exc:
        raise ValidationError(f'Not a valid MBTiles SQLite database: {exc}') from exc

def compare(a: Path,b: Path, pixels: bool=False) -> dict:
    """Compare all tile coordinates and payloads; optional decoded pixel comparison."""
    inspect(a);inspect(b)
    if pixels:
        try:
            from PIL import Image
        except ImportError as exc:
            raise ValidationError('Pixel comparison requires Pillow: pip install Pillow') from exc
    # Ordered streaming merge: never load a multi-GB MBTiles table into RAM.
    def read_next(cursor):
        return next(cursor,None)
    with sqlite3.connect(f'file:{a}?mode=ro',uri=True) as da, sqlite3.connect(f'file:{b}?mode=ro',uri=True) as db:
        sql='SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles ORDER BY zoom_level,tile_column,tile_row'
        ca=iter(da.execute(sql)); cb=iter(db.execute(sql))
        left=read_next(ca);right=read_next(cb)
        missing_a=missing_b=different=shared=0
        while left is not None or right is not None:
            if left is None or (right is not None and right[:3]<left[:3]):
                missing_a += 1;right=read_next(cb)
            elif right is None or left[:3]<right[:3]:
                missing_b += 1;left=read_next(ca)
            else:
                shared += 1
                if left[3]!=right[3]:
                    equal_pixels=False
                    if pixels:
                        try:
                            with Image.open(io.BytesIO(left[3])) as la, Image.open(io.BytesIO(right[3])) as lb:
                                equal_pixels=(la.size==lb.size and
                                              la.convert('RGBA').tobytes()==lb.convert('RGBA').tobytes())
                        except (OSError,ValueError):
                            pass
                    if not equal_pixels:
                        different += 1
                left=read_next(ca);right=read_next(cb)
    return dict(equal=(missing_a==missing_b==different==0),missing_in_first=missing_a,
                missing_in_second=missing_b,different=different,shared=shared)
