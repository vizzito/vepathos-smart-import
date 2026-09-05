"""Deteccion de schema: aliases, fuzzy y heuristicas de contenido."""
import pytest

from smart_import.mapping import RuleSchemaMapper
from smart_import.readers import read_any
from tests.conftest import FIXTURES


def detect(name, schema):
    return RuleSchemaMapper().detect(read_any(FIXTURES / name), schema)


@pytest.mark.parametrize("name", [
    "ref_ar_orders.csv", "ref_ar_orders.xlsx", "ref_ar_orders.json", "ref_us_seattle.xlsx",
])
def test_archivos_de_referencia_no_necesitan_revision(name, schema):
    result = detect(name, schema)
    assert not result.needs_review, result.ambiguous
    assert not result.unmapped, result.unmapped


@pytest.mark.parametrize("name,expected", [
    ("es_headers_raros.xlsx", {"Dest.": "address", "Receptor": "customer_name",
                               "Contacto 1": "phone", "Kg": "weight_kg",
                               "Cant bultos": "quantity", "Latitud": "lat", "Longitud": "lng"}),
    ("en_weird.csv", {"Ship To Address": "address", "Recipient": "customer_name",
                      "Mobile": "phone", "Pkg Count": "quantity",
                      "Latitude": "lat", "Longitude": "lng"}),
    ("preamble_dirty.xlsx", {"Dir. entrega": "address", "Nom Dest": "customer_name",
                             "Tel dest": "phone", "Cant.": "quantity"}),
    ("one_row_per_delivery.xlsx", {"cantidad de bultos": "quantity", "cliente": "customer_name"}),
])
def test_headers_raros_se_mapean_por_nombre(name, expected, schema):
    mapping = detect(name, schema).mapping
    for column, target in expected.items():
        assert column in mapping, f"{column} no fue mapeada"
        assert mapping[column].target == target, f"{column} -> {mapping[column].target}"


def test_sin_headers_utiles_decide_el_contenido(schema):
    """'Campo 1..6' no dice nada: el mapeo sale de los valores."""
    result = detect("no_headers.csv", schema)
    targets = {m.target for m in result.mapping.values()}
    assert {"address", "lat", "lng", "phone"} <= targets
    assert all(m.method == "heuristic" for m in result.mapping.values())
    assert result.needs_review          # contenido solo => no se auto-acepta


def test_una_fila_corrupta_no_reclasifica_la_columna(schema):
    """VP-1999 trae lat=95: no puede convertir la columna lat en lng."""
    mapping = detect("ref_ar_orders.csv", schema).mapping
    assert mapping["lat"].target == "lat"
    assert mapping["lng"].target == "lng"


def test_columna_que_mezcla_campos_va_a_revision(schema):
    result = detect("merged_field.csv", schema)
    assert result.needs_review
    assert result.mapping["Datos entrega"].confidence < 0.90
    assert any("varios campos" in w for w in result.warnings)


def test_columna_sin_correspondencia_queda_sin_mapear(schema):
    assert "Sucursal origen" in detect("preamble_dirty.xlsx", schema).unmapped


def test_un_target_no_se_asigna_a_dos_columnas(schema):
    for name in ("es_headers_raros.xlsx", "en_weird.csv", "ref_ar_orders.json"):
        targets = [m.target for m in detect(name, schema).mapping.values()]
        assert len(targets) == len(set(targets)), name


def test_funciona_con_ia_deshabilitada(monkeypatch, schema):
    monkeypatch.setenv("SMART_IMPORT_AI_ENABLED", "false")
    from smart_import.config import Config
    from smart_import.mapping import build_mapper
    result = build_mapper(Config.from_env()).detect(read_any(FIXTURES / "es_headers_raros.xlsx"), schema)
    assert not result.ai_used
    assert len(result.mapping) >= 7


def test_una_fila_corrupta_en_muestra_chica_tampoco_invierte_lat_lng(schema, tmp_path):
    """Con 4 filas, una corrupta es el 33%: un umbral porcentual fijo daba vuelta
    la columna entera y lat/lng salian intercambiadas."""
    from smart_import.mapping import RuleSchemaMapper
    from smart_import.readers import read_any

    archivo = tmp_path / "chico.csv"
    archivo.write_text(
        "Dir. entrega;Latitud;Longitud\n"
        "Av. Corrientes 1234;-34.6037;-58.3816\n"
        "Maipu 400;-34.5921;-58.3745\n"
        "Cabildo 1800;-34.5610;-58.4560\n"
        "Florida 500;95;200\n",              # fila deliberadamente corrupta
        encoding="utf-8")

    mapping = RuleSchemaMapper().detect(read_any(archivo), schema).mapping
    assert mapping["Latitud"].target == "lat"
    assert mapping["Longitud"].target == "lng"
