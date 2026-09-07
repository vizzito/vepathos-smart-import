"""Deteccion de schema: aliases, fuzzy y heuristicas de contenido."""
import math

import pytest

from smart_import.mapping import RuleSchemaMapper
from smart_import.mapping.heuristics import ColumnProfile, _to_float
from smart_import.pipeline import run_normalize
from smart_import.readers import read_any
from tests.conftest import FIXTURES, SCHEMA


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
    from smart_import.config import Config
    from smart_import.mapping import build_mapper
    result = build_mapper(Config.from_env()).detect(read_any(FIXTURES / "es_headers_raros.xlsx"), schema)
    assert not result.ai_used
    assert len(result.mapping) >= 7


def test_planilla_garrafas_mapea_direccion_sin_header(schema, tmp_path):
    """Reparto local: columna A vacia, B = calle (a veces sin altura), resto = SKUs.

    Antes: Nylon~lon y Chañ~cant robaban targets; col_2 quedaba sin mapear y
    se descartaban casi todas las filas.
    """
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "19-8"
    ws.append([None, "Valor Mayorista", 4000])
    ws.append([None, "Valor general", 4150])
    ws.append([None, None, "X5kg", "X 4kg", "Nylon", "Chañ", "Garrafas", "Pagó"])
    rows = [
        [None, "Alsina y Pelegrini", None, None, None, None, None, None],
        [None, "Avellaneda 1725", 15, None, None, None, None, 164250],
        [None, "Punto carne Moreno", 40, None, None, None, None, None],
        [None, "Las heras 1475 (punto c moreno)", None, None, None, None, None, 10400],
        [None, "Roca 1160", 8, None, None, None, None, 33200],
        [None, "san martin 1322", None, None, 5, None, None, 34400],
        [None, "Garibaldi y Montiel", 4, None, None, None, None, 31000],
        [None, "Sarmiento 1382", 5, None, None, None, None, None],
    ]
    for r in rows:
        ws.append(r)
    path = tmp_path / "recorrido_sprinter.xlsx"
    wb.save(path)

    result = RuleSchemaMapper().detect(read_any(path), schema)
    by_target = {m.target: m for m in result.mapping.values()}
    assert "address" in by_target, result.mapping
    assert by_target["address"].column == "col_2"
    # Productos no deben robar lng / quantity por fuzzy
    mapped_names = {m.column: m.target for m in result.mapping.values()}
    assert mapped_names.get("Nylon") != "lng"
    assert mapped_names.get("Chañ") != "quantity"


def test_fuzzy_no_mapea_nylon_a_lng_ni_chan_a_cantidad(schema):
    from smart_import.mapping import fuzzy

    assert fuzzy.candidates("Nylon", schema) == []
    assert not any(t == "quantity" for t, *_ in fuzzy.candidates("Chañ", schema))


def test_to_float_rechaza_nan_e_infinity():
    assert _to_float(float("nan")) is None
    assert _to_float(float("inf")) is None
    assert _to_float(float("-inf")) is None
    assert _to_float("NaN") is None
    assert _to_float("Infinity") is None
    assert _to_float(42.5) == 42.5
    assert _to_float(0) == 0.0


def test_column_profile_no_rompe_con_nan():
    """Regresion DBO1: weight/lat con NaN → ValueError int(NaN) en detect."""
    profile = ColumnProfile([1.0, float("nan"), 2.5, float("inf"), None, ""])
    assert profile.nums == [1.0, 2.5]
    assert profile.has_decimals is True
    assert profile.lo == 1.0
    assert profile.hi == 2.5


def test_json_con_nan_no_tira_422_en_mapping(tmp_path):
    """addresses_aws_DBO1 con NaN en coords/dimensiones no debe tumbar normalize."""
    result = run_normalize(
        FIXTURES / "boston_nan_coords.json",
        SCHEMA,
        tmp_path / "out.csv",
        emit=("flat", "nested"),
    )
    assert (result.report.get("deliveries") or 0) >= 1
    assert result.outputs.get("flat")


# ---------- 'altura' es dos cosas distintas en español ----------

