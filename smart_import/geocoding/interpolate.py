"""Interpolar una altura sobre la calle cuando OSM no tiene el número exacto.

No inventa numeración desde los extremos del way: hace falta un ancla de cada
lado (u.u. misma paridad, vereda par/impar). Si hay polilínea del highway, el
punto camina el way; si no, interpola en línea recta entre las dos casas.
"""
from __future__ import annotations

import math
from typing import NamedTuple

#: No interpolar de 100 a 4000: es otra zona de la avenida.
MAX_SPAN = 400
#: Si la casa queda a más de esto del way, no usamos la geometría (otro tramo).
MAX_PROJECT_M = 80.0


class HousePoint(NamedTuple):
    number: int
    lat: float
    lon: float


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, x)))


def encode_polyline(pts: list[tuple[float, float]], *, max_pts: int = 128) -> str | None:
    if len(pts) < 2:
        return None
    if len(pts) > max_pts:
        last = pts[-1]
        step = (len(pts) - 1) / (max_pts - 1)
        pts = [pts[int(round(i * step))] for i in range(max_pts - 1)] + [last]
    return ";".join(f"{lat:.6f},{lon:.6f}" for lat, lon in pts)


def parse_polyline(raw: str | None) -> list[tuple[float, float]]:
    if not raw:
        return []
    out: list[tuple[float, float]] = []
    for part in raw.split(";"):
        if "," not in part:
            continue
        a, b = part.split(",", 1)
        try:
            out.append((float(a), float(b)))
        except ValueError:
            continue
    return out


def _cum_m(poly: list[tuple[float, float]]) -> list[float]:
    acc = [0.0]
    for i in range(1, len(poly)):
        acc.append(acc[-1] + haversine_m(*poly[i - 1], *poly[i]))
    return acc


def _point_on_segment(
    lat1: float, lon1: float, lat2: float, lon2: float, lat: float, lon: float,
) -> tuple[float, float, float]:
    """Proyección en el segmento. Devuelve (t 0-1, lat, lon) del pie."""
    # Equirectangular local: suficiente para un tramo de calle.
    lat_m = 111_320.0
    lon_m = 111_320.0 * max(0.2, math.cos(math.radians((lat1 + lat2) / 2)))
    vx = (lon2 - lon1) * lon_m
    vy = (lat2 - lat1) * lat_m
    wx = (lon - lon1) * lon_m
    wy = (lat - lat1) * lat_m
    mag = vx * vx + vy * vy
    if mag <= 1e-6:
        return 0.0, lat1, lon1
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / mag))
    return t, lerp(lat1, lat2, t), lerp(lon1, lon2, t)


def project_onto_polyline(
    poly: list[tuple[float, float]], lat: float, lon: float,
) -> tuple[float, float] | None:
    """(metros desde el inicio, residual metros) o None."""
    if len(poly) < 2:
        return None
    cum = _cum_m(poly)
    best_along = 0.0
    best_res = float("inf")
    for i in range(1, len(poly)):
        t, plat, plon = _point_on_segment(*poly[i - 1], *poly[i], lat, lon)
        res = haversine_m(lat, lon, plat, plon)
        if res < best_res:
            best_res = res
            best_along = cum[i - 1] + t * (cum[i] - cum[i - 1])
    return best_along, best_res


def point_at_distance(
    poly: list[tuple[float, float]], dist_m: float,
) -> tuple[float, float] | None:
    if len(poly) < 2:
        return None
    cum = _cum_m(poly)
    total = cum[-1]
    if total <= 0:
        return poly[0]
    dist_m = max(0.0, min(total, dist_m))
    for i in range(1, len(poly)):
        if dist_m <= cum[i] + 1e-6:
            span = cum[i] - cum[i - 1]
            t = 0.0 if span <= 1e-6 else (dist_m - cum[i - 1]) / span
            la1, lo1 = poly[i - 1]
            la2, lo2 = poly[i]
            return lerp(la1, la2, t), lerp(lo1, lo2, t)
    return poly[-1]


def pick_brackets(
    want: int, houses: list[HousePoint], *, same_parity: bool,
) -> tuple[HousePoint, HousePoint] | None:
    pool = [
        h for h in houses
        if not same_parity or (h.number % 2 == want % 2)
    ]
    lo = [h for h in pool if h.number <= want]
    hi = [h for h in pool if h.number >= want]
    if not lo or not hi:
        return None
    left = max(lo, key=lambda h: h.number)
    right = min(hi, key=lambda h: h.number)
    if left.number == right.number:
        return None
    if right.number - left.number > MAX_SPAN:
        return None
    return left, right


def interpolate_house(
    want: int,
    houses: list[HousePoint],
    polyline: list[tuple[float, float]] | None = None,
) -> tuple[float, float] | None:
    """Punto interpolado o None (sin anclas, span enorme, want fuera)."""
    if want is None or not houses:
        return None
    pair = pick_brackets(want, houses, same_parity=True)
    if pair is None:
        pair = pick_brackets(want, houses, same_parity=False)
    if pair is None:
        return None
    left, right = pair
    span = right.number - left.number
    t = (want - left.number) / span
    poly = polyline if polyline and len(polyline) >= 2 else None
    if poly is not None:
        p0 = project_onto_polyline(poly, left.lat, left.lon)
        p1 = project_onto_polyline(poly, right.lat, right.lon)
        if (
            p0 is not None and p1 is not None
            and p0[1] <= MAX_PROJECT_M and p1[1] <= MAX_PROJECT_M
        ):
            along = lerp(p0[0], p1[0], t)
            pt = point_at_distance(poly, along)
            if pt is not None:
                return pt
    return lerp(left.lat, right.lat, t), lerp(left.lon, right.lon, t)
