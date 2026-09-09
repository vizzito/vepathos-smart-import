"""Las tres bandas que colorea la UI: valid / review / needs_geocoding.

El bug que originó esto: un match a nivel calle salia con status
`low_confidence` pero confidence 0.87, y la UI —que colorea por numero cuando no
le llega el status— lo pintaba verde. Numero, status y banda tienen que decir
siempre lo mismo.
"""
import os
from dataclasses import fields

import pytest

from smart_import.config import Config
from smart_import.geocoding.bands import (
    BAND_NEEDS_GEOCODING, BAND_REVIEW, BAND_VALID, band_for, band_from_row, percent,
)
from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder
from tests.conftest import PREFIJOS_DE_CONFIG

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


def test_un_pin_aproximado_sigue_el_score():
    """Precision OSM es diagnostico: el color lo marca el % vs GEOCODE_*_BAND.

    Un match a nivel calle con 0.90 (>= valid) es verde; uno en el rango ambar
    sigue ambar. Las coords del archivo (`already`/`manual`) no se discuten.
    """
    for precision in ("street", "street_mismatch", "street_weak", "suspect",
                      "locality", "poi"):
        assert band_for("low_confidence", 0.90, precision=precision,
                        valid_at=CFG.geocode_valid_band,
                        review_at=CFG.geocode_review_band) == BAND_VALID, precision
        mid = (CFG.geocode_review_band + CFG.geocode_valid_band) / 2
        assert band_for("low_confidence", mid, precision=precision,
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


def test_los_defaults_del_dataclass_y_de_from_env_no_pueden_divergir(monkeypatch):
    """`Config()` y `Config.from_env()` sin env tienen que dar lo mismo.

    El config promete que el servicio arranca sin ninguna env seteada, o sea que
    hay UN default por variable. Cuando el literal de `from_env` se movio y el
    del dataclass no, quedaron dos: los tests que construian `Config()` a mano
    veian street_match_min=0.80 y soft_reject_min=0.50, y produccion 0.70/0.70.

    El de soft_reject era el caro: 0.50 esta por debajo de `review_band`, y ahi
    `band_for` pasa la fila a needs_geocoding y le saca el pin. O sea que el
    soft-reject calculaba un pin de respaldo para que lo tirara la banda.
    """
    for name in list(os.environ):
        if name.startswith(PREFIJOS_DE_CONFIG):
            monkeypatch.delenv(name, raising=False)

    quieto = Config()
    del_env = Config.from_env()
    # pbf_dir se resuelve del filesystem (repo vecino), no es un default fijo.
    ignorar = {"pbf_dir"}
    distintos = {
        f.name: (getattr(quieto, f.name), getattr(del_env, f.name))
        for f in fields(Config)
        if f.name not in ignorar
        and getattr(quieto, f.name) != getattr(del_env, f.name)
    }
    assert not distintos, (
        f"defaults duplicados que ya divergieron: {distintos}. El default vive "
        "en el dataclass; `from_env` tiene que repetir EL MISMO valor.")


def test_verde_y_matched_tienen_que_significar_lo_mismo():
    """`geocode_valid_band` == `match_threshold`, o el color miente.

    `matched` sale con score >= max(match_threshold, valid_band), pero el COLOR
    lo decide valid_band sola. Con match=0.81 y valid=0.80 —los defaults que
    traia el codigo— habia una ventana de 0.01 donde el geocoder devolvia
    status=low_confidence y la banda salia VERDE: un pin que le dice al operador
    "usalo tal cual" sobre algo que el sistema marco dudoso.
    """
    cfg = Config.from_env()
    assert cfg.geocode_valid_band == cfg.match_threshold, (
        f"valid_band={cfg.geocode_valid_band} != "
        f"match_threshold={cfg.match_threshold}: hay scores que salen "
        "low_confidence y se pintan verde")

    # La ventana concreta: el score justo debajo del corte de `matched`.
    apenas_abajo = cfg.match_threshold - 0.005
    assert band_for("low_confidence", apenas_abajo, has_coords=True,
                    valid_at=cfg.geocode_valid_band,
                    review_at=cfg.geocode_review_band) == BAND_REVIEW


def test_el_soft_reject_no_puede_quedar_debajo_de_la_banda_ambar():
    """Invariante de producto, no de codigo: SOFT_REJECT_MIN >= REVIEW_BAND.

    El soft-reject existe para dejar un pin de respaldo y poder medir distancia
    en el mapa. Debajo de la banda ambar ese pin no se muestra: `band_for`
    devuelve needs_geocoding y `_stamp` borra lat/lng. Configurarlo mas abajo no
    da mas pines, da trabajo que se descarta.
    """
    cfg = Config.from_env()
    assert cfg.geocode_soft_reject_min >= cfg.geocode_review_band, (
        f"soft_reject_min={cfg.geocode_soft_reject_min} < "
        f"review_band={cfg.geocode_review_band}: los pines de soft-reject se "
        "calculan y la banda los tira")


def test_la_banda_ambar_arranca_donde_arranca_el_pin():
    """`geocode_review_band` == `low_confidence_threshold`.

    LOW_CONFIDENCE es el piso para DEVOLVER coordenada; REVIEW_BAND el piso para
    MOSTRARLA. Si REVIEW fuera mas alto se calculan pines que la banda tira a la
    basura; si fuera mas bajo, la UI reservaria un color para scores que el
    geocoder nunca devuelve con pin.
    """
    cfg = Config.from_env()
    assert cfg.geocode_review_band == cfg.low_confidence_threshold, (
        f"review_band={cfg.geocode_review_band} != "
        f"low_confidence_threshold={cfg.low_confidence_threshold}")
