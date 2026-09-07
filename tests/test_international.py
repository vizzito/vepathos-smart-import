"""El parser no puede depender de Argentina.

India ejercita todo lo que CABA esconde: pincode de 6 digitos, `landmark`,
`unit` antes de la calle, y numeracion `numero + via` en vez de `via + numero`.
"""
import pytest

from smart_import.addresses import AddressCandidateScorer, HeuristicAddressParser
from smart_import.detection.delivery_classifier import DeliveryCandidateClassifier
from smart_import.extraction.context import ExtractionContext
from smart_import.extraction.phone_extract import find_phones
from smart_import.extraction.pipeline import FieldExtractionPipeline

CASOS = [
    ("Rahul Sharma, Flat 14B, Shanti Nagar, Near Hanuman Temple, Andheri East, "
     "Mumbai 400069, +91 98765 43210", "Rahul Sharma", "+919876543210", "400069"),
    ("Priya Patel - 23 MG Road, Bengaluru 560001, phone +91 80 2345 6789",
     "Priya Patel", "+918023456789", "560001"),
]


@pytest.fixture
def india():
    return ExtractionContext(phone_region="IN")


@pytest.mark.parametrize("texto,nombre,telefono,pincode", CASOS)
def test_extrae_nombre_telefono_y_pincode(india, texto, nombre, telefono, pincode):
    record = FieldExtractionPipeline(context=india).run(texto)
    assert record.get("customer_name") == nombre
    assert record.get("phone") == telefono
    assert pincode in record.get("address")


def test_el_telefono_usa_la_region_del_job():
    hits = find_phones("contacto +91 99887 76655", ("IN",))
    assert hits and hits[0].valid and hits[0].e164 == "+919988776655"


def test_el_pincode_se_reconoce_como_codigo_postal():
    parsed = HeuristicAddressParser().parse("23 MG Road, Bengaluru 560001")
    assert parsed.get("postcode") == "560001"


def test_landmark_y_localidad_se_preservan():
    parsed = HeuristicAddressParser().parse(
        "Flat 14B, Shanti Nagar, Near Hanuman Temple, Andheri East, Mumbai 400069")
    assert parsed.get("landmark") == "Hanuman Temple"
    assert parsed.get("suburb") == "Shanti Nagar"


def test_el_scorer_no_necesita_tokens_argentinos():
    scorer = AddressCandidateScorer()
    assert scorer.score("23 MG Road, Bengaluru 560001").score >= 0.5
    assert scorer.score("Flat 14B, Shanti Nagar, Andheri East, Mumbai 400069").score >= 0.5


def test_el_documento_indio_da_tres_entregas_y_tres_ignorados(free_text, india):
    from smart_import.detection.segmenter import FreeTextSegmenter

    classifier = DeliveryCandidateClassifier(context=india)
    segments = FreeTextSegmenter().split(free_text["india"]).all_segments
    verdicts = [classifier.classify(s.body) for s in segments]
    assert sum(1 for v in verdicts if v.is_delivery) == 3
    assert sum(1 for v in verdicts if not v.is_delivery) == 3      # header + 2 de pie


def test_sin_region_declarada_no_se_agrega_pais(india):
    """Extraer != normalizar: sin contexto explicito la direccion queda como vino."""
    from smart_import.config import Config
    from smart_import.extraction.free_text import FreeTextExtractor

    sin_region = FreeTextExtractor(Config.from_env(), ExtractionContext())
    record = sin_region.run_value("Priya Patel - 23 MG Road, Bengaluru 560001")
    assert "India" not in record.get("address")
    assert "Argentina" not in record.get("address")
