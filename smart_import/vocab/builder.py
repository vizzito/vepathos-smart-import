"""Construye smart_import_vocab.sqlite desde el catálogo + fuentes vendored.

Fuente de verdad humana: resources/vocab/catalog.json (concepto + aliases).
UNECE / GS1 / libpostal se FUSIONAN al concepto existente (mismo código o alias).
Overrides: slang de clientes; si el alias ya existe, no crea un concepto nuevo.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from ..resources import _DIR, fold
from .store import VocabularyStore, _is_standard_code

VOCAB_DIR = _DIR / "vocab"
CATALOG_FILE = VOCAB_DIR / "catalog.json"
OVERRIDES_FILE = _DIR / "vepathos_overrides.json"
UNECE_FILE = VOCAB_DIR / "unece_rec21.csv"
GS1_FILE = VOCAB_DIR / "gs1_cbv_status.json"
LIBPOSTAL_FILE = VOCAB_DIR / "libpostal_street.json"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def build(
    dest: Path | str | None = None,
    *,
    catalog: bool = True,
    unece: bool = True,
    gs1: bool = True,
    libpostal: bool = True,
    overrides: bool = True,
) -> VocabularyStore:
    if dest is None:
        store = VocabularyStore.memory()
    else:
        dest = Path(dest)
        if dest.exists():
            dest.unlink()
        store = VocabularyStore.open(dest, create=True)
    if catalog:
        _seed_catalog(store)
    if unece:
        _import_unece(store)
    if gs1:
        _import_gs1(store)
    if libpostal:
        _import_libpostal(store)
    if overrides:
        _apply_overrides(store)
    store.commit()
    return store


def _slug(text: str) -> str:
    folded = fold(text) or text.lower()
    return _SLUG_RE.sub("_", folded).strip("_") or "unknown"


def _seed_catalog(store: VocabularyStore) -> None:
    data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    for item in data.get("packaging") or ():
        _ingest_catalog_item(store, item, domain="packaging")
    for item in data.get("status") or ():
        _ingest_catalog_item(store, item, domain="status")


def _ingest_catalog_item(store: VocabularyStore, item: dict, *, domain: str) -> int:
    key = str(item["id"]).strip()
    canonical = str(item.get("canonical") or "").strip()
    code = str(item.get("code") or "").strip()
    cid = store.upsert_concept(
        key, domain, canonical or key, code=code, source="catalog",
    )
    if canonical:
        store.add_alias(cid, canonical, language="en", source="catalog")
    if code:
        store.add_alias(cid, code.upper(), language="und", source="catalog")
    for lang, words in (item.get("aliases") or {}).items():
        if str(lang).startswith("_"):
            continue
        for raw in words or ():
            store.add_alias(cid, str(raw), language=str(lang), source="catalog")
    return cid


def _attach(
    store: VocabularyStore,
    *,
    domain: str,
    fallback_key: str,
    canonical: str,
    code: str = "",
    source: str,
    aliases: list[tuple[str, str | None]],
) -> int:
    cid = None
    if code:
        cid = store.concept_id_by_code(domain, code)
    if cid is None:
        cid = store.concept_id_by_alias(domain, canonical)
    if cid is None:
        for alias, _lang in aliases:
            cid = store.concept_id_by_alias(domain, alias)
            if cid is not None:
                break
    if cid is None:
        cid = store.upsert_concept(
            fallback_key, domain, canonical, code=code, source=source,
        )
    elif code:
        row = store.conn.execute(
            "SELECT key, canonical, code FROM concepts WHERE id = ?", (cid,)
        ).fetchone()
        if row and not row["code"] and code:
            store.conn.execute(
                "UPDATE concepts SET code = ? WHERE id = ?", (code, cid)
            )
    for alias, lang in aliases:
        store.add_alias(cid, alias, language=lang, source=source)
    if code:
        store.add_alias(cid, code, language="und", source=source)
    return cid


def _import_unece(store: VocabularyStore) -> None:
    if not UNECE_FILE.exists():
        return
    with UNECE_FILE.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            code = (row.get("code") or "").strip().upper()
            name = (row.get("name") or "").strip()
            if not code or not name:
                continue
            canonical = fold(name) or name.lower()
            _attach(
                store,
                domain="packaging",
                fallback_key=f"package.{_slug(name)}",
                canonical=canonical,
                code=code,
                source="UNECE_REC21",
                aliases=[(name, "en"), (code, "und")],
            )


def _import_gs1(store: VocabularyStore) -> None:
    if not GS1_FILE.exists():
        return
    data = json.loads(GS1_FILE.read_text(encoding="utf-8"))
    for kind in ("business_steps", "dispositions"):
        for raw in data.get(kind) or ():
            name = str(raw).strip()
            if not name:
                continue
            spaced = name.replace("_", " ")
            _attach(
                store,
                domain="status",
                fallback_key=f"status.{_slug(name)}",
                canonical=fold(name) or name,
                source="GS1_CBV",
                aliases=[(name, "en"), (spaced, "en")],
            )


def _import_libpostal(store: VocabularyStore) -> None:
    if not LIBPOSTAL_FILE.exists():
        return
    data = json.loads(LIBPOSTAL_FILE.read_text(encoding="utf-8"))
    abbrevs = data.get("abbreviations") or {}
    for abbr, full in abbrevs.items():
        key = f"address.{_slug(str(full))}"
        cid = store.concept_id_by_key(key) or store.upsert_concept(
            key, "address", str(full), source="libpostal_dicts",
        )
        store.add_alias(cid, str(full), language="und", source="libpostal_dicts")
        store.add_alias(cid, str(abbr), language="und", source="libpostal_dicts")


def _apply_overrides(store: VocabularyStore) -> None:
    if not OVERRIDES_FILE.exists():
        return
    data = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    extra = data.get("aliases") or {}
    for domain, words in extra.items():
        if domain.startswith("_"):
            continue
        prefix = "package" if domain == "packaging" else domain
        for raw in words or ():
            text = str(raw).strip()
            if not text:
                continue
            existing = store.concept_id_by_alias(domain, text)
            if existing is not None:
                store.add_alias(existing, text, language="und", source="vepathos_overrides")
                continue
            if _is_standard_code(text):
                existing = store.concept_id_by_code(domain, text)
                if existing is not None:
                    store.add_alias(existing, text, language="und", source="vepathos_overrides")
                    continue
            cid = store.upsert_concept(
                f"{prefix}.override.{_slug(text)}",
                domain,
                fold(text) or text.lower(),
                source="vepathos_overrides",
            )
            store.add_alias(cid, text, language="und", source="vepathos_overrides")
