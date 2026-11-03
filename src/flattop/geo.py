"""Great-circle helpers. Positions are (lat, lon) in decimal degrees; distances in nautical miles."""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict

EARTH_RADIUS_NM = 3440.065


class Pos(BaseModel):
    model_config = ConfigDict(frozen=True)

    lat: float
    lon: float


def distance_nm(a: Pos, b: Pos) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = p2 - p1
    dl = math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(a: Pos, b: Pos) -> float:
    """Initial true bearing from a to b, 0-360."""
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dl = math.radians(b.lon - a.lon)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360


def destination(a: Pos, bearing: float, dist_nm: float) -> Pos:
    d = dist_nm / EARTH_RADIUS_NM
    b = math.radians(bearing)
    p1, l1 = math.radians(a.lat), math.radians(a.lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return Pos(lat=math.degrees(p2), lon=(math.degrees(l2) + 540) % 360 - 180)


def in_sector(bearing: float, start: float, end: float) -> bool:
    """True if bearing lies in the clockwise sector start->end."""
    return (bearing - start) % 360 <= sector_width(start, end)


def sector_width(start: float, end: float) -> float:
    w = (end - start) % 360
    return 360.0 if w == 0 else w


def fmt_pos(p: Pos) -> str:
    ns = "S" if p.lat < 0 else "N"
    ew = "W" if p.lon < 0 else "E"
    return f"{abs(p.lat):.1f}°{ns} {abs(p.lon):.1f}°{ew}"
