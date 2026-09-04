"""Deteccion de formato, encoding, delimiter y fila de header."""
import pytest

from smart_import.readers import read_any
from smart_import.readers.base import detect_format
from tests.conftest import FIXTURES


@pytest.mark.parametrize("name,expected", [
    ("ref_ar_orders.csv", "csv"),
    ("ref_ar_orders.xlsx", "xlsx"),
    ("ref_ar_orders.json", "json"),
    ("legacy.xls", "xls"),
    ("tabs.tsv", "tsv"),
    ("pipe_delimited.txt", "txt"),
])
def test_detecta_formato(name, expected):
    assert detect_format(FIXTURES / name) == expected


@pytest.mark.parametrize("name,delimiter", [
    ("ref_ar_orders.csv", ","),
    ("semicolon_latin1.csv", ";"),
    ("tabs.tsv", "\t"),
    ("pipe_delimited.txt", "|"),
])
def test_detecta_delimiter(name, delimiter):
    assert read_any(FIXTURES / name).meta.delimiter == delimiter


def test_detecta_encoding_no_utf8_y_decodifica_acentos():
    table = read_any(FIXTURES / "semicolon_latin1.csv")
    assert table.meta.encoding.lower() not in ("utf-8", "utf-8-sig")
    assert any("ó" in c or "ó" in str(c) for c in table.columns), table.columns


def test_salta_preambulo_y_toma_el_header_real():
    table = read_any(FIXTURES / "preamble_dirty.xlsx")
    assert table.meta.header_row > 0
    assert "Dir. entrega" in table.columns
    assert not any("REPORTE" in str(c) for c in table.columns)


def test_descarta_filas_totalmente_vacias():
    table = read_any(FIXTURES / "preamble_dirty.xlsx")
    assert all(any(v is not None for v in row) for row in table.rows)


def test_elige_hoja_deliveries_entre_varias():
    table = read_any(FIXTURES / "preamble_dirty.xlsx")
    assert len(table.meta.sheets) > 1
    assert table.meta.sheet == "Datos"


def test_json_anidado_se_aplana_a_una_fila_por_bulto():
    table = read_any(FIXTURES / "ref_ar_orders.json")
    assert len(table) == 19
    # los nombres hoja permiten que el JSON pase por el mismo mapper que un CSV
    for leaf in ("length", "width", "height", "start", "end", "time_zone"):
        assert leaf in table.columns
