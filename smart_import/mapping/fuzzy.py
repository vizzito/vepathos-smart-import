"""Paso 3: fuzzy matching sobre nombres de columna (rapidfuzz)."""
from __future__ import annotations

from ..schemas import TargetSchema, normalize_key

MIN_SCORE = 0.72


def candidates(column: str, schema: TargetSchema) -> list[tuple[str, float, str, str]]:
    from rapidfuzz import fuzz

    norm = normalize_key(column)
    if not norm:
        return []

    out: list[tuple[str, float, str, str]] = []
    for fname, f in schema.fields.items():
        best, best_alias = 0.0, ""
        for alias in (normalize_key(fname), *f.normalized_aliases):
            if not alias:
                continue
            # token_set_ratio NO sirve aca: da 100 cuando el alias es subconjunto
            # del nombre ("datos entrega" vs "entrega"), que es justo el caso en
            # que el mapeo por nombre NO alcanza y hay que mandarlo a revision.
            # token_sort_ratio si distingue; partial_ratio se usa para abreviaturas
            # pero penalizado por diferencia de longitud.
            length_penalty = min(len(norm), len(alias)) / max(len(norm), len(alias))
            score = max(
                fuzz.token_sort_ratio(norm, alias),
                fuzz.ratio(norm, alias),
                fuzz.partial_ratio(norm, alias) * length_penalty,
            ) / 100.0
            if score > best:
                best, best_alias = score, alias
        if best >= MIN_SCORE:
            # techo 0.95: el fuzzy nunca debe ganarle a un alias exacto
            out.append((fname, min(0.95, best), "fuzzy", f"~'{best_alias}' ({best:.2f})"))
    return out
