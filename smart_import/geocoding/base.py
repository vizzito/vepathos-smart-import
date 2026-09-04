"""Interfaz de geocoder. Permite sumar proveedores externos sin tocar el pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

STATUS_ALREADY = "already_geocoded"
STATUS_MATCHED = "matched"
STATUS_LOW = "low_confidence"
STATUS_NOT_FOUND = "not_found"
STATUS_ERROR = "error"
STATUS_CACHED = "cached"


@dataclass
class GeocodeResult:
    status: str
    lat: float | None = None
    lon: float | None = None
    confidence: float = 0.0
    precision: str | None = None
    source: str | None = None
    normalized_address: str | None = None
    matched_text: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def has_coords(self) -> bool:
        return self.lat is not None and self.lon is not None


class GeocoderProvider(Protocol):
    name: str

    def geocode(self, address: str, origin: tuple[float, float] | None = None,
                bbox: tuple[float, float, float, float] | None = None) -> GeocodeResult: ...
