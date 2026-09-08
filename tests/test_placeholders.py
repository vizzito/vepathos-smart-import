"""Los pastes que mostramos como ejemplo tienen que funcionar.

Si el usuario pega el placeholder de la UI (o un demo de `examples/`)
y no salen entregas, el producto se ve roto. No alcanza con que el job
no explote: cada stop del ejemplo tiene que tener nombre, direccion y tel.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from smart_import.pipeline import run_normalize
from tests.conftest import EXAMPLES, FREE_TEXT, SCHEMA

DAY = date(2026, 9, 6)


def _normalize(path: Path, phone_region: str):
    return run_normalize(path, SCHEMA, phone_region=phone_region, service_date=DAY)


def test_placeholder_ui_whatsapp_6_es_el_ejemplo_de_la_caja():
    """El texto corto de la UI (6 bullets + pie 'pegá un chat')."""
    gold = json.loads((FREE_TEXT / "whatsapp_placeholder_6.expected.json").read_text())
    result = _normalize(FREE_TEXT / "whatsapp_placeholder_6.txt", "AR")
    assert result.report["text_mode"] == "free_text"
    assert result.report["deliveries"] == gold["deliveries"]
    assert result.report["invalid_rows"] == 0

    ignorados = " | ".join(
        i["text"] for i in result.report["extraction"]["ignored_samples"]
    )
    for needle in gold["ignored_contains"]:
        assert needle in ignorados, (needle, ignorados)

    extraidos = [d.get("customer_name") for d in result.deliveries]
    assert extraidos == [r["customer_name"] for r in gold["records"]]

    for delivery, esperado in zip(result.deliveries, gold["records"]):
        addr = delivery.get("address") or ""
        assert esperado["address_contains"] in addr, (esperado["customer_name"], addr)
        assert delivery.get("phone") == esperado["phone"]
        hora = esperado["tw_end_hour"]
        if hora is None:
            continue
        window = delivery.get("time_window")
        assert window, esperado["customer_name"]
        assert window["end"].endswith(f"{hora:02d}:00")


@pytest.mark.parametrize("path,region,n,names", [
    (FREE_TEXT / "whatsapp_12.txt", "AR", 12,
     ["Ana Perez", "Juan Lopez", "Maria Gomez", "Carlos Ruiz",
      "Martin Castro", "Facundo Molina", "Nicolas Diaz"]),
    (FREE_TEXT / "whatsapp_12_nobullets.txt", "AR", 12,
     ["Ana Perez", "Facundo Molina"]),
    (FREE_TEXT / "paste_55_caba.txt", "AR", 55, ["Ana Pérez", "Facundo Molina"]),
    (EXAMPLES / "geocode-truth" / "miami_whatsapp_6.txt", "US", 6,
     ["Ana Rivera", "James Lopez", "Maria Gomez", "Carlos Ruiz",
      "Martin Castro", "Facundo Molina"]),
    (EXAMPLES / "columna-mezclada" / "06_paste_ready.txt", "AR", 7,
     ["Laura Méndez", "Carla Benítez", "Emily Johnson"]),
    (EXAMPLES / "columna-mezclada" / "01_dash_name_address_phone.csv", "AR", 5,
     ["Laura Méndez", "Diego Ruiz"]),
    (EXAMPLES / "columna-mezclada" / "02_entregar_a_telefono.csv", "AR", 5, []),
    (EXAMPLES / "columna-mezclada" / "04_marketplace_whatsapp.csv", "AR", 5, []),
    (EXAMPLES / "columna-mezclada" / "05_pipe_messy_en_es.csv", "AR", 5, []),
    (FREE_TEXT / "paste_10_human_variants.txt", "AR", 10,
     ["Ana Pérez", "Lucia Fernandez", "Nicolas Diaz"]),
], ids=[
    "whatsapp_12", "whatsapp_12_nobullets", "paste_55_caba",
    "miami_whatsapp_6", "columna_mezclada_paste_ready", "columna_mezclada_01",
    "columna_mezclada_02", "columna_mezclada_04", "columna_mezclada_05",
    "paste_10_human_variants",
])
def test_demos_publicados_salen_completos(path, region, n, names):
    result = _normalize(path, region)
    assert result.report["deliveries"] == n, path.name
    assert result.report["invalid_rows"] == 0, path.name
    got = [d.get("customer_name") for d in result.deliveries]
    for name in names:
        assert name in got, (path.name, name, got)
    for delivery in result.deliveries:
        assert delivery.get("address"), (path.name, delivery)
        assert delivery.get("phone"), (path.name, delivery.get("customer_name"))


def test_lista_caba_reconoce_el_sexto_sin_punto():
    """Lista numerada: el item siguiente sin puntuacion ('6 nombre') es otro registro.

    Regla global: N == ultimo+1 y sigue una letra. No exige '6.' / '6)'.
    El nombre en minusculas y un telefono AR invalido no se inventan aca.
    """
    result = _normalize(FREE_TEXT / "lista_caba_6_ultimo_sin_punto.txt", "AR")
    assert result.report["text_mode"] == "free_text"
    assert result.report["deliveries"] == 6
    assert result.report["invalid_rows"] == 0

    names = [d.get("customer_name") or "" for d in result.deliveries]
    assert names[0] == "Ana Pérez"
    assert names[4] == "Lucía Fernández"

    last = result.deliveries[5]
    addr = (last.get("address") or "").lower()
    assert "santa fe" in addr and "890" in addr
    # no se fusiono con Lucia: el corte es global (N+1 sin puntuacion), no el nombre
    assert "lucía" not in addr and "lucia" not in addr
    assert "córdoba" not in addr and "cordoba" not in addr


def _geocode_extracted(result, *, city: str, country: str, lat: float, lon: float):
    """Misma query que la UI: address extraido + depot, sin enhance."""
    from smart_import.geocoding.depot_context import depot_from_params
    from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder
    from smart_import.geocoding.query import build_geocode_query

    if city == "CABA":
        from tests.test_geocode_accuracy import _caba_index
        cfg, index, _ = _caba_index()
    else:
        from tests.test_geocode_accuracy import _miami_index
        cfg, index, _ = _miami_index()
    geocoder = LocalOSMGeocoder(index)
    depot = depot_from_params(
        origin_lat=lat, origin_lon=lon,
        depot_city=city, depot_country=country,
        max_distance_km=float(cfg.max_geocode_distance_km),
    )
    out = []
    try:
        for delivery in result.deliveries:
            query = build_geocode_query(
                delivery.get("address") or "", depot=depot, enhance=False,
            )
            out.append((delivery, query, geocoder.geocode(query, origin=(lat, lon))))
    finally:
        geocoder.close()
    return out


@pytest.mark.real_geo
def test_placeholder_caba_geocodifica_las_6_puertas():
    """Alturas elegidas porque OSM las tiene. Si esto baja, el ejemplo de la UI miente."""
    result = _normalize(FREE_TEXT / "whatsapp_placeholder_6.txt", "AR")
    assert result.report["deliveries"] == 6
    rows = _geocode_extracted(
        result, city="CABA", country="Argentina",
        lat=-34.6037, lon=-58.3816,
    )
    for delivery, _query, geo in rows:
        assert geo.has_coords, delivery.get("customer_name")
        assert geo.precision == "housenumber", (
            delivery.get("customer_name"), geo.precision, geo.matched_text,
        )


@pytest.mark.real_geo
def test_placeholder_miami_geocodifica_las_6_puertas():
    result = _normalize(
        EXAMPLES / "geocode-truth" / "miami_whatsapp_6.txt", "US",
    )
    assert result.report["deliveries"] == 6
    rows = _geocode_extracted(
        result, city="Miami", country="United States",
        lat=25.77427, lon=-80.19366,
    )
    for delivery, _query, geo in rows:
        assert geo.has_coords, delivery.get("customer_name")
        assert geo.precision == "housenumber", (
            delivery.get("customer_name"), geo.precision, geo.matched_text,
        )
        assert "71st" not in (geo.matched_text or "")
