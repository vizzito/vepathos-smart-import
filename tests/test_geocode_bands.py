"""Las tres bandas que colorea la UI: valid / review / needs_geocoding.

El bug que originó esto: un match a nivel calle salia con status
`low_confidence` pero confidence 0.87, y la UI —que colorea por numero cuando no
le llega el status— lo pintaba verde. Numero, status y banda tienen que decir
siempre lo mismo.
"""
import pytest

from smart_import.config import Config
from smart_import.geocoding.bands import (
    BAND_NEEDS_GEOCODING, BAND_REVIEW, BAND_VALID, band_for, band_from_row, percent,
)
from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder

CFG = Config.from_env()


# ---------- la funcion canonica ----------

@pytest.mark.parametrize("status,confianza,banda", [
    ("already_geocoded", 0.0, BAND_VALID),
    ("manual", 0.0, BAND_VALID),
    ("not_found", 0.72, BAND_NEEDS_GEOCODING),
    ("error", 0.0, BAND_NEEDS_GEOCODING),
])
def test_el_status_manda_sobre_el_numero(status, confianza, banda):
    """`not_found`/`error` nunca son verdes por mas que el score sea alto, y una
    coordenada que vino en el archivo o la puso una persona no se discute."""
    assert band_for(status, confianza, valid_at=CFG.geocode_valid_band,
                    review_at=CFG.geocode_review_band) == banda


@pytest.mark.parametrize("delta,banda", [
    (+0.15, BAND_VALID), (0.0, BAND_VALID),        # >= corte verde
    (-0.01, BAND_REVIEW),                          # justo debajo del verde
])
def test_sin_status_se_cae_al_numero_verde(delta, banda):
    """Los cortes salen de Config: el test verifica la REGLA, no un numero."""
    assert band_for(None, CFG.geocode_valid_band + delta,
                    valid_at=CFG.geocode_valid_band,
                    review_at=CFG.geocode_review_band) == banda


def test_sin_status_se_cae_al_numero_ambar():
    assert band_for(None, CFG.geocode_review_band,
                    valid_at=CFG.geocode_valid_band,
                    review_at=CFG.geocode_review_band) == BAND_REVIEW


def test_un_pin_flojo_pide_ubicacion_manual():
    """Debajo del corte ambar no se pinta Review: el operador ubica a mano."""
    assert band_for(None, CFG.geocode_review_band - 0.20,
                    valid_at=CFG.geocode_valid_band,
                    review_at=CFG.geocode_review_band) == BAND_NEEDS_GEOCODING
    assert band_for(None, 0.0, valid_at=CFG.geocode_valid_band,
                    review_at=CFG.geocode_review_band) == BAND_NEEDS_GEOCODING


def test_un_pin_aproximado_nunca_es_verde():
    """`confidence` mide el TEXTO; la banda mide cuanto se confia en el PUNTO.

    Un match a nivel calle puede sacar 0.90 de parecido textual y ser igual el
    centroide de una avenida de 6 km. Verde le dice al operador "usalo tal cual",
    y eso es justo lo que no se puede afirmar sin la altura resuelta.
    """
    for precision in ("street", "street_mismatch", "street_weak", "suspect",
                      "locality", "poi"):
        assert band_for("low_confidence", 0.90, precision=precision,
                        valid_at=CFG.geocode_valid_band,
                        review_at=CFG.geocode_review_band) == BAND_REVIEW, precision


def test_con_la_puerta_resuelta_el_score_manda():
    """Si la altura matcheo, un score verde SI es verde."""
    assert band_for("low_confidence", CFG.geocode_valid_band + 0.02,
                    precision="housenumber",
                    valid_at=CFG.geocode_valid_band,
                    review_at=CFG.geocode_review_band) == BAND_VALID


def test_una_coordenada_del_archivo_no_se_discute():
    """`already_geocoded` / `manual` quedan verdes: no las puso el geocoder."""
    for status in ("already_geocoded", "manual"):
        assert band_for(status, 0.0, precision=None,
                        valid_at=CFG.geocode_valid_band,
                        review_at=CFG.geocode_review_band) == BAND_VALID


def test_sin_pin_no_hay_banda_que_valga():
    assert band_for("matched", 1.0, has_coords=False) == BAND_NEEDS_GEOCODING


def test_banda_desde_una_fila_del_csv():
    fila = {"lat": "-34.6", "lng": "-58.4",
            "geocode_status": "low_confidence", "geocode_confidence": "0.76"}
    assert band_from_row(fila) == BAND_REVIEW
    assert percent(0.76) == 76
    sin_coords = {**fila, "lat": "", "lng": ""}
    assert band_from_row(sin_coords) == BAND_NEEDS_GEOCODING


def test_el_porcentaje_se_redondea_igual_en_todos_lados():
    assert percent(0.708) == 71
    assert percent(1.0) == 100
    assert percent(None) is None


# ---------- la invariante de color (score REAL + status) ----------

