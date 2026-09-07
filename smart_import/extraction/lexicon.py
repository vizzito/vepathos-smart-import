"""Léxicos por locale para extracción heurística.

Fuente unica: `resources/labels.json` (via `smart_import.resources`).
Agregar un idioma o alias = editar ese JSON; no tocar este modulo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from ..resources import available_locales as resource_locales
from ..resources import fold, label_set, load_labels


@dataclass(frozen=True, slots=True)
class CompiledLexicon:
    """Union de packs activos, con lookups O(1) sobre labels fold-eados."""

    locales: tuple[str, ...]
    name_labels: frozenset[str]
    address_labels: frozenset[str]
    phone_labels: frozenset[str]
    deliver_prefixes: tuple[str, ...]          # longest-first, folded
    deliver_linkers: frozenset[str]
    phone_label_re: re.Pattern[str]
    deliver_prefix_re: re.Pattern[str]

    def classify_label(self, raw_label: str) -> str | None:
        key = fold(raw_label).strip(" .:.-_")
        if key in self.name_labels:
            return "customer_name"
        if key in self.address_labels:
            return "address"
        if key in self.phone_labels:
            return "phone"
        return None


def _alt_pattern(words: Iterable[str]) -> str:
    parts = sorted({fold(w) for w in words}, key=len, reverse=True)
    if not parts:
        return "(?!)"  # never matches
    return "(?:%s)" % "|".join(re.escape(p) for p in parts)


@lru_cache(maxsize=16)
def get_lexicon(locales: tuple[str, ...] | None = None) -> CompiledLexicon:
    """Compila desde labels.json. locales=None → todos los packs conocidos."""
    data = load_labels()
    all_codes = tuple(sorted(data["locales"]))
    wanted = set(locales) if locales else None
    codes = tuple(c for c in all_codes if wanted is None or c in wanted)
    if not codes:
        codes = all_codes

    name = label_set("name_labels", codes)
    address = label_set("address_labels", codes)
    phone = label_set("phone_labels", codes)
    linkers = label_set("deliver_linkers", codes)
    prefixes = tuple(sorted(label_set("deliver_prefixes", codes), key=len, reverse=True))

    phone_label_re = re.compile(
        rf"(?:^|[^\w]){_alt_pattern(phone)}\s*[:.]?\s*",
        re.IGNORECASE,
    )
    deliver_prefix_re = re.compile(
        rf"^\s*{_alt_pattern(prefixes)}\s+",
        re.IGNORECASE,
    )
    return CompiledLexicon(
        locales=codes,
        name_labels=name,
        address_labels=address,
        phone_labels=phone,
        deliver_prefixes=prefixes,
        deliver_linkers=linkers,
        phone_label_re=phone_label_re,
        deliver_prefix_re=deliver_prefix_re,
    )


def available_locales() -> tuple[str, ...]:
    return resource_locales()
