"""VocabularyStore: catálogo conceptual, UNECE/GS1/libpostal, anti-FP de códigos."""
from __future__ import annotations

from smart_import.mapping.heuristics import candidates
from smart_import.resources import (
    ambiguous_aliases,
    packaging_codes,
    packaging_words,
    status_words,
)
from smart_import.vocab.builder import build
from smart_import.vocab.store import reset_store_cache


def test_catalog_groups_aliases_on_one_concept():
    store = build(dest=None, catalog=True, unece=False, gs1=False,
                  libpostal=False, overrides=False)
    for alias in ("box", "caja", "cajas", "boîte", "BX", "bx"):
        hit = store.resolve(alias, "packaging")
        assert hit is not None, alias
        assert hit["key"] == "package.box"
        assert hit["canonical"] == "box"
        assert hit["code"] == "BX"
    assert store.resolve("pendiente", "status")["key"] == "status.pending"
    store.close()


def test_runtime_sin_sqlite_usa_catalogo(monkeypatch, tmp_path):
    monkeypatch.setenv("SMART_IMPORT_VOCAB_PATH", str(tmp_path / "missing.sqlite"))
    reset_store_cache()
    try:
        assert "caja" in packaging_words()
        assert "pending" in status_words()
        assert "bx" in packaging_codes()
    finally:
        reset_store_cache()


def test_unece_bx_se_fusiona_con_package_box():
    store = build(dest=None, catalog=True, unece=True, gs1=False,
                  libpostal=False, overrides=False)
    assert store.resolve("BX", "packaging")["key"] == "package.box"
    assert "bx" in store.codes("packaging")
    stats = store.stats()
    assert stats["packaging_concepts"] >= 40
    store.close()


def test_gs1_status_y_alias_vepathos():
    store = build(dest=None, catalog=True, unece=False, gs1=True,
                  libpostal=False, overrides=True)
    assert store.resolve("in_transit", "status") is not None
    assert store.resolve("entregado", "status")["key"] == "status.delivered"
    assert store.resolve("en reparto", "status")["key"] == "status.out_for_delivery"
    assert store.resolve("cargadas", "packaging") is not None
    store.close()


def test_libpostal_dicts_abreviaturas():
    store = build(dest=None, catalog=False, unece=False, gs1=False,
                  libpostal=True, overrides=False)
    expand = store.address_expand()
    assert expand.get("st") == "street"
    assert expand.get("av") == "avenida"
    assert expand.get("r.") == "rua"
    store.close()


def test_ambiguous_rules_siguen_en_overrides():
    rules = ambiguous_aliases()
    aliases = {a for rule in rules for a in rule.get("alias") or []}
    assert {"altura", "alt"} <= aliases
    assert "departamento" in aliases
    assert "estado" in aliases
    assert "long" in aliases
    prefers = {rule["prefer"] for rule in rules}
    assert {"house_number", "unit", "length_cm", "region"} <= prefers


def test_columna_bx_es_packaging():
    targets = {t for t, *_ in candidates(["BX", "BG", "CT", "BX", "BG"])}
    assert "packaging" in targets


def test_buenos_aires_no_es_packaging_por_ba():
    targets = {t for t, *_ in candidates(
        ["Buenos Aires", "Buenos Aires", "Córdoba", "Rosario", "Mendoza"]
    )}
    assert "packaging" not in targets


def test_romero_no_es_packaging_por_ro():
    targets = {t for t, *_ in candidates(
        ["Romero", "Romero", "García", "Pérez", "López"]
    )}
    assert "packaging" not in targets


def test_store_se_puede_leer_desde_otro_thread():
    """Starlette corre POST /imports en un threadpool. El store se abre antes
    en el thread principal (parse/mapping). Sin eso, packaging_codes() tira
    ProgrammingError y el import responde 422 sin job_id — no es el AND/OR.
    """
    import threading

    from smart_import.resources import packaging_codes
    from smart_import.vocab.store import get_store

    get_store()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            assert packaging_codes()
        except BaseException as exc:  # noqa: BLE001 — lo reassertimos abajo
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert errors == [], errors


def test_cli_setup_y_stats(tmp_path):
    dest = tmp_path / "vocab.sqlite"
    store = build(dest, catalog=True, unece=True, gs1=True, libpostal=True)
    stats = store.stats()
    store.close()
    assert stats["concepts"] >= 100
    assert stats["aliases"] >= 200
    from smart_import.vocab.__main__ import main
    assert main(["stats", "--db", str(dest)]) == 0
    assert main(["setup", "--out", str(tmp_path / "vocab2.sqlite")]) == 0
