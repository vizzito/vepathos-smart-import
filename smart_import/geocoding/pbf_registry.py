"""Descubre que .osm.pbf hay disponibles y cual cubre un punto/bbox.

Aprovecha que el cutter YA codifica el bbox en el nombre del extract:

    data/_extracts/<zona>/n60.02_s59.55_e10.95_w10.68-pyrosm.osm.pbf

Se lee solo el nombre del archivo: cero acoplamiento con el codigo del cutter y
cero necesidad de abrir PBFs de 300 MB para saber que contienen. Los PBF de pais
(argentina-pyrosm.osm.pbf) quedan como cobertura amplia de ultimo recurso.

Prioridad al resolver (depot / bbox):
  1. extract con bbox en el nombre que cubra el punto  → el MAS CHICO
  2. extract que cubra un bbox pedido                  → el MAS CHICO
  3. zone_hint por nombre de carpeta/archivo
  4. PBF de pais (sin bbox en el nombre) cuyo bbox
     en pbf_country_bounds.json cubre el punto
     → mayor margen interior (nunca el mas chico en disco)
  5. None  (nunca inventa ni llama afuera)

Los PBF viven en la raíz data/ del route-optimizer, separados por continente:

    data/south-america_tile_…/argentina-pyrosm.osm.pbf
    data/asia_tile_…/india/western-zone-pyrosm.osm.pbf
    data/north-america_tile_…/us_tile_…/florida-pyrosm.osm.pbf
    data/_extracts/…/n-34.48_s-34.73_e-58.30_w-58.58-pyrosm.osm.pbf

`scan()` hace rglob: el nombre de carpeta `*_tile_*` no importa. El JSON de
bounds dice QUÉ archivo abrir cuando no hay extract (slug + path_hints).
Calles = índice OSM de ese PBF, no vocab sqlite.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..resources import coverage_bounds_for

PBF_SUFFIX = "-pyrosm.osm.pbf"
_BBOX_RE = re.compile(
    r"n(?P<north>-?\d+(?:\.\d+)?)_s(?P<south>-?\d+(?:\.\d+)?)"
    r"_e(?P<east>-?\d+(?:\.\d+)?)_w(?P<west>-?\d+(?:\.\d+)?)"
)


def _coverage_for(entry: "PbfEntry") -> tuple[float, float, float, float] | None:
    return coverage_bounds_for(entry.country_slug, entry.path)


@dataclass(frozen=True)
class PbfEntry:
    path: Path
    zone: str
    north: float | None = None
    south: float | None = None
    east: float | None = None
    west: float | None = None
    size_bytes: int = 0

    @property
    def has_bbox(self) -> bool:
        return None not in (self.north, self.south, self.east, self.west)

    @property
    def key(self) -> str:
        """Identidad estable para nombrar el indice derivado."""
        return self.path.name.replace(PBF_SUFFIX, "").replace(".osm.pbf", "")

    @property
    def country_slug(self) -> str | None:
        """'argentina' desde 'argentina-pyrosm.osm.pbf'."""
        if self.has_bbox:
            return None
        name = self.path.name.lower()
        stem = name.replace(PBF_SUFFIX, "").replace(".osm.pbf", "")
        return stem or None

    @property
    def area(self) -> float:
        if not self.has_bbox:
            return float("inf")
        return abs(self.north - self.south) * abs(self.east - self.west)

    def covers(self, lat: float, lon: float, margin: float = 0.0) -> bool:
        if not self.has_bbox:
            return False
        return (self.south - margin <= lat <= self.north + margin
                and self.west - margin <= lon <= self.east + margin)

    def covers_bbox(self, north: float, south: float, east: float, west: float) -> bool:
        if not self.has_bbox:
            return False
        return (self.south <= south and self.north >= north
                and self.west <= west and self.east >= east)

    def country_covers(self, lat: float, lon: float) -> bool:
        slug = self.country_slug
        if not slug:
            return False
        bounds = _coverage_for(self)
        if not bounds:
            return False
        n, s, e, w = bounds
        return s <= lat <= n and w <= lon <= e

    def country_interior_margin(self, lat: float, lon: float) -> float:
        """Qué tan 'adentro' está el punto del bbox del país (min distancia al borde).

        Sirve para desempatar solapes (CABA cae en el bbox flojo de Uruguay y
        Argentina): preferimos el país donde el depot queda más lejos del borde.
        """
        slug = self.country_slug
        if not slug:
            return float("-inf")
        bounds = _coverage_for(self)
        if not bounds:
            return float("-inf")
        n, s, e, w = bounds
        if not (s <= lat <= n and w <= lon <= e):
            return float("-inf")
        return min(n - lat, lat - s, e - lon, lon - w)

    def as_dict(self) -> dict:
        return {
            "path": str(self.path), "zone": self.zone, "key": self.key,
            "size_mb": round(self.size_bytes / (1024 * 1024), 1),
            "kind": "extract" if self.has_bbox else "country",
            "bbox": ({"north": self.north, "south": self.south,
                      "east": self.east, "west": self.west} if self.has_bbox else None),
        }


def _parse(path: Path) -> PbfEntry:
    m = _BBOX_RE.search(path.name)
    zone = path.parent.name
    size = path.stat().st_size if path.exists() else 0
    if not m:
        return PbfEntry(path=path, zone=zone, size_bytes=size)
    return PbfEntry(
        path=path, zone=zone, size_bytes=size,
        north=float(m.group("north")), south=float(m.group("south")),
        east=float(m.group("east")), west=float(m.group("west")),
    )


class PbfRegistry:
    def __init__(self, entries: list[PbfEntry]):
        self.entries = entries

    @classmethod
    def scan(cls, root: str | Path) -> "PbfRegistry":
        base = Path(root).expanduser()
        if not base.exists():
            return cls([])
        found = [p for p in base.rglob("*.osm.pbf") if p.is_file() and ".locks" not in p.parts]
        return cls([_parse(p) for p in sorted(found)])

    def with_bbox(self) -> list[PbfEntry]:
        return [e for e in self.entries if e.has_bbox]

    def broad(self) -> list[PbfEntry]:
        """PBF sin bbox en el nombre (paises enteros): cobertura de ultimo recurso."""
        return [e for e in self.entries if not e.has_bbox]

    def find_for_point(self, lat: float, lon: float, margin: float = 0.0) -> PbfEntry | None:
        """El extract MAS CHICO que cubra el punto: menos parseo, mas precision."""
        covering = [e for e in self.with_bbox() if e.covers(lat, lon, margin)]
        if covering:
            return min(covering, key=lambda e: (e.area, e.size_bytes))
        return None

    def find_for_bbox(self, north: float, south: float, east: float,
                      west: float) -> PbfEntry | None:
        covering = [e for e in self.with_bbox() if e.covers_bbox(north, south, east, west)]
        if covering:
            return min(covering, key=lambda e: (e.area, e.size_bytes))
        return None

    def find_country_for_point(self, lat: float, lon: float) -> PbfEntry | None:
        """PBF de pais cuyo bbox aproximado cubre el punto.

        Si varios paises solapan (Rio de la Plata: AR vs UY), gana el de mayor
        margen interior — NO el mas chico en disco (Uruguay ~56 MB ganaba mal
        sobre Argentina ~400 MB y geocodificaba CABA contra calles uruguayas).
        """
        covering = [e for e in self.broad() if e.country_covers(lat, lon)]
        if not covering:
            return None
        return max(
            covering,
            key=lambda e: (e.country_interior_margin(lat, lon), -e.size_bytes),
        )

    def resolve(self, lat: float | None = None, lon: float | None = None,
                bbox: tuple[float, float, float, float] | None = None,
                zone_hint: str | None = None) -> PbfEntry | None:
        """Prioridad: extract bbox > punto > zone_hint > pais. Nunca adivina fuera."""
        if bbox:
            if found := self.find_for_bbox(*bbox):
                return found
            north, south, east, west = bbox
            lat = lat if lat is not None else (north + south) / 2
            lon = lon if lon is not None else (east + west) / 2
        if lat is not None and lon is not None:
            if found := self.find_for_point(lat, lon):
                return found
        if zone_hint:
            matches = [e for e in self.entries if zone_hint.lower() in e.zone.lower()
                       or zone_hint.lower() in e.path.name.lower()]
            if matches:
                return min(matches, key=lambda e: (0 if e.has_bbox else 1, e.size_bytes))
        if lat is not None and lon is not None:
            if found := self.find_country_for_point(lat, lon):
                return found
        return None
