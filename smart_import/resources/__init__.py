"""Datos de dominio fuera del codigo ejecutable.

Las listas de palabras (etiquetas, unidades administrativas, abreviaturas…)
viven en JSON junto a este paquete. La logica solo las carga.

Overrides de despliegue:
  SMART_IMPORT_LABELS_PATH          → labels.json
  SMART_IMPORT_GEO_KEYWORDS_PATH    → geo_keywords.json
  SMART_IMPORT_OVERRIDES_PATH       → vepathos_overrides.json
  SMART_IMPORT_VOCAB_PATH           → smart_import_vocab.sqlite
  SMART_IMPORT_LOCALITY_EXPAND_PATH → locality_expand.json
  SMART_IMPORT_PACKAGE_LEXICON_PATH → package_lexicon.json
  SMART_IMPORT_PBF_BOUNDS_PATH      → pbf_country_bounds.json
  SMART_IMPORT_RESOURCES_DIR        → directorio que reemplaza todos los JSON
                                      por nombre de archivo
"""
from __future__ import annotations

import json
import os
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent

LABELS_FILE = _DIR / "labels.json"
GEO_KEYWORDS_FILE = _DIR / "geo_keywords.json"
OVERRIDES_FILE = _DIR / "vepathos_overrides.json"

#: campos del labels.json que son listas de etiquetas por locale
LABEL_GROUPS = (
    "name_labels", "address_labels", "phone_labels", "time_labels",
    "reference_labels", "package_labels", "deliver_prefixes", "deliver_linkers",
    "name_prefixes", "greetings", "closings", "street_tokens",
    "street_suffixes",
)


def fold(text: str) -> str:
    """NFKD + minusculas + sin marcas: 'Direccion' ~ 'direccion'."""
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _resolve(filename: str, env_var: str | None = None) -> Path:
    """Override por archivo, o por directorio compartido, o el empaquetado."""
    if env_var:
        override = os.getenv(env_var)
        if override:
            return Path(override)
    base = os.getenv("SMART_IMPORT_RESOURCES_DIR")
    if base:
        candidate = Path(base) / filename
        if candidate.exists():
            return candidate
    return _DIR / filename


@lru_cache(maxsize=32)
def load_json(filename: str, env_var: str | None = None) -> dict[str, Any]:
    """Carga un JSON de resources/. Fallback al empaquetado si el override falta."""
    target = _resolve(filename, env_var)
    if not target.exists():
        target = _DIR / filename
    return json.loads(target.read_text(encoding="utf-8"))


def string_list(filename: str, key: str, *, env_var: str | None = None) -> tuple[str, ...]:
    data = load_json(filename, env_var)
    items = data.get(key) or ()
    return tuple(str(x) for x in items)


def string_set(filename: str, key: str, *, env_var: str | None = None,
               fold_words: bool = False) -> frozenset[str]:
    words = string_list(filename, key, env_var=env_var)
    if fold_words:
        return frozenset(fold(w) for w in words)
    return frozenset(words)


def string_map(filename: str, key: str, *, env_var: str | None = None) -> dict[str, str]:
    raw = load_json(filename, env_var).get(key) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


# --------------------------------------------------------------------------- labels

@lru_cache(maxsize=4)
def load_labels(path: str | None = None) -> dict:
    """El JSON crudo. `path=None` usa SMART_IMPORT_LABELS_PATH o el empaquetado."""
    if path:
        target = Path(path)
    else:
        target = _resolve("labels.json", "SMART_IMPORT_LABELS_PATH")
    if not target.exists():
        target = LABELS_FILE
    return json.loads(target.read_text(encoding="utf-8"))


@lru_cache(maxsize=32)
def label_set(group: str, locales: tuple[str, ...] | None = None) -> frozenset[str]:
    """Union foldeada de un grupo de etiquetas. `locales=None` = todos."""
    data = load_labels()
    wanted = set(locales) if locales else None
    out: set[str] = set()
    for code, pack in data["locales"].items():
        if wanted is not None and code not in wanted:
            continue
        out.update(fold(w) for w in pack.get(group, ()))
    return frozenset(out)


