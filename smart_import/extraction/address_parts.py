"""De un string de address → campos del schema (partes) → compose final.

El extractor de texto libre produce un ``address`` crudo. Este paso:
  1. parsea con el AddressParser (heuristico / enhanced)
  2. rellena house_number / unit / zone / city / region / postcode / country
     sin pisar lo que ya vino por etiquetas
  3. si hay ``road``, deja address = calle (como columna street tabular)
  4. compone el string final con la misma convencion que el normalize tabular

Asi tabular y free-text terminan en el MISMO objeto schema antes de geocode.
"""
from __future__ import annotations

from typing import Any

from ..normalization.address import apply_composed_address
from .result import ExtractedRecord, FieldValue

#: componente ParsedAddress → campo del schema Vepathos
_COMPONENT_TO_SCHEMA: tuple[tuple[str, str], ...] = (
    ("house_number", "house_number"),
    ("unit", "unit"),
    ("suburb", "zone"),
    ("neighbourhood", "zone"),   # setdefault: suburb gana si ambos
    ("city", "city"),
    ("state", "region"),
    ("postcode", "postcode"),
    ("country", "country"),
)

_PART_FIELDS = (
    "address", "house_number", "unit", "zone", "city", "region", "postcode", "country",
)


def fill_address_parts_from_text(
    record: ExtractedRecord,
    parser,
    *,
    context: Any = None,
) -> bool:
    """Rellena partes + recomponer address. True si toco algun campo."""
    raw_address = record.get("address")
    if not raw_address or parser is None:
        return False

    parsed = parser.parse(str(raw_address), context)
    changed = False

    for component, schema_field in _COMPONENT_TO_SCHEMA:
        value = (parsed.get(component) or "").strip()
        if not value:
            continue
        if schema_field == "unit" and parsed.get("level") and not record.get("unit"):
            # "3er piso" a veces viene como level
            level = (parsed.get("level") or "").strip()
            if level and level.lower() not in value.lower():
                value = f"{value}, piso {level}".strip(", ")
        before = record.get(schema_field)
        record.set(FieldValue(
            schema_field, value, value, record.confidence_of("address"),
            f"address_parts:{parsed.parser}", None,
            (f"parser {component} → {schema_field}",),
        ))
        if record.get(schema_field) != before:
            changed = True

    # level solo → unit
    level = (parsed.get("level") or "").strip()
    if level and not record.get("unit"):
        record.set(FieldValue(
            "unit", f"piso {level}", level, record.confidence_of("address"),
            f"address_parts:{parsed.parser}", None, ("parser level → unit",),
        ))
        changed = True

    road = (parsed.get("road") or "").strip()
    if road and record.get("house_number"):
        # Igual que tabular: address = calle; la altura vive en house_number.
        previous = record.fields["address"]
        if previous.value != road:
            record.fields["address"] = FieldValue(
                "address", road, previous.raw, previous.confidence, previous.method,
                previous.span,
                (*previous.evidence, f"address_parts: road='{road}'"),
            )
            changed = True

    values = {name: record.get(name) for name in _PART_FIELDS}
    if context is not None and not values.get("country"):
        ctx_country = getattr(context, "country", None)
        if ctx_country:
            values["country"] = ctx_country
    if apply_composed_address(values):
        previous = record.fields.get("address")
        composed = values["address"]
        record.fields["address"] = FieldValue(
            "address", composed,
            previous.raw if previous else composed,
            previous.confidence if previous else record.confidence_of("address"),
            previous.method if previous else "address_parts",
            previous.span if previous else None,
            (*(previous.evidence if previous else ()), "compose desde partes"),
        )
        changed = True

    return changed
