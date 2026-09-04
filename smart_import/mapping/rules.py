"""Paso 1-2: alias exacto y nombre normalizado. Sin IA, sin fuzzy."""
from __future__ import annotations

from ..schemas import TargetSchema, normalize_key


def candidates(column: str, schema: TargetSchema) -> list[tuple[str, float, str, str]]:
    """[(target, confidence, method, evidence)] por coincidencia de nombre."""
    out: list[tuple[str, float, str, str]] = []
    raw = str(column).strip()
    norm = normalize_key(raw)
    if not norm:
        return out

    for fname, f in schema.fields.items():
        if raw == fname:
            out.append((fname, 1.0, "alias", f"nombre exacto '{raw}'"))
            continue
        if norm == normalize_key(fname):
            out.append((fname, 0.99, "alias", f"nombre normalizado '{norm}'"))
            continue
        if norm in f.normalized_aliases:
            out.append((fname, 0.97, "normalized", f"alias '{norm}'"))
    return out
