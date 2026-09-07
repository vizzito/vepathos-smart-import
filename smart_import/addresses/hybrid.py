"""Combina parsers: gana el que aporta mas componentes precisos, campo por campo.

No se asume que libpostal siempre gane. El hibrido arranca con el resultado del
parser primario y solo completa lo que falta con el secundario: asi una mejora del
backend suma sin poder pisar lo que las reglas ya resolvieron bien.
"""
from __future__ import annotations

from .base import COMPONENTS, AddressParser, ParsedAddress


class HybridAddressParser(AddressParser):
    """Primario + secundarios, en orden de preferencia."""

    name = "hybrid"

    def __init__(self, primary: AddressParser, *secondary: AddressParser):
        self.primary = primary
        self.secondary = tuple(p for p in secondary if p is not None)

    def available(self) -> bool:
        return self.primary.available()

    def parse(self, text: str, context=None) -> ParsedAddress:
        result = self.primary.parse(text, context)
        components = dict(result.components)
        evidence = list(result.evidence)

        for parser in self.secondary:
            if not parser.available():
                continue
            other = parser.parse(text, context)
            for name in COMPONENTS:
                value = other.components.get(name)
                if value and not components.get(name):
                    components[name] = value
                    evidence.append(f"{parser.name} aporto {name} '{value}'")

        return ParsedAddress(text=result.text, components=components,
                             parser=self.name, evidence=tuple(evidence))

    def describe(self) -> dict:
        return {"name": self.name,
                "available": self.available(),
                "parsers": [p.describe() for p in (self.primary, *self.secondary)]}