def _mapear(tmp_path, cabecera: str, fila: str):
    from smart_import.config import Config
    from smart_import.mapping import build_mapper
    from smart_import.readers import read_any
    from smart_import.schemas import TargetSchema
    from tests.conftest import SCHEMA

    csv = tmp_path / "in.csv"
    csv.write_text(f"{cabecera}\n{fila}\n", encoding="utf-8")
    tabla = read_any(csv)
    resultado = build_mapper(Config.from_env()).detect(tabla, TargetSchema.load(SCHEMA))
    return {c: m.target for c, m in resultado.mapping.items()}


@pytest.mark.parametrize("caso,cabecera,fila,columna,esperado", [
    # 'Altura': alto del bulto Y numero de puerta (AR/LatAm)
    ("altura=puerta", "Pedido,Calle,Altura,Ciudad,Cliente",
     "P-1,Av. Cabildo,2450,CABA,Ana", "Altura", "house_number"),
    ("altura=alto", "Pedido,Direccion,Largo,Ancho,Altura,Peso",
     "P-2,Av. Cabildo 2450,30,20,15,3.5", "Altura", "height_cm"),
    # 'Long': longitud Y largo
    ("long=longitud", "Direccion,Lat,Long,Cliente",
     "Av. Cabildo 2450,-34.56,-58.45,Ana", "Long", "lng"),
    ("long=largo", "Direccion,Long,Ancho,Peso",
     "Av. Cabildo 2450,30,20,3.5", "Long", "length_cm"),
    # 'Departamento': provincia (UY/CO/PE) Y depto de un edificio (AR)
    ("depto=unidad", "Calle,Altura,Departamento,Ciudad",
     "Av. Cabildo,2450,3B,CABA", "Departamento", "unit"),
    ("depto=provincia", "Direccion,Ciudad,Departamento,Pais",
     "Av. Cabildo 2450,Montevideo,Canelones,UY", "Departamento", "region"),
    # 'Estado': provincia (BR/MX) Y estado del envio
    ("estado=provincia", "Direccion,Ciudad,Estado,Pais",
     "Rua Augusta 1500,Sao Paulo,SP,BR", "Estado", "region"),
    ("estado=status", "Direccion,Bulto,Peso,Estado",
     "Av. Cabildo 2450,K1,3.5,ENTREGADO", "Estado", "status"),
])
def test_un_alias_ambiguo_lo_resuelve_el_contexto(tmp_path, caso, cabecera, fila,
                                                  columna, esperado):
    """El nombre de columna es identico y el contenido tambien es un numero.

    Lo unico que distingue 'Altura 2450' (puerta) de 'Altura 15' (alto) es que
    otras columnas trae el archivo.
    """
    assert _mapear(tmp_path, cabecera, fila)[columna] == esperado, caso


def test_las_reglas_de_ambiguedad_salen_del_json():
    """Sumar un caso tiene que ser editar datos, no codigo."""
    from smart_import.resources import ambiguous_aliases

    reglas = ambiguous_aliases()
    assert reglas, "no se cargo vepathos_overrides.json"
    for regla in reglas:
        assert regla["alias"] and regla["prefer"]
        assert regla["when_any"] or regla["when_none"]


def test_si_ya_hay_columna_de_puerta_no_se_toca_altura(tmp_path):
    mapeo = _mapear(tmp_path, "Calle,Nro puerta,Altura,Ancho",
                    "Av. Cabildo,2450,15,20")
    assert mapeo["Nro puerta"] == "house_number"
    assert mapeo["Altura"] == "height_cm"


def test_la_correccion_queda_avisada_para_que_el_usuario_la_revierta(tmp_path):
    from smart_import.config import Config
    from smart_import.mapping import build_mapper
    from smart_import.readers import read_any
    from smart_import.schemas import TargetSchema
    from tests.conftest import SCHEMA

    csv = tmp_path / "in.csv"
    csv.write_text("Calle,Altura,Ciudad\nAv. Cabildo,2450,CABA\n", encoding="utf-8")
    resultado = build_mapper(Config.from_env()).detect(
        read_any(csv), TargetSchema.load(SCHEMA))
    assert any("house_number" in w for w in resultado.warnings)
