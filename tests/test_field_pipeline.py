"""Extraccion progresiva campo por campo. Cero llamadas a modelo."""
from datetime import date

import pytest

from smart_import.extraction.canvas import TextCanvas
from smart_import.extraction.labels import find_labels
from smart_import.extraction.packages import extract_packages
from smart_import.extraction.phone_extract import find_phones
from smart_import.extraction.pipeline import FieldExtractionPipeline
from smart_import.extraction.time_window import find_time_expression, to_window

DAY = date(2026, 9, 6)


@pytest.fixture
def pipeline(ar_context):
    return FieldExtractionPipeline(context=ar_context)


# ---------- telefonos ----------

@pytest.mark.parametrize("texto,e164", [
    ("el cel de ella es el 11-4000-1000. Dijo que", "+541140001000"),
    ("Juan Lopez (1140011001) entrega en Palermo", "+541140011001"),
    ("Martin Castro (11 4007 1007) - Av. Pueyrredon 359", "+541140071007"),
    ("TE: +54 9 11 4003 1003.", "+5491140031003"),
    ("Nicolas Diaz (contact: 11-4054-1054) -", "+541140541054"),
])
def test_telefonos_con_libphonenumber(texto, e164):
    hits = find_phones(texto, ("AR",))
    assert hits and hits[0].e164 == e164
    assert hits[0].valid
    assert texto[hits[0].span[0]:hits[0].span[1]] == hits[0].raw   # span exacto


@pytest.mark.parametrize("texto", [
    "entregar antes de las 14hs", "Mumbai 400069", "Caja 20x30x40 cm, 8 kg",
    "Salutos", "Av. Cordoba 248 piso 3 B",
])
def test_no_confunde_horarios_cp_ni_medidas_con_telefonos(texto):
    assert not [h for h in find_phones(texto, ("AR",)) if h.valid]


# ---------- etiquetas explicitas ----------

def test_las_etiquetas_cortan_donde_empieza_la_siguiente():
    texto = ("Direccion: Av. Rivadavia 211 (Caballito / CABA) -- "
             "Destinatario: Carlos Ruiz -- TE: +54 9 11 4003 1003.")
    found = {h.field: h.value for h in find_labels(texto)}
    assert found["address"] == "Av. Rivadavia 211 (Caballito / CABA)"
    assert found["customer_name"] == "Carlos Ruiz"
    assert found["phone"] == "+54 9 11 4003 1003"


def test_una_etiqueta_dentro_de_parentesis_no_se_lleva_el_resto():
    texto = "Nicolas Diaz (contact: 11-4054-1054) - Av. Elcano 2098 esquina Superi"
    found = {h.field: h.value for h in find_labels(texto)}
    assert found.get("customer_name") in (None, "11-4054-1054")
    assert "Av. Elcano" not in (found.get("customer_name") or "")


# ---------- ventanas horarias ----------

@pytest.mark.parametrize("texto,hora", [
    ("solo recibe si es antes de las 14hs!!", 14),
    ("Ojo: entregar antes de las 2 PM.", 14),
    ("HORARIO: preferentemente antes de 14hs", 14),
    ("Urgente antes de las 14!!", 14),
    ("Deliver before 4pm", 16),
])
def test_horarios_deterministicos(texto, hora):
    hit = find_time_expression(texto)
    assert hit is not None
    assert to_window(hit, DAY)["tw_end"].endswith(f"{hora:02d}:00")


def test_rangos_horarios():
    assert to_window(find_time_expression("entre 9 y 12"), DAY) == {
        "tw_start": "2026-09-06 09:00", "tw_end": "2026-09-06 12:00"}
    assert to_window(find_time_expression("de 10:00 a 15:00"), DAY)["tw_start"].endswith("10:00")


def test_si_no_se_entiende_no_se_inventa_la_ventana():
    hit = find_time_expression("a partir de las 10")     # sin fin conocido
    assert hit is not None and to_window(hit, DAY) == {}


# ---------- bultos ----------

def test_bultos_peso_y_medidas(ar_context):
    values = {v.field: v.value for v in
              extract_packages(TextCanvas("Caja 20x30x40 cm, 8 kg, 2 bultos"), ar_context)}
    assert values == {"length_cm": 20.0, "width_cm": 30.0, "height_cm": 40.0,
                      "weight_kg": 8.0, "quantity": 2}


