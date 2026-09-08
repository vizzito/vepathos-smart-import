"""Tres niveles de evidencia para el nombre, y lo que NO puede pasar por nombre.

El riesgo de la regla en minusculas es exactamente uno: convertir una nota
('dejar en porteria') o un destino ('entregar a palermo') en un cliente. La
mitad de abajo son esos casos.
"""
import pytest

from smart_import.extraction.canvas import TextCanvas
from smart_import.extraction.customer_name import (
    CONF_CAPITALIZED, CONF_LOWERCASE, CONF_PREFIX, extract_customer_name,
)


def name_of(text, context):
    return extract_customer_name(TextCanvas(text), context)


# ---------- (1) capitalizacion, en cualquier alfabeto ----------

@pytest.mark.parametrize("text, expected", [
    ("Ana Perez vive en Corrientes 100", "Ana Perez"),
    ("María Gómez, Av Cabildo 174", "María Gómez"),
    ("Maria de los Angeles Perez, Callao 100", "Maria de los Angeles Perez"),
    # Estos cuatro se perdian con `[A-ZÁÉÍÓÚÜÑ]`: el alfabeto castellano no
    # alcanza para una lista de entregas de verdad.
    ("Ângela Sousa, Rua Augusta 1500", "Ângela Sousa"),
    ("Öztürk Demir, Istiklal Caddesi 12", "Öztürk Demir"),
    ("Łukasz Nowak, Krucza 5", "Łukasz Nowak"),
    ("Đorđe Petrovic, Knez Mihailova 10", "Đorđe Petrovic"),
])
def test_nombres_capitalizados(ar_context, text, expected):
    found = name_of(text, ar_context)
    assert found is not None and found.value == expected
    assert found.confidence == CONF_CAPITALIZED


def test_prefijo_explicito_gana_confianza(ar_context):
    found = name_of("Para Maria Gomez, Av Corrientes 100", ar_context)
    assert found.value == "Maria Gomez"
    assert found.confidence == CONF_PREFIX
    assert "Para" not in found.value, "el prefijo es etiqueta, no parte del nombre"


def test_una_coma_corta_la_racha(ar_context):
    """'Ana Perez, Juan Lopez' son dos nombres; no se pegan en uno."""
    found = name_of("Ana Perez, Juan Lopez", ar_context)
    assert found.value == "Ana Perez"


# ---------- (2) sin mayusculas: estructura en vez de lexico ----------

@pytest.mark.parametrize("text, expected", [
    ("martin vizzolini, av santa fe 890, palermo, ba", "martin vizzolini"),
    ("juan lopez | av santa fe 137 | 11 4000-1000", "juan lopez"),
    ("ana maria perez, av corrientes 100, caba", "ana maria perez"),
])
def test_primer_campo_en_minuscula(ar_context, text, expected):
    found = name_of(text, ar_context)
    assert found is not None and found.value == expected
    assert found.confidence == CONF_LOWERCASE
    assert found.method == "first_field"


def test_la_minuscula_pide_separadores(ar_context):
    """Una frase suelta no tiene campos: sin separador no hay candidato."""
    assert name_of("martin vizzolini av santa fe 890", ar_context) is None


@pytest.mark.parametrize("text", [
    "dejar en porteria, av corrientes 100",
    "entregar a palermo, av santa fe 890",
    "llamar al 11-4000-1000 para coordinar",
    "hola buenas, av corrientes 100",
    "av santa fe 890, palermo",
    "gracias equipo, av corrientes 100",
    "2 bultos 5 kg, av corrientes 100",
])
def test_lo_que_no_es_un_nombre(ar_context, text):
    found = name_of(text, ar_context)
    assert found is None or found.confidence >= CONF_CAPITALIZED, \
        f"{text!r} produjo el nombre {found.value!r}"


def test_la_capitalizada_le_gana_a_la_posicional(ar_context):
    """Si hay mayusculas en algun lado, esa evidencia manda."""
    found = name_of("av santa fe 890, Ana Perez, palermo", ar_context)
    assert found.value == "Ana Perez"
    assert found.confidence == CONF_CAPITALIZED


# ---------- limitacion conocida: el nombre despues de la direccion ----------

def test_el_nombre_va_antes_de_la_direccion(ar_context):
    """El orden del peel decide: `address` corre ANTES que `customer_name`.

    Si el nombre viene primero, el extractor de direcciones toma solo la calle
    y el nombre queda libre. Si viene despues, la direccion se lo lleva puesto
    y no queda nada para el paso siguiente.

    Este test FIJA la limitacion, no la celebra. Medido sobre el corpus: 0 de
    581 entregas de texto libre la sufren, porque la gente escribe el nombre
    primero y porque el caso que si la generaba —un CSV pegado como texto— hoy
    lo resuelve la capa de bloques embebidos antes de llegar aca.
    """
    from smart_import.config import Config
    from smart_import.extraction.free_text import FreeTextExtractor

    ex = FreeTextExtractor(Config.from_env(), ar_context)

    antes = ex.run_value("Martin Diaz, Lavalle 3690, 1160715969")
    assert antes.get("customer_name") == "Martin Diaz"
    assert "Martin" not in (antes.get("address") or "")

    despues = ex.run_value("Lavalle 3690, Martin Diaz, 1160715969")
    assert despues.get("customer_name") is None, \
        "si algun dia esto deja de ser None, la limitacion se arreglo: " \
        "revisar que la direccion no pierda la localidad de un pueblo chico"

    # Con etiqueta explicita funciona en cualquier orden.
    etiquetado = ex.run_value("Lavalle 3690, Cliente: Martin Diaz")
    assert etiquetado.get("customer_name") == "Martin Diaz"
