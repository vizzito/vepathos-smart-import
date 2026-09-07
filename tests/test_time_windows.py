"""Time-window soft constraints + numbered paste reader."""
from datetime import date
from pathlib import Path

import pytest

from smart_import.extraction.heuristics import try_heuristic
from smart_import.extraction.time_window import find_time_expression, to_window
from smart_import.extraction.time_windows import parse_before_constraint
from smart_import.extraction.tz import resolve_timezone, validate_iana
from smart_import.readers import read_any
from smart_import.readers.text_reader import looks_like_numbered_delivery_list


DAY = date(2026, 9, 6)


@pytest.mark.parametrize("raw,end_h,end_m", [
    ("entregar antes de las 14hs", 14, 0),
    ("antes de las 4 pm", 16, 0),
    ("before 4pm", 16, 0),
    ("Deliver before 16:30", 16, 30),
    ("hasta las 9:00", 9, 0),
])
def test_parse_before_constraint(raw, end_h, end_m):
    tw = parse_before_constraint(raw, DAY)
    assert tw is not None
    assert tw["tw_start"] == "2026-09-06 00:00"
    assert tw["tw_end"] == f"2026-09-06 {end_h:02d}:{end_m:02d}"


def test_heuristic_arrow_with_tw_and_reference():
    line = (
        "1) Ana Pérez <11 4000-1000> → Av. Corrientes 100, CABA, CABA "
        "| entregar antes de las 14hs"
    )
    hit = try_heuristic(line, region="AR", service_date=DAY)
    assert hit is not None
    assert hit["customer_name"] == "Ana Pérez"
    assert hit["address"].startswith("Av. Corrientes 100")
    assert "CABA" in hit["address"]
    assert hit["phone"] == "11 4000-1000"
    assert hit["tw_end"] == "2026-09-06 14:00"
    assert "14hs" in hit["reference"] or "entregar" in hit["reference"]


PASTE = (
    "Hola equipo,\n\n"
    "1) Ana Pérez <11 4000-1000> → Av. Corrientes 100, CABA | entregar antes de las 14hs\n"
    "2) Juan López <11 4001-1001> → Av. Santa Fe 137, Palermo, CABA\n"
    "3) María Gómez <11 4002-1002> → Av. Cabildo 174, Belgrano, CABA\n\n"
    "Gracias,\nDespacho\n"
)


def test_paste_numerado_se_lee_como_texto_libre_entero(tmp_path: Path):
    """Un .txt asi ya no se parte por comas: llega entero a la extraccion."""
    assert looks_like_numbered_delivery_list(PASTE)
    path = tmp_path / "paste.txt"
    path.write_text(PASTE, encoding="utf-8")
    table = read_any(path)
    assert table.meta.text_mode == "free_text"
    assert table.meta.delimiter is None
    assert table.meta.header_row == 0
    assert table.columns == ["document"]
    assert len(table) == 1
    assert table.rows[0][0] == PASTE            # documento completo, sin recortes


def test_caba_antes_de_las_14_sale_en_utc():
    """America/Argentina/Buenos_Aires es UTC-3 todo el anio: 14:00 local → 17:00Z."""
    hit = find_time_expression("entregar antes de las 14hs")
    tw = to_window(hit, DAY, timezone="America/Argentina/Buenos_Aires")
    assert tw["tw_start"] == "2026-09-06T03:00:00Z"
    assert tw["tw_end"] == "2026-09-06T17:00:00Z"
    assert tw["tw_timezone"] == "UTC"


def test_sin_timezone_sigue_naive():
    hit = find_time_expression("entregar antes de las 14hs")
    tw = to_window(hit, DAY)
    assert tw == {"tw_start": "2026-09-06 00:00", "tw_end": "2026-09-06 14:00"}
    assert "tw_timezone" not in tw


def test_ventana_incompleta_no_inventa_timezone():
    hit = find_time_expression("a partir de las 10")
    assert hit is not None
    assert to_window(hit, DAY, timezone="America/Argentina/Buenos_Aires") == {}


def test_dst_nueva_york():
    """America/New_York en julio es UTC-4: 14:00 local → 18:00Z."""
    summer = date(2026, 7, 15)
    hit = find_time_expression("before 2pm")
    tw = to_window(hit, summer, timezone="America/New_York")
    assert tw["tw_end"] == "2026-07-15T18:00:00Z"
    assert tw["tw_timezone"] == "UTC"


def test_settings_gana_sobre_depot():
    assert resolve_timezone(
        "America/Argentina/Buenos_Aires",
        "America/Sao_Paulo",
    ) == "America/Argentina/Buenos_Aires"
    assert resolve_timezone(None, "America/Sao_Paulo") == "America/Sao_Paulo"
    assert resolve_timezone("No/Such/Zone", "America/Sao_Paulo") == "America/Sao_Paulo"
    assert resolve_timezone(None, None) is None
    assert validate_iana("UTC") == "UTC"
    assert validate_iana("not-a-zone") is None


def test_heuristic_con_timezone_emite_utc():
    line = "Ana Pérez <11 4000-1000> → Av. Corrientes 100 | entregar antes de las 14hs"
    hit = try_heuristic(line, region="AR", service_date=DAY,
                        timezone="America/Argentina/Buenos_Aires")
    assert hit is not None
    assert hit["tw_end"] == "2026-09-06T17:00:00Z"
    assert hit["tw_timezone"] == "UTC"


def test_paste_numerado_produce_tres_entregas_sin_el_pie(tmp_path: Path):
    """El preambulo y la despedida se ignoran; ninguna linea se pierde como header."""
    from smart_import.detection.delivery_classifier import DeliveryCandidateClassifier
    from smart_import.detection.segmenter import FreeTextSegmenter
    from smart_import.extraction.context import ExtractionContext

    segmented = FreeTextSegmenter().split(PASTE)
    classifier = DeliveryCandidateClassifier(context=ExtractionContext(phone_region="AR"))
    verdicts = [(s, classifier.classify(s.body)) for s in segmented.all_segments]

    deliveries = [s for s, v in verdicts if v.is_delivery]
    assert len(deliveries) == 3
    assert "entregar antes" in deliveries[0].text
    assert "CABA" in deliveries[0].text
    ignored = [s.text for s, v in verdicts if not v.is_delivery]
    assert any("Gracias" in t for t in ignored)
    assert any("Despacho" in t for t in ignored)