def available_locales() -> tuple[str, ...]:
    return tuple(sorted(load_labels()["locales"]))


# --------------------------------------------------------------------------- geo keywords

_GEO_ENV = "SMART_IMPORT_GEO_KEYWORDS_PATH"
_GEO_FILE = "geo_keywords.json"


def admin_unit_words() -> frozenset[str]:
    """Subdivisiones administrativas numeradas (Comuna 2, 11th Arrondissement…)."""
    return string_set(_GEO_FILE, "admin_unit_words", env_var=_GEO_ENV)


def name_glue_words() -> frozenset[str]:
    """Conectores validos dentro de un nombre de calle (de, del, of…)."""
    return string_set(_GEO_FILE, "name_glue", env_var=_GEO_ENV, fold_words=True)


def road_narration_words() -> frozenset[str]:
    """Lo que la gente escribe ALREDEDOR de una calle, no la calle.

    'que vive en Av. Corrientes' -> 'que vive en' es narracion. Se listan estas
    (conjunto chico y cerrado) en vez de exigir que el nombre de calle venga
    capitalizado: los exports reales traen 'Av cabildo 834' y 'AV JUAN DE GARAY'.
    """
    return string_set(_GEO_FILE, "road_narration_words", env_var=_GEO_ENV, fold_words=True)


def landmark_hints() -> tuple[str, ...]:
    return string_list(_GEO_FILE, "landmark_hints", env_var=_GEO_ENV)


def unit_hints() -> tuple[str, ...]:
    return string_list(_GEO_FILE, "unit_hints", env_var=_GEO_ENV)


def building_tokens() -> frozenset[str]:
    """Palabras que delatan el nombre de un edificio ('Towers', 'Bhavan', 'Plaza').

    Sin esto no hay forma de distinguir 'Cyber Towers' (edificio) de 'Rahul Sharma'
    (persona): las dos son dos palabras alfabeticas sin digitos.
    """
    return string_set(_GEO_FILE, "building_tokens", env_var=_GEO_ENV, fold_words=True)


def suspicious_road_words() -> frozenset[str]:
    """Palabras que el heuristico a veces toma como calle y no lo son."""
    return string_set(_GEO_FILE, "suspicious_road_words", env_var=_GEO_ENV, fold_words=True)


def suspicious_road_prefixes() -> tuple[str, ...]:
    """Prefijos de direccionamiento sin calle ('plot ', 'manzana ')."""
    return string_list(_GEO_FILE, "suspicious_road_prefixes", env_var=_GEO_ENV)


def unit_prefixes() -> tuple[str, ...]:
    return string_list(_GEO_FILE, "unit_prefixes", env_var=_GEO_ENV)


def landmark_prefixes() -> tuple[str, ...]:
    return string_list(_GEO_FILE, "landmark_prefixes", env_var=_GEO_ENV)


def neighbourhood_prefixes() -> tuple[str, ...]:
    return string_list(_GEO_FILE, "neighbourhood_prefixes", env_var=_GEO_ENV)


def address_abbreviations() -> dict[str, str]:
    curated = string_map(_GEO_FILE, "abbreviations", env_var=_GEO_ENV)
    extra = _store().address_expand()
    if not extra:
        return curated
    return {**extra, **curated}


# --------------------------------------------------------------------------- mapping vocab (SQLite / catálogo)

def _store():
    from ..vocab.store import get_store
    return get_store()


def packaging_words() -> frozenset[str]:
    return _store().aliases("packaging")


_PKG_ENV = "SMART_IMPORT_PACKAGE_LEXICON_PATH"
_PKG_FILE = "package_lexicon.json"