def test_un_numero_suelto_no_es_una_cantidad(ar_context):
    assert extract_packages(TextCanvas("11 de Septiembre Nro 1913"), ar_context) == []


# ---------- pipeline completo ----------

CASOS = [
    ("Ana Perez paso a avisar que vive en Av. Corrientes 100 en CABA y el cel de "
     "ella es el 11-4000-1000. Dijo que solo recibe si es antes de las 14hs!!",
     "Ana Perez", "Av. Corrientes 100", "+541140001000"),
    ("Para Maria Gomez el paquete va a Belgrano en Av. Cabildo 174, llamar al "
     "11-4002-1002 si no contesta.",
     "Maria Gomez", "Av. Cabildo 174", "+541140021002"),
    ("Direccion: Av. Rivadavia 211 (Caballito / CABA) -- Destinatario: Carlos Ruiz "
     "-- TE: +54 9 11 4003 1003.",
     "Carlos Ruiz", "Av. Rivadavia 211", "+5491140031003"),
    ("Entregar a Lucia Fernandez en Av. Cordoba 248 piso 3 B (1140041004).",
     "Lucia Fernandez", "Av. Cordoba 248", "+541140041004"),
    ("11-4011-1011 es el numero de Santiago Herrera. Hay que llevarle las cosas a "
     "Lavalle 507 Microcentro. Nota: el portero electrico dice Herrera.",
     "Santiago Herrera", "Lavalle 507", "+541140111011"),
    ("Facundo Molina // Malabia 1136, Palermo // TEL: 1140281028 // Ojo: entregar "
     "antes de las 2 PM.",
     "Facundo Molina", "Malabia 1136", "+541140281028"),
    ("CONTACTO: Diego Martinez (1140351035). LUGAR: Darwin 1395 Villa Crespo. "
     "HORARIO: preferentemente antes de 14hs.",
     "Diego Martinez", "Darwin 1395", "+541140351035"),
    ("Entregar en Nuñez a Lucia Fernandez, la direccion exacta es 11 de Septiembre "
     "Nro 1913, cel 1140491049 (tiene franja antes de las 14).",
     "Lucia Fernandez", "11 de Septiembre", "+541140491049"),
]


@pytest.mark.parametrize("texto,nombre,direccion,telefono", CASOS)
def test_extrae_los_tres_campos_clave(pipeline, texto, nombre, direccion, telefono):
    record = pipeline.run(texto, service_date=DAY)
    assert record.get("customer_name") == nombre
    assert direccion in record.get("address")
    assert record.get("phone") == telefono


@pytest.mark.parametrize("texto,_n,_a,_p", CASOS)
def test_el_nombre_nunca_arrastra_la_etiqueta(pipeline, texto, _n, _a, _p):
    nombre = pipeline.run(texto, service_date=DAY).get("customer_name") or ""
    for etiqueta in ("Para ", "Destinatario", "CONTACTO", "Entregar", "Direccion"):
        assert not nombre.startswith(etiqueta)


@pytest.mark.parametrize("texto,_n,_a,_p", CASOS)
def test_la_direccion_no_es_la_frase_entera(pipeline, texto, _n, _a, _p):
    direccion = pipeline.run(texto, service_date=DAY).get("address") or ""
    assert len(direccion) < len(texto) * 0.6


def test_cada_campo_trae_confianza_metodo_y_evidencia(pipeline):
    record = pipeline.run(CASOS[0][0], service_date=DAY)
    telefono = record.fields["phone"]
    assert telefono.method == "phonenumbers"
    assert telefono.confidence >= 0.9
    assert telefono.raw == "11-4000-1000"          # el original se preserva
    assert record.fields["address"].evidence


def test_el_texto_horario_crudo_se_conserva_aparte_de_la_ventana(pipeline):
    record = pipeline.run(CASOS[6][0], service_date=DAY)
    assert record.get("delivery_time_text") == "preferentemente antes de 14hs"
    assert record.get("tw_end") == "2026-09-06 14:00"


def test_no_hay_ninguna_llamada_a_modelo(pipeline):
    for texto, *_ in CASOS:
        pipeline.run(texto, service_date=DAY)
    assert pipeline.stats.ai_calls == 0
    assert pipeline.stats.records == len(CASOS)
