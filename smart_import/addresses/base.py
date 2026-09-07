"""Contrato de parsing de direcciones. El resto del sistema no sabe quien parsea.

Componentes internacionales a proposito: NO se asume `street + house_number`.
Una direccion de Mumbai es `unit + suburb + landmark + city + postcode` y tiene
que caber en la misma estructura que `Av. Corrientes 100, CABA`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

#: componentes reconocidos. El orden es el de lectura tipica de una direccion.
COMPONENTS = (
    "house_number", "road", "unit", "level", "building",
    "suburb", "neighbourhood", "city", "state", "postcode", "country", "landmark",
)

#: componentes que hacen a una direccion *precisa* (no solo a una localidad)
PRECISE_COMPONENTS = ("house_number", "road", "unit", "building", "postcode")


@dataclass(frozen=True)
class ParsedAddress:
    """Lo que un parser entendio. `text` siempre es el original, sin tocar."""
    text: str
    components: dict[str, str] = field(default_factory=dict)
    parser: str = ""
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def normalized(self) -> str:
        """Los componentes en orden de lectura. Vacio si no se entendio nada."""
        parts = [self.components[c] for c in COMPONENTS if self.components.get(c)]
        return ", ".join(parts)

    @property
    def is_precise(self) -> bool:
        return any(self.components.get(c) for c in PRECISE_COMPONENTS)

    def get(self, name: str) -> str:
        return self.components.get(name, "")

    def as_dict(self) -> dict:
        return {"text": self.text, "components": dict(self.components),
                "parser": self.parser, "normalized": self.normalized,
                "evidence": list(self.evidence)}


class AddressParser(ABC):
    """Implementaciones: heuristic, libpostal, hybrid."""

    name: str = "base"

    @abstractmethod
    def parse(self, text: str, context=None) -> ParsedAddress:
        """Descompone `text`. Nunca inventa componentes que no esten en el texto."""

    def available(self) -> bool:
        """False cuando faltan dependencias/datos; el sistema arranca igual."""
        return True

    def describe(self) -> dict:
        return {"name": self.name, "available": self.available()}
