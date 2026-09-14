"""Variantes de direcciones con ruido humano para probar parser/enhance/geocode.

Niveles (1–5):
  1 — calle + altura mínima (sin ciudad/CP/país)
  2 — + un dato extra (CP o depto)
  3 — subconjunto con ciudad/país; permutaciones de orden
  4 — como 3 + typos / puntuación rota
  5 — dirección casi completa; permutaciones de bloques

Cada variante conserva lat/lng del registro base (ground truth).
"""
from __future__ import annotations

import itertools
import random
import re
import unicodedata
from dataclasses import replace
from typing import Callable

from .address_corpus import CorpusRecord, _COUNTRY_LABELS

_STREET_PREFIXES = (
    "Avenida ", "Av. ", "Av ", "Calle ", "Pasaje ", "Paseo ", "Boulevard ",
    "Diagonal ",
)

_SEPARATORS = (", ", " ", " - ")


def _strip_street_prefix(street: str) -> str:
    for prefix in _STREET_PREFIXES:
        if street.startswith(prefix):
            return street[len(prefix):]
    return street


def _country_label(code: str | None) -> str | None:
    if not code:
        return None
    return _COUNTRY_LABELS.get(code.upper(), code)


def _street_block(rec: CorpusRecord, *, short: bool = False) -> str:
    street = (rec.street or "").strip()
    if not street:
        return ""
    if short:
        return _strip_street_prefix(street)
    return street


def _core_line(rec: CorpusRecord, *, short_street: bool = False,
               sep: str = " ") -> str:
    street = _street_block(rec, short=short_street)
    number = (rec.number or "").strip()
    if street and number:
        if sep == ", ":
            return f"{street}, {number}"
        return f"{street}{sep}{number}".strip()
    return street or number


def _oa_parts(rec: CorpusRecord, *, include_country: bool = True) -> list[str]:
    """Bloques estilo OpenAddresses (Av. X, 800, ciudad, CP, país)."""
    parts: list[str] = []
    if rec.street:
        parts.append(rec.street)
    if rec.number:
        parts.append(rec.number)
    if rec.unit:
        parts.append(f"Apt {rec.unit}")
    if rec.city:
        parts.append(rec.city)
    if rec.postcode:
        parts.append(rec.postcode)
    if include_country:
        country = _country_label(rec.country)
        if country:
            parts.append(country)
    return parts


def _sparse_parts(rec: CorpusRecord) -> list[str]:
    """Nivel 3: calle+altura, ciudad, país (sin CP)."""
    parts: list[str] = []
    core = _core_line(rec, short_street=True, sep=" ")
    if core:
        parts.append(core)
    if rec.city:
        parts.append(rec.city)
    country = _country_label(rec.country)
    if country:
        parts.append(country.lower())
    return parts


def _join_permutation(parts: list[str], sep: str) -> str:
    if not parts:
        return ""
    if sep == " ":
        return " ".join(parts)
    out = parts[0]
    for part in parts[1:]:
        out = f"{out}{sep}{part}"
    return out


def _permutations(parts: list[str], *, max_count: int,
                  rng: random.Random) -> list[list[str]]:
    if len(parts) <= 1:
        return [parts]
    perms = list(itertools.permutations(parts))
    rng.shuffle(perms)
    uniq: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for perm in perms:
        key = tuple(perm)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(list(perm))
        if len(uniq) >= max_count:
            break
    return uniq or [parts]


def _drop_one(parts: list[str], rng: random.Random) -> list[str]:
    if len(parts) <= 2:
        return parts
    idx = rng.randrange(len(parts))
    return parts[:idx] + parts[idx + 1:]


def _typo_light(text: str, rng: random.Random) -> str:
    """Typos suaves: mayúsculas, espacios, acentos, palabra duplicada."""
    out = text
    if rng.random() < 0.5:
        out = re.sub(r"\b([Aa]v\.?)\b", lambda m: m.group(1).lower(), out, count=1)
    if rng.random() < 0.4 and "avenida" not in out.lower():
        out = out.replace("Av.", "avenida ", 1).replace("  ", " ")
    if rng.random() < 0.35:
        out = re.sub(r",(\S)", r", \1", out)
        out = re.sub(r"  +", " ", out)
    if rng.random() < 0.25:
        out = unicodedata.normalize("NFD", out)
        out = "".join(ch for ch in out if unicodedata.category(ch) != "Mn")
    if rng.random() < 0.2:
        words = out.split()
        if len(words) >= 2:
            i = rng.randrange(len(words))
            words.insert(i, words[i])
            out = " ".join(words)
    if rng.random() < 0.3:
        out = out.lower() if rng.random() < 0.5 else out
    return out.strip()


