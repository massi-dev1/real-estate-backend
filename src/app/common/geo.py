"""Geometry conversion helpers shared across modules (§5 ``app/common``).

A dependency like shapely would be overkill for these shapes: EWKB for a 2D
point is 21-25 bytes with a fixed layout, and the WKT we write ourselves.
Grown from the listings-only point helpers when agents (§8.5) needed
MultiPolygon service areas too.
"""

import struct

from geoalchemy2 import WKBElement, WKTElement

_EWKB_SRID_FLAG = 0x20000000

LonLat = tuple[float, float]


def to_point(lat: float, lng: float) -> WKTElement:
    return WKTElement(f"POINT({lng} {lat})", srid=4326)


def point_lonlat(element: WKBElement | WKTElement | None) -> LonLat | None:
    """Extract (lon, lat) from a stored WKB point or a just-assigned WKT one."""
    if element is None:
        return None
    if isinstance(element, WKTElement):
        wkt = str(element.data)
        inner = wkt[wkt.index("(") + 1 : wkt.index(")")]
        lng_s, lat_s = inner.split()
        return float(lng_s), float(lat_s)
    data = element.data
    raw = bytes.fromhex(data) if isinstance(data, str) else bytes(data)
    order = "<" if raw[0] == 1 else ">"
    (geom_type,) = struct.unpack_from(f"{order}I", raw, 1)
    offset = 9 if geom_type & _EWKB_SRID_FLAG else 5
    lng, lat = struct.unpack_from(f"{order}dd", raw, offset)
    return lng, lat


def to_multipolygon(polygons: list[list[LonLat]]) -> WKTElement:
    """MultiPolygon WKT from closed outer rings of validated (lon, lat) floats.

    Callers (schema validators) guarantee each ring is closed and range-checked;
    the WKT is built only from parsed floats — never from raw client text.
    """
    parts = []
    for ring in polygons:
        coords = ", ".join(f"{lon} {lat}" for lon, lat in ring)
        parts.append(f"(({coords}))")
    return WKTElement(f"MULTIPOLYGON({', '.join(parts)})", srid=4326)


def multipolygon_rings(element: WKBElement | WKTElement | None) -> list[list[LonLat]] | None:
    """Outer rings of a stored WKB MultiPolygon as (lon, lat) lists.

    Inner rings (holes) are not produced by our writer and are skipped on read.
    """
    if element is None:
        return None
    if isinstance(element, WKTElement):
        return _rings_from_wkt(str(element.data))
    data = element.data
    raw = bytes.fromhex(data) if isinstance(data, str) else bytes(data)
    return _rings_from_ewkb(raw)


def _rings_from_wkt(wkt: str) -> list[list[LonLat]]:
    inner = wkt[wkt.index("(") + 1 : wkt.rindex(")")]
    rings: list[list[LonLat]] = []
    # Our writer emits one outer ring per polygon: "((x y, ...)), ((x y, ...))".
    for chunk in inner.split(")),"):
        coords = chunk.strip().lstrip("(").rstrip(")").strip()
        ring = []
        for pair in coords.split(","):
            lon_s, lat_s = pair.split()
            ring.append((float(lon_s), float(lat_s)))
        rings.append(ring)
    return rings


def _rings_from_ewkb(raw: bytes) -> list[list[LonLat]]:
    order = "<" if raw[0] == 1 else ">"
    (geom_type,) = struct.unpack_from(f"{order}I", raw, 1)
    offset = 9 if geom_type & _EWKB_SRID_FLAG else 5
    (num_polygons,) = struct.unpack_from(f"{order}I", raw, offset)
    offset += 4
    rings: list[list[LonLat]] = []
    for _ in range(num_polygons):
        # Each polygon: byte order (1) + geom type (4) + ring count (4).
        p_order = "<" if raw[offset] == 1 else ">"
        (num_rings,) = struct.unpack_from(f"{p_order}I", raw, offset + 5)
        offset += 9
        for ring_index in range(num_rings):
            (num_points,) = struct.unpack_from(f"{p_order}I", raw, offset)
            offset += 4
            points = struct.unpack_from(f"{p_order}{num_points * 2}d", raw, offset)
            offset += num_points * 16
            if ring_index == 0:  # outer ring only; holes are never written by us
                rings.append([(points[i], points[i + 1]) for i in range(0, len(points), 2)])
    return rings
