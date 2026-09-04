"""Descubre que .osm.pbf hay disponibles y cual cubre un punto/bbox.

Aprovecha que el cutter YA codifica el bbox en el nombre del extract:

    data/_extracts/<zona>/n60.02_s59.55_e10.95_w10.68-pyrosm.osm.pbf

Se lee solo el nombre del archivo: cero acoplamiento con el codigo del cutter y
cero necesidad de abrir PBFs de 300 MB para saber que contienen. Los PBF de pais
(argentina-pyrosm.osm.pbf) quedan como cobertura amplia de ultimo recurso.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PBF_SUFFIX = "-pyrosm.osm.pbf"
_BBOX_RE = re.compile(
    r"n(?P<north>-?\d+(?:\.\d+)?)_s(?P<south>-?\d+(?:\.\d+)?)"
    r"_e(?P<east>-?\d+(?:\.\d+)?)_w(?P<west>-?\d+(?:\.\d+)?)"
)


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

    def as_dict(self) -> dict:
        return {
            "path": str(self.path), "zone": self.zone, "key": self.key,
            "size_mb": round(self.size_bytes / (1024 * 1024), 1),
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

    def resolve(self, lat: float | None = None, lon: float | None = None,
                bbox: tuple[float, float, float, float] | None = None,
                zone_hint: str | None = None) -> PbfEntry | None:
        """bbox > punto > zona por nombre. Nunca adivina fuera de eso."""
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
                return min(matches, key=lambda e: e.size_bytes)
        return None
