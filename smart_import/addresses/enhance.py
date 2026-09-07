"""Heuristico + enhancer opcional (libpostal). No reemplaza: solo mejora.

Flujo:
  1. siempre corre el parser primario (heuristico)
  2. si el gate dice que hace falta Y el enhancer esta disponible, lo consulta
  3. completa huecos con valores *plausibles*; si la road del primario era
     sospechosa, permite pisar road/house_number

libpostal a veces inventa basura ('house_number=shanti', 'road=nagar near …').
Por eso no se copia todo ciegamente: ver `_plausible_*`.

Con `SMART_IMPORT_LIBPOSTAL_ENABLED=false` el factory ni construye esta clase.
"""
from __future__ import annotations

import re
from collections import Counter

from ..logging_setup import get_logger
from ..resources import building_tokens, fold, unit_prefixes
from .base import COMPONENTS, AddressParser, ParsedAddress
from .gate import enhancement_reason, needs_enhancement, road_is_suspicious

#: campos que cambian el geocode; city/country no cuentan como "ayudo"
_MATERIAL = frozenset({"road", "house_number"})
_EXAMPLE_CAP = 5

#: localidad / meta de bajo riesgo (building NO: libpostal mete nombres de persona)
_SAFE_FILL = frozenset({
    "city", "suburb", "neighbourhood", "postcode", "landmark",
    "level", "unit",
})
#: solo se pisan si el primario tenia road sospechosa
_OVERRIDABLE_WHEN_SUSPICIOUS = ("road", "house_number")

_HAS_DIGIT = re.compile(r"\d")
_ALPHA_ONLY = re.compile(r"^[^\W\d_]+$", re.UNICODE)

_log = get_logger("addresses")
_PREVIEW = 72


def _preview(text: str) -> str:
    raw = (text or "").replace("\n", " ").strip()
    if len(raw) <= _PREVIEW:
        return raw
    return raw[:_PREVIEW - 1] + "…"


def _plausible_house_number(value: str) -> bool:
    """Rechaza 'shanti' y 'flat 14b'; acepta '42', '14B', '2-1-1', 'plot no. 42'."""
    v = (value or "").strip()
    if not v or not _HAS_DIGIT.search(v):
        return False
    if _ALPHA_ONLY.fullmatch(v):
        return False
    folded = fold(v)
    for prefix in unit_prefixes():
        p = fold(prefix)
        if folded == p or folded.startswith(p + " ") or folded.startswith(p + "."):
            return False
    return True


def _plausible_building(value: str) -> bool:
    """Rechaza nombres de persona ('rahul sharma'); acepta 'Cyber Towers'.

    Las dos son dos palabras alfabeticas sin digitos, asi que la forma no alcanza:
    hace falta un token de tipo de edificio ('towers', 'plaza', 'bhavan'), que vive
    en `resources/geo_keywords.json`.
    """
    v = (value or "").strip()
    if not v:
        return False
    words = [w.strip(".,;:") for w in v.split() if w.strip(".,;:")]
    if not words:
        return False
    if any(fold(w) in building_tokens() for w in words):
        return True
    # Sin token de edificio: dos+ palabras solo-letras sin digito = nombre propio.
    if (len(words) >= 2 and not _HAS_DIGIT.search(v)
            and all(_ALPHA_ONLY.fullmatch(w) for w in words)):
        return False
    return True


def _plausible_road(value: str, locales: tuple[str, ...] | None) -> bool:
    """Rechaza basura tipica de libpostal en India ('nagar near hanuman temple')."""
    v = (value or "").strip()
    if not v or road_is_suspicious(v, locales):
        return False
    folded = fold(v)
    if " near " in f" {folded} ":
        return False
    return True