def package_lexicon() -> dict[str, Any]:
    """Numeros en letras, abreviaturas de despacho, unidades y pistas cada-uno/total.

    Los SUSTANTIVOS de bulto no estan aca: salen del catalogo (`packaging_alias_map`).
    """
    return load_json(_PKG_FILE, _PKG_ENV)


def packaging_alias_map() -> dict[str, dict]:
    """alias foldeado → {canonical, code, key} del dominio packaging.

    Es el MISMO catalogo que usa el mapper de columnas; el lector de texto libre
    lo usa para reconocer 'cajas' / 'boxes' / 'caixas' como el concepto `box`.
    """
    return _store().alias_map("packaging", words_only=True)


def packaging_codes() -> frozenset[str]:
    """Codigos UNECE/GS1 de 2–3 letras (BX, BG…). Nunca se usan en fuzzy."""
    store = _store()
    return store.codes("packaging") | store.aliases("packaging", codes_only=True)


def ambiguous_aliases() -> tuple[dict, ...]:
    """Alias que significan cosas distintas segun que OTRAS columnas haya.

    'Altura' junto a 'Calle' es el numero de puerta; junto a 'Ancho' es el alto
    del bulto. No se resuelve con alias —el nombre es el mismo— sino con el
    contexto de columnas del archivo. Fuente: vepathos_overrides.json.
    """
    datos = load_json("vepathos_overrides.json", "SMART_IMPORT_OVERRIDES_PATH").get(
        "ambiguous_aliases"
    ) or []
    return tuple(datos)


def status_words() -> frozenset[str]:
    return _store().aliases("status")


# --------------------------------------------------------------------------- locality expand (address maximize / dedupe)

_LOC_ENV = "SMART_IMPORT_LOCALITY_EXPAND_PATH"
_LOC_FILE = "locality_expand.json"