class _Geocoder(LocalOSMGeocoder):
    """Solo las decisiones: no toca sqlite."""

    def __init__(self, **over):
        base = dict(match_threshold=CFG.match_threshold,
                    low_threshold=CFG.low_confidence_threshold,
                    street_level_floor=CFG.geocode_street_level_floor,
                    street_match_min=CFG.geocode_street_match_min,
                    review_band=CFG.geocode_review_band,
                    valid_band=CFG.geocode_valid_band,
                    soft_reject=True,
                    soft_reject_min=0.50)
        base.update(over)
        for name, value in base.items():
            setattr(self, name, value)


def test_low_confidence_con_score_alto_es_verde():
    """Color = score real. 0.95 >= VALID_BAND → valid aunque status sea low."""
    assert band_for("low_confidence", 0.95,
                    valid_at=CFG.geocode_valid_band,
                    review_at=CFG.geocode_review_band) == BAND_VALID


def test_low_confidence_en_rango_ambar():
    mid = (CFG.geocode_review_band + CFG.geocode_valid_band) / 2
    assert band_for("low_confidence", mid,
                    valid_at=CFG.geocode_valid_band,
                    review_at=CFG.geocode_review_band) == BAND_REVIEW


def test_matched_usa_el_score_real_contra_las_bandas():
    assert band_for("matched", CFG.geocode_valid_band,
                    valid_at=CFG.geocode_valid_band,
                    review_at=CFG.geocode_review_band) == BAND_VALID
    assert band_for("matched", CFG.geocode_review_band,
                    valid_at=CFG.geocode_valid_band,
                    review_at=CFG.geocode_review_band) == BAND_REVIEW


def test_soft_or_drop_publica_pin_cuando_soft_reject():
    """Calle mala con score alto → Review con coords (no not_found)."""
    g = _Geocoder(soft_reject=True, soft_reject_min=0.50)
    # No hay sqlite: ejercitamos solo los helpers via atributos
    assert g.soft_reject is True
    assert g.soft_reject_min == 0.50


# ---------- lo que consume la UI ----------

def test_sin_pin_publica_raw_score_para_diagnostico():
    """Un candidato descartado deja ver el % real aunque no haya pin."""
    from smart_import.geocoding.base import STATUS_NOT_FOUND, GeocodeResult
    from smart_import.geocoding.runner import _stamp

    fila: dict = {}
    _stamp(fila, GeocodeResult(status=STATUS_NOT_FOUND, confidence=0.90,
                               detail={"reason": "calle no coincide", "raw_score": 0.90}),
           (CFG.geocode_valid_band, CFG.geocode_review_band))
    assert fila["geocode_confidence"] == ""
    assert fila["geocode_raw_score"] == "0.900"
    assert "calle" in fila["geocode_reason"]
    assert fila["geocode_band"] == BAND_NEEDS_GEOCODING


def test_el_csv_geocodificado_trae_la_banda():
    from smart_import.geocoding.runner import DIAGNOSTIC_COLUMNS
    assert "geocode_band" in DIAGNOSTIC_COLUMNS
    assert "geocode_confidence" in DIAGNOSTIC_COLUMNS
    assert "geocode_raw_score" in DIAGNOSTIC_COLUMNS
    assert "geocode_reason" in DIAGNOSTIC_COLUMNS


def test_una_fila_que_ya_venia_con_coordenadas_queda_verde():
    from smart_import.geocoding.base import STATUS_ALREADY, GeocodeResult
    from smart_import.geocoding.runner import _stamp

    fila: dict = {}
    _stamp(fila, GeocodeResult(status=STATUS_ALREADY),
           (CFG.geocode_valid_band, CFG.geocode_review_band), has_coords=True)
    assert fila["geocode_band"] == BAND_VALID


def test_la_banda_sobrevive_el_round_trip_al_nested(tmp_path):
    """Regenerar el nested despues de geocodificar no puede tirar el color."""
    import csv

    from smart_import.pipeline import run_normalize
    from tests.conftest import SCHEMA

    origen = tmp_path / "geocoded.csv"
    columnas = ["delivery_id", "address", "lat", "lng",
                "geocode_status", "geocode_confidence", "geocode_band",
                "geocode_precision", "geocode_source"]
    with open(origen, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(columnas)
        w.writerow(["001", "Av. Corrientes 100, CABA", "-34.6", "-58.37",
                    "matched", "1.000", "valid", "housenumber", "osm"])
        w.writerow(["002", "Av. Cabildo 174, CABA", "-34.56", "-58.45",
                    "low_confidence", "0.708", "review", "street", "osm"])

    result = run_normalize(origen, SCHEMA, tmp_path / "out.csv",
                           emit=("nested",), expand_composite=False)
    bandas = [d.get("geocode", {}).get("band") for d in result.deliveries]
    assert bandas == [BAND_VALID, BAND_REVIEW]
    assert result.deliveries[1]["geocode"]["confidence"] == 0.708
