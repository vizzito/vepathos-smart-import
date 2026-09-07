"""TXT tabular vs texto libre.

El bug que originó el refactor: las comas del lenguaje natural convertian un
paste de WhatsApp en un CSV de 2 columnas, y la primera entrega se perdia tomada
como header.
"""
import pytest

from smart_import.detection.text_mode import (
    TextMode, classify_text_mode, header_score, is_prose_line,
)
from smart_import.readers import read_any
from tests.conftest import EXAMPLES


def test_el_paste_de_whatsapp_no_es_una_tabla(free_text):
    decision = classify_text_mode(free_text["whatsapp_12"], fmt="txt")
    assert decision.mode is TextMode.FREE_TEXT
    assert decision.delimiter is None
    assert decision.reasons


def test_tampoco_lo_es_sin_viñetas(free_text):
    """La deteccion no puede depender de que alguien haya puesto guiones."""
    decision = classify_text_mode(free_text["whatsapp_12_nobullets"], fmt="txt")
    assert decision.mode is TextMode.FREE_TEXT


def test_una_coma_repetida_no_alcanza_para_declarar_tabla():
    prosa = (
        "Hola, buenas tardes, les dejo el pedido\n"
        "Necesito que pasen por casa, toquen timbre, y dejen el paquete\n"
        "Si no estoy, lo puede recibir el portero, se llama Juan\n"
        "Gracias, saludos\n"
    )
    decision = classify_text_mode(prosa, fmt="txt")
    assert decision.mode is TextMode.FREE_TEXT


@pytest.mark.parametrize("name", [
    "pipe_delimited.txt", "tabs.tsv", "en_weird.csv",
    "semicolon_latin1.csv", "no_headers.csv", "merged_field.csv",
])
def test_los_archivos_realmente_tabulares_siguen_siendo_tabulares(name):
    text = (EXAMPLES / name).read_text(encoding="utf-8", errors="replace")
    decision = classify_text_mode(text, fmt="txt")
    assert decision.mode is TextMode.TABULAR, decision.reasons
    assert decision.consistency >= 0.80


def test_el_lector_preserva_el_documento_entero(free_text):
    table = read_any(EXAMPLES / "free-text" / "whatsapp_12.txt")
    assert table.meta.text_mode == "free_text"
    assert table.meta.delimiter is None
    assert table.meta.header_row == 0
    assert table.meta.preamble_rows == 0
    assert table.columns == ["document"]
    assert len(table) == 1
    assert table.rows[0][0] == free_text["whatsapp_12"]


def test_ana_perez_no_se_pierde_como_header(free_text):
    """Antes el reader elegia header_row=2 y se comia la primera entrega."""
    table = read_any(EXAMPLES / "free-text" / "whatsapp_12.txt")
    assert "Ana Perez" in table.rows[0][0]


@pytest.mark.parametrize("line,prose", [
    ("- Ana Perez vive en Av. Corrientes 100", True),
    ("1) Juan Lopez", True),
    ("Hola chicos!", True),
    ("Salutos,", True),
    ("Despacho", True),
    ("direccion,cliente,telefono", False),
    ("Av. Corrientes 100,Ana Perez,1140001000", False),
])
def test_reconoce_la_forma_de_una_linea_de_prosa(line, prose):
    assert is_prose_line(line) is prose


def test_un_header_de_verdad_puntua_alto():
    assert header_score(["direccion", "cliente", "telefono"]) >= 0.9
    assert header_score(["Hola chicos! Dejo el listado"]) == 0.0
    assert header_score(["1", "2", "3"]) < 0.5


def test_la_decision_explica_por_que(free_text):
    decision = classify_text_mode(free_text["whatsapp_12"], fmt="txt")
    payload = decision.as_dict()
    assert payload["mode"] == "free_text"
    assert payload["reasons"]
