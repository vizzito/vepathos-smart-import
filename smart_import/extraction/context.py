"""Contexto regional del job. Nada de Argentina hardcodeada.

Lo que el usuario sabe del archivo (region telefonica, pais, ciudad, depot) mejora
teléfonos, direcciones y codigos postales. Si no lo sabe, el parser funciona igual,
solo con menos ayuda.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractionContext:
    #: ISO 3166-1 alpha-2 para phonenumbers: AR, BR, US, IN...
    phone_region: str | None = None
    country: str | None = None
    country_code: str | None = None
    city: str | None = None
    state: str | None = None
    #: packs lexicos activos (es, en, pt). None = todos
    locales: tuple[str, ...] | None = None
    origin_lat: float | None = None
    origin_lon: float | None = None
    bbox: tuple[float, float, float, float] | None = None

    @property
    def regions(self) -> tuple[str, ...]:
        """Regiones a probar con phonenumbers, la del job primero."""
        ordered: list[str] = []
        for candidate in (self.phone_region, self.country_code):
            if candidate and candidate.upper() not in ordered:
                ordered.append(candidate.upper())
        return tuple(ordered)

    @classmethod
    def from_config(cls, config, **overrides) -> "ExtractionContext":
        base = {
            "phone_region": getattr(config, "default_phone_region", None) or None,
            "locales": None,
        }
        base.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**base)

    def as_dict(self) -> dict:
        return {
            "phone_region": self.phone_region, "country": self.country,
            "country_code": self.country_code, "city": self.city, "state": self.state,
            "locales": list(self.locales) if self.locales else None,
        }
