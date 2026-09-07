"""Dataset de regresion: el paste de WhatsApp, de punta a punta.

Es el caso que motivo el refactor. Si esto se rompe, se rompio lo importante.
"""
from datetime import date

import pytest

from smart_import.pipeline import run_normalize
from tests.conftest import FREE_TEXT, SCHEMA

DAY = date(2026, 9, 6)


@pytest.fixture(scope="module")
def resultado():
    return run_normalize(FREE_TEXT / "whatsapp_12.txt", SCHEMA,
                         phone_region="AR", service_date=DAY)


@pytest.fixture(scope="module")
def sin_viñetas():
    return run_normalize(FREE_TEXT / "whatsapp_12_nobullets.txt", SCHEMA,
                         phone_region="AR", service_date=DAY)


def test_el_archivo_se_lee_como_texto_libre(resultado):
    assert resultado.report["text_mode"] == "free_text"
    assert resultado.report["input"]["delimiter"] is None
    assert resultado.report["input"]["header_row"] == 0


def test_son_doce_entregas(resultado):
    assert resultado.report["deliveries"] == 12
    assert resultado.report["extraction"]["deliveries"] == 12


def test_el_preambulo_y_la_despedida_se_ignoran(resultado):
    extraction = resultado.report["extraction"]
    assert extraction["ignored"] == 3
    ignorados = " | ".join(i["text"] for i in extraction["ignored_samples"])
    assert "Hola chicos" in ignorados
    assert "Salutos" in ignorados
    assert "Despacho" in ignorados


def test_salutos_y_despacho_nunca_son_una_direccion(resultado):
    for delivery in resultado.deliveries:
        address = (delivery.get("address") or "")
        assert "Salutos" not in address
        assert "Despacho" not in address
        assert "Hola chicos" not in address


def test_estan_los_doce_nombres(resultado, expected_deliveries):
    extraidos = [d.get("customer_name") for d in resultado.deliveries]
    esperados = [r["customer_name"] for r in expected_deliveries["records"]]
    assert extraidos == esperados


def test_ana_perez_no_se_pierde(resultado):
    """Antes desaparecia: el reader la tomaba como fila de header."""
    assert "Ana Perez" in [d.get("customer_name") for d in resultado.deliveries]


def test_las_direcciones_son_correctas(resultado, expected_deliveries):
    for delivery, esperado in zip(resultado.deliveries, expected_deliveries["records"]):
        assert esperado["address_contains"] in (delivery.get("address") or "")


def test_los_telefonos_quedan_en_e164(resultado, expected_deliveries):
    for delivery, esperado in zip(resultado.deliveries, expected_deliveries["records"]):
        assert delivery.get("phone") == esperado["phone"]


def test_las_ventanas_horarias_conocidas(resultado, expected_deliveries):
    """En la forma anidada la ventana vive en `time_window`, no suelta."""
    for delivery, esperado in zip(resultado.deliveries, expected_deliveries["records"]):
        hora = esperado["tw_end_hour"]
        window = delivery.get("time_window")
        if hora is None:
            continue
        assert window, delivery.get("customer_name")
        assert window["end"].endswith(f"{hora:02d}:00"), delivery.get("customer_name")


def test_el_texto_horario_original_se_conserva(resultado):
    """`delivery_time_text` guarda lo que el cliente escribio, sin interpretar."""
    fields = resultado.report["extraction"]["fields"]
    assert fields["delivery_time_text"]["detected"] >= 4
    assert fields["tw_end"]["detected"] == fields["tw_start"]["detected"]


def test_cada_entrega_tiene_un_id_generado_por_codigo(resultado):
    ids = [d.get("delivery_id") for d in resultado.deliveries]
    assert ids == [f"{i:03d}" for i in range(1, 13)]


def test_no_se_hizo_ni_una_llamada_a_modelo(resultado):
    assert resultado.report["ai_calls"] == 0
    assert resultado.report["extraction"]["ai_calls"] == 0


def test_el_report_trae_confianza_por_campo(resultado):
    fields = resultado.report["extraction"]["fields"]
    assert fields["address"]["detected"] == 12
    assert fields["phone"]["high_confidence"] == 12


def test_ninguna_entrega_queda_invalida(resultado):
    assert resultado.report["invalid_rows"] == 0
    assert resultado.report["ignored_rows"] == 0


def test_el_mismo_documento_sin_viñetas_da_el_mismo_resultado(sin_viñetas):
    assert sin_viñetas.report["text_mode"] == "free_text"
    assert sin_viñetas.report["deliveries"] == 12
    nombres = [d.get("customer_name") for d in sin_viñetas.deliveries]
    assert "Ana Perez" in nombres and "Nicolas Diaz" in nombres
    assert all("Salutos" not in (d.get("address") or "") for d in sin_viñetas.deliveries)


def test_es_practicamente_instantaneo(resultado):
    """12 registros con reglas: milisegundos, no decenas de segundos."""
    assert resultado.report["extraction"]["processing_time_ms"] < 500
    assert resultado.report["processing_times"]["total"] < 1.0
