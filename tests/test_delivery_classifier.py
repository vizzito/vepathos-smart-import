"""El clasificador que impide que "Salutos" termine como address."""
import pytest

from smart_import.detection.delivery_classifier import (
    DELIVERY, IGNORE, DeliveryCandidateClassifier,
)
from smart_import.detection.segmenter import FreeTextSegmenter


@pytest.fixture
def classifier(ar_context):
    return DeliveryCandidateClassifier(context=ar_context)


@pytest.mark.parametrize("texto", [
    "Hola", "Gracias", "Saludos", "Despacho", "Fin",
    "Pendiente", "Sin novedades", "Urgente", "Cliente", "Entrega",
    "Salutos,",
    "Hola chicos! Dejo el listado cargado de cualquier forma como me lo mandaron",
])
def test_no_hay_entrega_sin_evidencia(classifier, texto):
    verdict = classifier.classify(texto)
    assert verdict.classification == IGNORE
    assert verdict.reasons


@pytest.mark.parametrize("texto", [
    "Ana Perez vive en Av. Corrientes 100 en CABA, cel 11-4000-1000",
    "Juan Lopez (1140011001) entrega en Palermo, la calle es Av. Santa Fe al 137.",
    "CONTACTO: Diego Martinez (1140351035). LUGAR: Darwin 1395 Villa Crespo.",
    "Rahul Sharma, Flat 14B, Shanti Nagar, Andheri East, Mumbai 400069, +91 98765 43210",
])
def test_hay_entrega_cuando_se_acumula_evidencia(classifier, texto):
    verdict = classifier.classify(texto)
    assert verdict.classification == DELIVERY
    assert verdict.score >= 0.55
    assert verdict.reasons


def test_el_veredicto_explica_que_señales_encontro(classifier):
    verdict = classifier.classify(
        "Ana Perez vive en Av. Corrientes 100, cel 11-4000-1000, antes de las 14hs")
    razones = " | ".join(verdict.reasons)
    assert "telefono valido" in razones
    assert "direccion" in razones
    assert "horaria" in razones


def test_el_documento_completo_da_doce_entregas_y_tres_ignorados(free_text, classifier):
    segments = FreeTextSegmenter().split(free_text["whatsapp_12"]).all_segments
    verdicts = [classifier.classify(s.body) for s in segments]
    assert sum(1 for v in verdicts if v.is_delivery) == 12
    assert sum(1 for v in verdicts if not v.is_delivery) == 3


def test_ningun_falso_positivo_en_el_corpus_de_ruido(free_text, classifier):
    segments = FreeTextSegmenter().split(free_text["false_positives"]).all_segments
    assert all(not classifier.classify(s.body).is_delivery for s in segments)


def test_los_thresholds_salen_de_la_config(ar_context):
    from smart_import.config import Config
    estricto = DeliveryCandidateClassifier(
        Config.from_env().replace(delivery_accept_threshold=0.99), ar_context)
    verdict = estricto.classify("Juan Lopez (1140011001) en Av. Santa Fe al 137.")
    assert verdict.classification != DELIVERY      # la misma entrada, otro umbral
