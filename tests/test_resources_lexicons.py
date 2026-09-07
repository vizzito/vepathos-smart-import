"""Resources: lexicons viven en JSON, no en Python."""
from __future__ import annotations

from smart_import.geocoding.depot_context import normalize_locality_label
from smart_import.resources import (
    address_abbreviations,
    admin_unit_words,
    available_locales,
    country_bounds,
    country_label_from_slug,
    label_set,
    packaging_words,
    status_words,
)


def test_admin_unit_words_loaded_from_json():
    words = admin_unit_words()
    assert "comuna" in words
    assert "arrondissement" in words
    assert "barangay" in words
    assert "quận" in words
    assert isinstance(words, frozenset)


def test_numbered_admin_uses_resource_words():
    assert normalize_locality_label("Comuna 2") is None
    assert normalize_locality_label("11th Arrondissement") is None
    assert normalize_locality_label("Barangay 5") is None
    assert normalize_locality_label("Palermo") == "Palermo"


def test_country_bounds_json_cubre_aliases_globales():
    from smart_import.resources import coverage_bounds_for

    bounds = country_bounds()
    labels = country_label_from_slug()
    assert "argentina" in bounds and "india" in bounds and "japan" in bounds
    assert bounds["usa"] == bounds["united-states"] == bounds["us"]
    assert bounds["uk"] == bounds["united-kingdom"]
    assert labels["argentina"] == "Argentina"
    assert labels["india"] == "India"
    assert labels["usa"] == "United States"
    miami = coverage_bounds_for(
        "florida",
        "/data/north-america_tile_x/us_tile_y/florida-pyrosm.osm.pbf",
    )
    assert miami is not None
    n, s, e, w = miami
    assert s <= 25.76 <= n and w <= -80.19 <= e


def test_labels_and_packaging_vocab():
    assert set(available_locales()) >= {"es", "en", "pt"}
    assert "avenida" in label_set("street_tokens", ("es",))
    assert "caja" in packaging_words()
    assert "pending" in status_words()
    assert address_abbreviations()["av"] == "avenida"


def test_locality_expand_merges_geonames_when_present():
    from pathlib import Path
    from smart_import.resources import locality_expansions, load_json, _DIR

    load_json.cache_clear()
    locality_expansions.cache_clear()
    geo = _DIR / "locality_expand_geonames.json"
    exp = locality_expansions()
    assert any("caba" in cues for cues, _ in exp)  # curado
    if geo.exists():
        assert len(exp) > 100
        assert any("mumbai" in cues or "paris" in cues for cues, _ in exp)


# ---------- lo que se aprendio validando el refactor ----------

def test_las_palabras_de_admin_matchean_con_y_sin_diacriticos():
    """Los exports reales llegan 'Quận 1' y 'Quan 1'. Los dos son subdivisiones."""
    from smart_import.geocoding.depot_context import normalize_locality_label

    for con, sin in (("Quận 1", "Quan 1"), ("ilçe 5", "ilce 5"),
                     ("Região 2", "Regiao 2"), ("Município 9", "Municipio 9"),
                     ("Alcaldía 3", "Alcaldia 3")):
        assert normalize_locality_label(con) is None, con
        assert normalize_locality_label(sin) is None, sin


def test_una_localidad_de_verdad_sobrevive():
    from smart_import.geocoding.depot_context import normalize_locality_label

    for nombre in ("Palermo", "Villa Crespo", "São Paulo",
                   "Ciudad Autónoma de Buenos Aires"):
        assert normalize_locality_label(nombre) == nombre


def test_los_conectores_de_nombre_no_estan_duplicados_en_python():
    """`customer_name` compila su regex desde resources, no desde una tupla propia."""
    import inspect

    from smart_import.extraction import customer_name
    from smart_import.resources import name_glue_words

    fuente = inspect.getsource(customer_name)
    assert "name_glue_words" in fuente
    assert "van|von" not in fuente and "de|del|la|las" not in fuente
    assert {"de", "van", "von", "bin"} <= set(name_glue_words())


def test_un_nombre_con_dos_conectores_se_extrae_entero():
    from smart_import.extraction.context import ExtractionContext
    from smart_import.extraction.pipeline import FieldExtractionPipeline

    pipeline = FieldExtractionPipeline(context=ExtractionContext(phone_region="AR"))
    casos = {
        "Entregar a Maria de los Angeles Perez en Av. Corrientes 100":
            "Maria de los Angeles Perez",
        "Para Jan van der Berg el paquete va a Av. Cabildo 174": "Jan van der Berg",
        "CONTACTO: Ahmed bin Salem. LUGAR: Av. Santa Fe 137": "Ahmed bin Salem",
    }
    for texto, esperado in casos.items():
        assert pipeline.run(texto).get("customer_name") == esperado


def test_agregar_una_palabra_al_json_cambia_el_comportamiento(tmp_path, monkeypatch):
    """El contrato del refactor: editar datos, no codigo."""
    import json

    from smart_import import resources
    from smart_import.resources import GEO_KEYWORDS_FILE

    datos = json.loads(GEO_KEYWORDS_FILE.read_text(encoding="utf-8"))
    datos["admin_unit_words"] = [*datos["admin_unit_words"], "zorbulandia"]
    destino = tmp_path / "geo_keywords.json"
    destino.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")

    # `load_json` es el unico cache de la cadena; el resto lee a traves suyo.
    # En produccion el override se toma al arrancar el proceso, no en caliente.
    monkeypatch.setenv("SMART_IMPORT_GEO_KEYWORDS_PATH", str(destino))
    resources.load_json.cache_clear()
    try:
        assert "zorbulandia" in resources.admin_unit_words()
    finally:
        monkeypatch.delenv("SMART_IMPORT_GEO_KEYWORDS_PATH", raising=False)
        resources.load_json.cache_clear()
    assert "zorbulandia" not in resources.admin_unit_words()