@lru_cache(maxsize=1)
def phone_region_country_map() -> dict[str, str]:
    """ISO-2 (mayúsculas) → nombre de país preferido para queries OSM."""
    raw = load_json(_LOC_FILE, _LOC_ENV).get("phone_region_country") or {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        code = str(k).strip().upper()
        name = str(v).strip()
        if code and name:
            out[code] = name
    # Fallback: tabla ISO completa empaquetada
    if len(out) < 10:
        iso = load_json("iso3166_alpha2.json")
        for k, v in iso.items():
            out.setdefault(str(k).upper(), str(v))
    return out


@lru_cache(maxsize=1)
def locality_alias_groups() -> tuple[frozenset[str], ...]:
    """Grupos de aliases foldeados (CABA ≈ Ciudad Autónoma…)."""
    groups = load_json(_LOC_FILE, _LOC_ENV).get("alias_groups") or ()
    return tuple(
        frozenset(fold(str(x)) for x in group if str(x).strip())
        for group in groups
        if group
    )


@lru_cache(maxsize=1)
def locality_expansions() -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """(cues_folded, tokens). Curado primero, luego GeoNames si el archivo existe."""
    out: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    seen_cue: set[str] = set()

    def _ingest(rows) -> None:
        for row in rows or ():
            if not isinstance(row, dict):
                continue
            cues_raw = [str(c) for c in (row.get("cues") or ()) if str(c).strip()]
            tokens = tuple(str(t).strip() for t in (row.get("tokens") or ()) if str(t).strip())
            if not cues_raw or not tokens:
                continue
            cues: list[str] = []
            for c in cues_raw:
                fc = fold(c)
                if fc in seen_cue:
                    continue
                seen_cue.add(fc)
                cues.append(fc)
            if cues:
                out.append((tuple(cues), tokens))

    # 1) curado (aliases finos: CABA, barrios, CDMX…)
    _ingest(load_json(_LOC_FILE, _LOC_ENV).get("expansions"))
    # 2) GeoNames opcional (cities15000) — no falla si falta el archivo
    geonames = _resolve("locality_expand_geonames.json", "SMART_IMPORT_LOCALITY_GEONAMES_PATH")
    if not geonames.exists():
        geonames = _DIR / "locality_expand_geonames.json"
    if geonames.exists():
        try:
            _ingest(json.loads(geonames.read_text(encoding="utf-8")).get("expansions"))
        except (OSError, json.JSONDecodeError):
            pass
    return tuple(out)


# --------------------------------------------------------------------------- PBF coverage bounds (qué archivo abrir, no calles)

_BOUNDS_FILE = "pbf_country_bounds.json"
_BOUNDS_ENV = "SMART_IMPORT_PBF_BOUNDS_PATH"
_REGION_FILE = "pbf_region_bounds.json"
_REGION_ENV = "SMART_IMPORT_PBF_REGION_BOUNDS_PATH"


def _norm_slug(value: str) -> str:
    return (value or "").strip().lower().replace("_", "-")


def _parse_bounds_row(slug: str, row: dict) -> dict | None:
    if not isinstance(row, dict):
        return None
    try:
        bounds = (float(row["n"]), float(row["s"]), float(row["e"]), float(row["w"]))
    except (KeyError, TypeError, ValueError):
        return None
    keys = tuple(
        k for k in (_norm_slug(slug), *(_norm_slug(a) for a in (row.get("aliases") or ())))
        if k
    )
    if not keys:
        return None
    hints = tuple(str(h).lower() for h in (row.get("path_hints") or ()) if str(h).strip())
    return {
        "keys": keys,
        "bounds": bounds,
        "path_hints": hints,
        "label": str(row.get("label") or "").strip(),
    }


@lru_cache(maxsize=1)
def _coverage_records() -> tuple[dict, ...]:
    """País + subdivisiones Geofabrik (florida, kanto, western-zone…)."""
    out: list[dict] = []
    countries = load_json(_BOUNDS_FILE, _BOUNDS_ENV).get("countries") or {}
    regions = load_json(_REGION_FILE, _REGION_ENV).get("regions") or {}
    for slug, row in {**countries, **regions}.items():
        parsed = _parse_bounds_row(slug, row)
        if parsed:
            out.append(parsed)
    return tuple(out)


@lru_cache(maxsize=1)
def country_bounds() -> dict[str, tuple[float, float, float, float]]:
    """slug → bbox. Si hay alias colisionado (georgia), gana el registro sin hints."""
    out: dict[str, tuple[float, float, float, float]] = {}
    hinted: dict[str, tuple[float, float, float, float]] = {}
    for rec in _coverage_records():
        target = hinted if rec["path_hints"] else out
        for key in rec["keys"]:
            target.setdefault(key, rec["bounds"])
    for key, bounds in hinted.items():
        out.setdefault(key, bounds)
    return out


def coverage_bounds_for(
    slug: str | None, path: str | Path | None = None,
) -> tuple[float, float, float, float] | None:
    """Bbox del PBF: slug del archivo + pista de carpeta (`us_tile`, `/india/`)."""
    key = _norm_slug(slug or "")
    if not key:
        return None
    path_l = str(path or "").lower().replace("\\", "/")
    matches: list[tuple[int, tuple[float, float, float, float]]] = []
    for rec in _coverage_records():
        if key not in rec["keys"]:
            continue
        hints = rec["path_hints"]
        if hints:
            score = sum(1 for h in hints if h in path_l)
            if path_l and score == 0:
                continue
            matches.append((score if path_l else 0, rec["bounds"]))
        else:
            matches.append((0, rec["bounds"]))
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


@lru_cache(maxsize=1)
def country_label_from_slug() -> dict[str, str]:
    """slug → label. Preferí registros sin path_hints cuando hay colisión."""
    out: dict[str, str] = {}
    hinted: dict[str, str] = {}
    for rec in _coverage_records():
        if not rec["label"]:
            continue
        target = hinted if rec["path_hints"] else out
        for key in rec["keys"]:
            target.setdefault(key, rec["label"])
    for key, label in hinted.items():
        out.setdefault(key, label)
    return out
