"""Read the bbox from an OSM PBF HeaderBlock without loading the full extract."""
from __future__ import annotations
import struct
import zlib
from pathlib import Path

class PBFError(ValueError):
    pass

def _varint(data: bytes, start: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if start >= len(data):
            raise PBFError("truncated protobuf varint")
        b = data[start]
        start += 1
        value |= (b & 0x7f) << shift
        if b & 0x80 == 0:
            return value, start
    raise PBFError("protobuf varint too long")

def _fields(data: bytes):
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise PBFError("invalid field number")
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 2:
            n, pos = _varint(data, pos)
            if n > len(data) - pos:
                raise PBFError("truncated length-delimited field")
            value = data[pos:pos+n]
            pos += n
        elif wire == 1:
            if len(data) - pos < 8:
                raise PBFError("truncated fixed64")
            value = data[pos:pos+8]
            pos += 8
        elif wire == 5:
            if len(data) - pos < 4:
                raise PBFError("truncated fixed32")
            value = data[pos:pos+4]
            pos += 4
        else:
            raise PBFError(f"unsupported protobuf wire type {wire}")
        yield field, value

def _zigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)

def read_bbox(path: str | Path) -> tuple[float, float, float, float]:
    """Return (west, south, east, north); error if extract has no header bbox."""
    with Path(path).open('rb') as stream:
        prefix = stream.read(4)
        if len(prefix) != 4:
            raise PBFError("not a valid OSM PBF: missing BlobHeader size")
        header_len = struct.unpack('>I', prefix)[0]
        if not 0 < header_len <= 65536:
            raise PBFError("invalid OSM PBF BlobHeader size")
        raw_header = stream.read(header_len)
        if len(raw_header) != header_len:
            raise PBFError("truncated OSM PBF BlobHeader")
        header = dict(_fields(raw_header))
        if header.get(1) != b'OSMHeader' or not isinstance(header.get(3), int):
            raise PBFError("first PBF blob is not OSMHeader")
        blob_len = header[3]
        if not 0 < blob_len <= 64 * 1024 * 1024:
            raise PBFError("OSMHeader blob is too large")
        blob_data = stream.read(blob_len)
        if len(blob_data) != blob_len:
            raise PBFError("truncated OSMHeader blob")
    blob = dict(_fields(blob_data))
    if isinstance(blob.get(1), bytes):
        header_block = blob[1]
    elif isinstance(blob.get(3), bytes):
        raw_size = blob.get(2)
        if not isinstance(raw_size, int) or raw_size > 64 * 1024 * 1024:
            raise PBFError("missing or excessive uncompressed header size")
        decoder = zlib.decompressobj()
        header_block = decoder.decompress(blob[3], raw_size + 1)
        if len(header_block) != raw_size or not decoder.eof or decoder.unconsumed_tail:
            raise PBFError("invalid OSMHeader compressed data")
    else:
        raise PBFError("unsupported OSMHeader compression")
    header_fields = dict(_fields(header_block))
    bbox = header_fields.get(1)
    if not isinstance(bbox, bytes):
        raise PBFError("PBF has no header bounding box; specify --bbox west,south,east,north")
    values = dict(_fields(bbox))
    if not all(isinstance(values.get(n), int) for n in (1, 2, 3, 4)):
        raise PBFError("PBF bounding box is incomplete")
    west = _zigzag(values[1]) * 1e-9
    east = _zigzag(values[2]) * 1e-9
    north = _zigzag(values[3]) * 1e-9
    south = _zigzag(values[4]) * 1e-9
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise PBFError("PBF contains an invalid bounding box")
    return west, south, east, north
