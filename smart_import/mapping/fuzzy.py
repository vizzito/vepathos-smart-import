"""Paso 3: fuzzy matching sobre nombres de columna (rapidfuzz)."""
from __future__ import annotations

from ..schemas import TargetSchema, normalize_key

MIN_SCORE = 0.78
# Alias cortos ("lon", "x", "tel") solo matchean por rules exactas: el fuzzy
# de "Nylon"~"lon" o "Chañ"~"cant" inventa mappings peligrosos.
MIN_ALIAS_LEN = 4
# Si las longitudes difieren mucho, partial_ratio encuentra substrings basura.
MIN_LENGTH_RATIO = 0.65


def candidates(column: str, schema: TargetSchema) -> list[tuple[str, float, str, str]]:
    from rapidfuzz import fuzz

    norm = normalize_key(column)
    if not norm:
        return []

    out: list[tuple[str, float, str, str]] = []
    for fname, f in schema.fields.items():
        if f.input_only:
            continue  # compound semantics require an exact alias or explicit review
        best, best_alias = 0.0, ""
        for alias in (normalize_key(fname), *f.normalized_aliases):
            if not alias or len(alias) < MIN_ALIAS_LEN:
                continue
            # token_set_ratio NO sirve aca: da 100 cuando el alias es subconjunto
            # del nombre ("datos entrega" vs "entrega"), que es justo el caso en
            # que el mapeo por nombre NO alcanza y hay que mandarlo a revision.
            # token_sort_ratio si distingue; partial_ratio se usa para abreviaturas
            # pero penalizado por diferencia de longitud.
            length_penalty = min(len(norm), len(alias)) / max(len(norm), len(alias))
            if length_penalty < MIN_LENGTH_RATIO:
                continue
            # partial_ratio("nylon","lon")=100; ratio ya da 75 — exige overlap real.
            partial = fuzz.partial_ratio(norm, alias) * length_penalty
            score = max(
                fuzz.token_sort_ratio(norm, alias),
                fuzz.ratio(norm, alias),
                partial,
            ) / 100.0
            # Contencion accidental: "nylon" contiene "lon" pero no es longitud.
            shorter, longer = (alias, norm) if len(alias) <= len(norm) else (norm, alias)
            if shorter in longer and len(shorter) < len(longer) and length_penalty < 0.85:
                score = min(score, 0.70)
            if score > best:
                best, best_alias = score, alias
        # Tokens cortos (chan~cant) necesitan casi exactitud; el fuzzy laxo inventa.
        min_ok = MIN_SCORE
        if best_alias and (len(best_alias) <= 5 or len(norm) <= 5):
            min_ok = max(MIN_SCORE, 0.90)
        if best >= min_ok:
            # techo 0.95: el fuzzy nunca debe ganarle a un alias exacto
            out.append((fname, min(0.95, best), "fuzzy", f"~'{best_alias}' ({best:.2f})"))
    return out
