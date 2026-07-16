"""Point conversion helpers — the only geometry the listings API needs.

A dependency like shapely would be overkill for one point type: EWKB for a 2D
point is 21-25 bytes with a fixed layout, and the WKT we write ourselves.
Geocoding from the address arrives with the Celery part (§8.1).
"""

import struct

from geoalchemy2 import WKBElement, WKTElement

_EWKB_SRID_FLAG = 0x20000000


def to_point(lat: float, lng: float) -> WKTElement:
    return WKTElement(f"POINT({lng} {lat})", srid=4326)


def point_lonlat(element: WKBElement | WKTElement | None) -> tuple[float, float] | None:
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
