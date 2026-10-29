import struct
import zlib
from pathlib import Path
import pytest
from osm2mbtiles.pbf import read_bbox, PBFError

def varint(n):
    data=[]
    while n > 127:
        data.append((n&127)|128)
        n >>= 7
    return bytes(data+[n])

def pb(num, value):
    if isinstance(value, bytes):
        return varint((num<<3)|2)+varint(len(value))+value
    return varint(num<<3)+varint(value)

def zigzag(x):
    return (x<<1) ^ (x>>63)

def sample_pbf(path, bbox=(126.6,33.0,130.9,38.7), compressed=True):
    w,s,e,n=bbox
    bb=b''.join(pb(k,zigzag(round(v*1e9))) for k,v in [(1,w),(2,e),(3,n),(4,s)])
    raw=pb(1,bb)
    blob=(pb(2,len(raw))+pb(3,zlib.compress(raw))) if compressed else pb(1,raw)
    header=pb(1,b'OSMHeader')+pb(3,len(blob))
    path.write_bytes(struct.pack('>I',len(header))+header+blob)
    return path

@pytest.mark.parametrize('compressed',[True,False])
def test_read_bbox(tmp_path,compressed):
    p=sample_pbf(tmp_path/'korea.pbf',compressed=compressed)
    assert read_bbox(p)==pytest.approx((126.6,33.0,130.9,38.7))

def test_invalid_header(tmp_path):
    p=tmp_path/'bad.pbf'
    p.write_bytes(b'xxxxxxx')
    with pytest.raises(PBFError):
        read_bbox(p)

def test_missing_bbox(tmp_path):
    p=tmp_path/'no_bbox.pbf'
    raw=pb(4,b'OsmSchema-V0.6')
    blob=pb(1,raw)
    header=pb(1,b'OSMHeader')+pb(3,len(blob))
    p.write_bytes(struct.pack('>I',len(header))+header+blob)
    with pytest.raises(PBFError,match='--bbox'):
        read_bbox(p)
