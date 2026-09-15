"""Esquinas: 'Garibaldi y Montiel' → el punto donde se cruzan las dos calles.

En las planillas de reparto chicas la esquina ES la direccion: 24 de 91 filas
reales de Tandil (2026-09-15) venian asi ('lungui y navarro', 'MAIPU Y
MONTIEL', 'entre rios y dufau (mi sueño)'), y el geocoder las descartaba antes
de buscarlas porque no tienen altura.

El cruce se calcula con lo que tenga el indice:

  * polilineas de los highways (indices construidos desde 2026-09-14): cruce de
    segmentos, que es el punto exacto;
  * si el indice es anterior y no tiene `geom`, el par de nodos mas cercano entre
    las dos calles (direcciones + centroides de tramos). En Tandil los cruces
    reales dan 4-17 m; por encima de MAX_NODE_GAP_M no se afirma nada.

Una esquina nunca es verde: sale ambar con precision `intersection`, porque la
esquina no dice de que lado de la calle esta la puerta. Si dos pares de calles
homonimas se cruzan en lugares distintos del extract (dos pueblos con San
Martin y Moreno), gana el cruce mas cercano al depot y el detalle lo avisa.
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass

#: separadores de esquina. 'y'/'e' solo entre palabras: 'Rosales y Rivas' puede
#: ser el NOMBRE de una calle, y eso se decide contra el indice, no aca.
_CONNECTOR = re.compile(
    r"\s+(?:y|e|&|and|esq(?:uina)?\.?(?:\s+(?:con|de))?|c/|/|x|con)\s+", re.IGNORECASE)
_PARENS = re.compile(r"\(([^()]*)\)")
#: por encima de esto dos nodos no son la misma esquina
MAX_NODE_GAP_M = 40.0
#: dos cruces a mas de esto son esquinas distintas (homonimos en otro pueblo)
CLUSTER_M = 150.0
#: confianza fija: la esquina es un match estructural, no textual
INTERSECTION_CONFIDENCE = 0.75


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


def intersection_candidates(text: str) -> list[tuple[str, str]]:
    """Pares (calle A, calle B) que el texto podria nombrar, en orden de preferencia.

    'Ferrari (entre rios y dufau)' prueba primero lo de adentro del parentesis;
    'Montiel y 25 de mayo, Tandil' descarta la cola de localidad.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    pieces: list[str] = [m.group(1) for m in _PARENS.finditer(raw)]
    outside = _PARENS.sub(" ", raw)
    pieces.append(outside)
    out: list[tuple[str, str]] = []
    for piece in pieces:
        head = re.split(r"[,;]", piece)[0].strip()
        parts = _CONNECTOR.split(head, maxsplit=1)
        if len(parts) != 2:
            continue
        a, b = (p.strip(" .-") for p in parts)
        if len(a) >= 3 and len(b) >= 3 and (a, b) not in out:
            out.append((a, b))
    return out


@dataclass(frozen=True)
class Crossing:
    lat: float
    lon: float
    gap_m: float
    clusters: int
    method: str          # segments | nodes


def _points(conn: sqlite3.Connection, names: list[str]) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for name in names:
        pts.extend((float(a), float(b)) for a, b in conn.execute(
            "SELECT lat, lon FROM places WHERE street = ? OR (kind = 'highway' AND name = ?)",
            (name, name)))
    return pts


def _polylines(conn: sqlite3.Connection, names: list[str]) -> list[list[tuple[float, float]]]:
    from .interpolate import parse_polyline

    polys: list[list[tuple[float, float]]] = []
    try:
        for name in names:
            for (raw,) in conn.execute(
                    "SELECT geom FROM places WHERE kind = 'highway' AND name = ?"
                    " AND geom IS NOT NULL AND geom <> ''", (name,)):
                poly = parse_polyline(raw)
                if len(poly) >= 2:
                    polys.append(poly)
    except sqlite3.OperationalError:
        return []                           # indice sin columna geom
    return polys


def _segment_cross(p1, p2, p3, p4) -> tuple[float, float] | None:
    """Cruce de dos segmentos en lat/lon (plano local: alcanza a escala de cuadra)."""
    (y1, x1), (y2, x2), (y3, x3), (y4, x4) = p1, p2, p3, p4
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-18:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / den
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return (y1 + t * (y2 - y1), x1 + t * (x2 - x1))
    return None


def _cluster(hits: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """Agrupa cruces cercanos; de cada grupo queda el de menor separacion."""
    groups: list[list[tuple[float, float, float]]] = []
    for hit in sorted(hits, key=lambda h: h[2]):
        for group in groups:
            if haversine_m(hit[0], hit[1], group[0][0], group[0][1]) <= CLUSTER_M:
                group.append(hit)
                break
        else:
            groups.append([hit])
    return [g[0] for g in groups]


def find_crossing(conn: sqlite3.Connection, names_a: list[str], names_b: list[str],
                  origin: tuple[float, float] | None = None) -> Crossing | None:
    hits: list[tuple[float, float, float]] = []
    method = "segments"
    polys_a, polys_b = _polylines(conn, names_a), _polylines(conn, names_b)
    if polys_a and polys_b:
        for pa in polys_a:
            for pb in polys_b:
                for i in range(len(pa) - 1):
                    for j in range(len(pb) - 1):
                        pt = _segment_cross(pa[i], pa[i + 1], pb[j], pb[j + 1])
                        if pt is not None:
                            hits.append((pt[0], pt[1], 0.0))
    if not hits:
        method = "nodes"
        pts_a, pts_b = _points(conn, names_a), _points(conn, names_b)
        if not pts_a or not pts_b:
            return None
        cell = 0.0005                        # ~55 m: vecinos en las 9 celdas
        grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
        for lat, lon in pts_b:
            grid.setdefault((int(lat // cell), int(lon // cell)), []).append((lat, lon))
        for lat, lon in pts_a:
            ci, cj = int(lat // cell), int(lon // cell)
            best: tuple[float, float, float] | None = None
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for blat, blon in grid.get((ci + di, cj + dj), ()):
                        gap = haversine_m(lat, lon, blat, blon)
                        if gap <= MAX_NODE_GAP_M and (best is None or gap < best[2]):
                            best = ((lat + blat) / 2, (lon + blon) / 2, gap)
            if best is not None:
                hits.append(best)
    if not hits:
        return None
    clusters = _cluster(hits)
    if origin is not None:
        chosen = min(clusters, key=lambda h: haversine_m(origin[0], origin[1], h[0], h[1]))
    else:
        chosen = min(clusters, key=lambda h: h[2])
    return Crossing(lat=chosen[0], lon=chosen[1], gap_m=round(chosen[2], 1),
                    clusters=len(clusters), method=method)