class EnhancingAddressParser(AddressParser):
    """Calidad extra: primario + enhancer bajo demanda."""

    name = "enhanced"

    def __init__(self, primary: AddressParser, enhancer: AddressParser,
                 locales: tuple[str, ...] | None = None):
        self.primary = primary
        self.enhancer = enhancer
        self.locales = locales
        self.enhancer_calls = 0
        self.enhancer_skips = 0
        self.fields_added = 0
        self.fields_rejected = 0
        self.helped = 0
        self.noop = 0
        self.by_reason: Counter[str] = Counter()
        self.helped_examples: list[str] = []
        #: skip | helped | noop | off — ultima llamada, para geocode-accuracy
        self.last_outcome = "off"
        self.last_reason: str | None = None

    def available(self) -> bool:
        return self.primary.available()

    def parse(self, text: str, context=None) -> ParsedAddress:
        result = self.primary.parse(text, context)
        reason = enhancement_reason(result, self.locales)

        if reason is None or not self.enhancer.available():
            self.enhancer_skips += 1
            self.last_outcome = "skip"
            self.last_reason = reason or "gate_closed"
            # Solo en verbose: el camino caliente es "heuristico alcanzo".
            _log.debug(
                "libpostal skip  reason=%s  road=%r  text=%r",
                self.last_reason,
                result.get("road") or None,
                _preview(text),
                extra={"stage": "ADDRESS"},
            )
            return result

        self.enhancer_calls += 1
        self.by_reason[reason] += 1
        self.last_reason = reason
        other = self.enhancer.parse(text, context)
        components = dict(result.components)
        evidence = list(result.evidence)
        evidence.append(f"enhancer consultado ({reason})")

        added: list[str] = []
        rejected: list[str] = []
        material: list[str] = []
        suspicious = road_is_suspicious(components.get("road", ""), self.locales)
        for name in COMPONENTS:
            value = other.components.get(name)
            if not value:
                continue
            if not self._accept(name, value, filling=not components.get(name),
                                suspicious=suspicious):
                evidence.append(f"{self.enhancer.name} rechazo {name} '{value}'")
                rejected.append(f"{name}={value!r}")
                self.fields_rejected += 1
                continue
            current = components.get(name)
            if not current:
                components[name] = value
                evidence.append(f"{self.enhancer.name} aporto {name} '{value}'")
                added.append(f"{name}={value!r}")
                self.fields_added += 1
                if name in _MATERIAL:
                    material.append(f"{name}={value!r}")
            elif suspicious and name in _OVERRIDABLE_WHEN_SUSPICIOUS:
                components[name] = value
                evidence.append(
                    f"{self.enhancer.name} corrigio {name} '{current}' -> '{value}'")
                added.append(f"{name}:{current!r}->{value!r}")
                self.fields_added += 1
                if name in _MATERIAL:
                    material.append(f"{name}:{current!r}->{value!r}")

        if material:
            self.helped += 1
            self.last_outcome = "helped"
            if len(self.helped_examples) < _EXAMPLE_CAP:
                self.helped_examples.append(
                    f"{_preview(text)} → {', '.join(material)}")
            # Por fila solo en verbose: 1800 calles CABA no son un log util.
            _log.debug(
                "libpostal helped  why=%s  added=%s  text=%r",
                reason, ", ".join(material), _preview(text),
                extra={"stage": "ADDRESS"},
            )
        else:
            self.noop += 1
            self.last_outcome = "noop"
            _log.debug(
                "libpostal noop  reason=%s  added=%s  rejected=%s  text=%r",
                reason,
                ",".join(added) or "—",
                ",".join(rejected) or "—",
                _preview(text),
                extra={"stage": "ADDRESS"},
            )
        return ParsedAddress(text=result.text, components=components,
                             parser=self.name, evidence=tuple(evidence))

    def _accept(self, name: str, value: str, *, filling: bool,
                suspicious: bool) -> bool:
        if name in _SAFE_FILL:
            return True
        if name == "building":
            return _plausible_building(value)
        if name == "house_number":
            return _plausible_house_number(value)
        if name == "road":
            return _plausible_road(value, self.locales)
        return filling

    def stats(self) -> dict:
        total = self.enhancer_calls + self.enhancer_skips
        return {
            "mode": "on-demand",
            "enhancer_calls": self.enhancer_calls,
            "enhancer_skips": self.enhancer_skips,
            "fields_added": self.fields_added,
            "fields_rejected": self.fields_rejected,
            "helped": self.helped,
            "noop": self.noop,
            "skip_pct": round(100.0 * self.enhancer_skips / total, 1) if total else 0.0,
            "by_reason": dict(self.by_reason),
            "helped_examples": list(self.helped_examples),
        }

    def describe(self) -> dict:
        return {
            "name": self.name,
            "available": self.available(),
            "enhancer": self.enhancer.describe(),
            "primary": self.primary.describe(),
            **self.stats(),
        }


__all__ = [
    "EnhancingAddressParser",
    "needs_enhancement",
    "enhancement_reason",
    "road_is_suspicious",
]