def _variants_level1(rec: CorpusRecord, rng: random.Random) -> list[str]:
    forms = [
        _core_line(rec, short_street=True, sep=", "),
        _core_line(rec, short_street=True, sep=" "),
        f"{rec.number} {_strip_street_prefix(rec.street or '')}".strip(),
    ]
    return [f for f in dict.fromkeys(f.strip() for f in forms if f.strip())]


def _variants_level2(rec: CorpusRecord, rng: random.Random) -> list[str]:
    base = _core_line(rec, short_street=True, sep=" ")
    out: list[str] = []
    if rec.postcode:
        out.append(f"{base}, {rec.postcode}")
        out.append(f"{base} {rec.postcode}")
    if rec.unit:
        out.append(f"{base}, depto {rec.unit}")
        out.append(f"{base} dto {rec.unit}")
    if rec.city and not out:
        out.append(f"{base}, {rec.city}")
    return [f for f in dict.fromkeys(f.strip() for f in out if f.strip())] or _variants_level1(rec, rng)


def _variants_level3(rec: CorpusRecord, rng: random.Random,
                     *, max_permutations: int) -> list[str]:
    parts = _sparse_parts(rec)
    if len(parts) < 2:
        return _variants_level2(rec, rng)
    out: list[str] = []
    for sep in _SEPARATORS[:2]:
        for perm in _permutations(parts, max_count=max_permutations, rng=rng):
            line = _join_permutation(perm, sep)
            if line:
                out.append(line)
    return list(dict.fromkeys(out))


def _variants_level4(rec: CorpusRecord, rng: random.Random,
                     *, max_permutations: int) -> list[str]:
    bases = _variants_level3(rec, rng, max_permutations=max_permutations)
    out: list[str] = []
    for base in bases:
        out.append(_typo_light(base, rng))
        if rng.random() < 0.5:
            out.append(_typo_light(base, rng))
    return list(dict.fromkeys(f for f in out if f))


def _variants_level5(rec: CorpusRecord, rng: random.Random,
                     *, max_permutations: int) -> list[str]:
    parts = _oa_parts(rec, include_country=False)
    if len(parts) < 2:
        return [rec.address]
    out: list[str] = []
    for sep in _SEPARATORS:
        for perm in _permutations(parts, max_count=max_permutations, rng=rng):
            line = _join_permutation(perm, sep)
            if line:
                out.append(line)
        dropped = _drop_one(parts, rng)
        if len(dropped) >= 2:
            for perm in _permutations(dropped, max_count=max(2, max_permutations // 2),
                                      rng=rng):
                line = _join_permutation(perm, sep)
                if line:
                    out.append(line)
    out.append(rec.address)
    return list(dict.fromkeys(out))


_LEVEL_BUILDERS: dict[int, Callable[..., list[str]]] = {
    1: _variants_level1,
    2: _variants_level2,
    3: _variants_level3,
    4: _variants_level4,
    5: _variants_level5,
}


def expand_records_with_noise(
    records: list[CorpusRecord],
    level: int,
    *,
    seed: int = 42,
    cumulative: bool = False,
    max_permutations: int = 4,
    rate: float = 1.0,
    include_baseline: bool = False,
) -> list[CorpusRecord]:
    """Expande cada registro base en variantes ruidosas verificables."""
    if level < 1 or level > 5:
        raise ValueError("noise level debe estar entre 1 y 5")
    if not 0.0 < rate <= 1.0:
        raise ValueError("noise rate debe estar en (0, 1]")

    rng = random.Random(seed)
    levels = list(range(1, level + 1)) if cumulative else [level]
    out: list[CorpusRecord] = []

    for rec in records:
        if not rec.has_coords or not rec.street or not rec.number:
            out.append(rec)
            continue
        if rate < 1.0 and rng.random() > rate:
            if include_baseline:
                out.append(rec)
            continue

        clean = rec.address
        if include_baseline:
            out.append(replace(
                rec,
                extra={**rec.extra, "address_clean": clean, "noise_level": 0,
                         "noise_variant": 0},
            ))

        for lv in levels:
            builder = _LEVEL_BUILDERS[lv]
            if lv >= 3:
                variants = builder(rec, rng, max_permutations=max_permutations)
            else:
                variants = builder(rec, rng)
            for vi, variant in enumerate(variants, start=1):
                sid = rec.source_id or "row"
                out.append(replace(
                    rec,
                    address=variant,
                    source=f"{rec.source}+noise",
                    source_id=f"{sid}_n{lv}_v{vi}",
                    style=f"noise{lv}",
                    extra={
                        **rec.extra,
                        "address_clean": clean,
                        "noise_level": lv,
                        "noise_variant": vi,
                    },
                ))
    return out
