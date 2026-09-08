"""Armar la query de geocode SIN mutar el address visible del usuario.

Reglas:
  * La UI / CSV siguen mostrando exactamente lo que cargo el cliente.
  * La query interna = address + localidad de la fila + tokens del depot.
  * `enhance=True` (opt-in): reescribe la query con road+house del parser
    (capa enhance/heuristico). No pisa el address persistido.
  * Reintento limpio: saca piso/depto de la query si el primer match es flojo.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from ..normalization.address import already_present, dedupe_address_segments
from .base import (
    STATUS_LOW, STATUS_MATCHED, STATUS_NOT_FOUND, GeocodeResult,
)
from .depot_context import DepotContext, normalize_locality_label

#: columnas tipicas de export ecommerce / schema que aportan localidad
_LOCALITY_KEYS = (
    "zone", "locality", "neighborhood", "neighbourhood", "barrio",
    "city", "shipping_city",
    "region", "state", "province",
    "shipping_province", "shipping_province_name",
    "postcode", "zip", "shipping_zip", "postal_code", "zipcode",
    "country", "shipping_country",
)

#: piso / depto / unit — ruido para FTS; el barrio NO se toca
_UNIT_CLAUSE = re.compile(
    r",?\s*\b(?:piso|p\.?\s*\d+|depto\.?|dept\.?|departamento|apt\.?|apto\.?|"
    r"apartment|unit|ph|oficina|of\.?|floor|suite|room)\b[^,]*(?=,|$)",
    re.IGNORECASE,
)


def locality_tokens_from_row(row: Mapping[str, Any] | None) -> list[str]:
    """Localidad estructurada de la fila (City/Province/…), sin duplicar."""
    if not row:
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    for key in _LOCALITY_KEYS:
        raw = row.get(key)
        if raw is None or not str(raw).strip():
            continue
        # postcodes: no pasar por normalize_locality_label (descarta CP sueltos)
        if key in ("postcode", "zip", "shipping_zip", "postal_code", "zipcode"):
            label = str(raw).strip()
        else:
            label = normalize_locality_label(str(raw))
        if not label:
            continue
        fold = label.casefold()
        if fold in seen:
            continue
        seen.add(fold)
        tokens.append(label)
    return tokens


def append_missing(base: str, tokens: list[str]) -> str:
    raw = dedupe_address_segments((base or "").strip())
    if not raw:
        return raw
    extras = [t for t in tokens if t and not already_present(raw, t)]
    if not extras:
        return raw
    return dedupe_address_segments(f"{raw}, {', '.join(extras)}")


def cleaned_query(query: str) -> str | None:
    """Saca cláusulas de piso/depto. None si no cambia nada util."""
    raw = dedupe_address_segments((query or "").strip())
    if not raw:
        return None
    cleaned = _UNIT_CLAUSE.sub("", raw)
    cleaned = dedupe_address_segments(re.sub(r"\s+,", ",", cleaned))
    cleaned = dedupe_address_segments(cleaned)
    if not cleaned or cleaned.casefold() == raw.casefold():
        return None
    return cleaned


def enhanced_street_query(address: str, locality: list[str]) -> str | None:
    """Query = road + house_number del parser + localidad (opt-in enhance)."""
    from .address import parse

    parsed = parse(address or "")
    road = (parsed.road or "").strip()
    number = (parsed.house_number or "").strip()
    if not road:
        return None
    core = f"{road} {number}".strip() if number else road
    return append_missing(core, locality) or None


def build_geocode_query(
    address: str,
    *,
    row: Mapping[str, Any] | None = None,
    depot: DepotContext | None = None,
    enhance: bool = False,
) -> str:
    """Query interna para el indice. Nunca reemplaza el address de la fila."""
    display = dedupe_address_segments((address or "").strip())
    if not display:
        return ""

    locality = locality_tokens_from_row(row)
    if enhance:
        rebuilt = enhanced_street_query(display, locality)
        base = rebuilt or append_missing(display, locality)
    else:
        base = append_missing(display, locality)

    if depot is not None:
        return depot.enrich_address(base)
    return base


def result_rank(result: GeocodeResult | None) -> tuple:
    """Mayor = mejor. Preferir housenumber sobre street, luego status, luego conf."""
    if result is None or not result.has_coords:
        return (0, 0, 0.0)
    precision = {"housenumber": 3, "street": 2, "locality": 1}.get(
        (result.precision or "").lower(), 0)
    status = {STATUS_MATCHED: 2, STATUS_LOW: 1}.get(result.status, 0)
    return (precision, status, float(result.confidence or 0.0))


def is_weak_result(result: GeocodeResult | None, *, review_band: float) -> bool:
    """Vale la pena un reintento limpio / enhance."""
    if result is None or result.status in (STATUS_NOT_FOUND, "error"):
        return True
    if not result.has_coords:
        return True
    if result.status == STATUS_LOW:
        return True
    if (result.precision or "").lower() == "street":
        return True
    if float(result.confidence or 0.0) < float(review_band):
        return True
    return False


def pick_better(*results: GeocodeResult | None) -> GeocodeResult | None:
    """Elige el mejor resultado. Nunca descarta un GeocodeResult a favor de None.

    Regresión Tandil: dos `not_found` (sin coords) empataban con el rank de
    `None` → `pick_better` devolvía None y el runner hacía
    `result = better` → AttributeError en `result.status`.
    """
    best = None
    for result in results:
        if result is None:
            continue
        if best is None or result_rank(result) > result_rank(best):
            best = result
    return best
