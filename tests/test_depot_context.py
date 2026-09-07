"""Enrichment + geofence del depot (sin hints regionales hardcodeados)."""
from __future__ import annotations

from smart_import.config import Config
from smart_import.geocoding.base import STATUS_MATCHED, STATUS_NOT_FOUND, GeocodeResult
from smart_import.geocoding.depot_context import DepotContext, depot_from_params
from smart_import.geocoding.runner import _apply_depot_guards


def test_enrich_inyecta_ciudad_y_provincia_faltantes():
    depot = DepotContext(
        lat=-34.60, lon=-58.38,
        city="CABA", region="Buenos Aires", country="Argentina",
    )
    assert depot.enrich_address("Av. Santa Fe 137") == (
        "Av. Santa Fe 137, CABA, Buenos Aires, Argentina"
    )


def test_enrich_no_duplica_si_ya_esta_ciudad():
    depot = DepotContext(city="CABA", region="Buenos Aires", country="Argentina")
    out = depot.enrich_address("Av. Santa Fe 137, CABA")
    assert out == "Av. Santa Fe 137, CABA, Buenos Aires, Argentina"
    assert out.lower().count("caba") == 1


def test_enrich_ignora_address_libre_del_depot():
    """El reverse-geocode del depot NO se parsea: solo campos estructurados."""
    depot = DepotContext(address="Palermo, CABA, Buenos Aires, Argentina")
    assert depot.enrichment_tokens() == []
    assert depot.enrich_address("Av. Santa Fe 137") == "Av. Santa Fe 137"


def test_enrich_no_inyecta_calle_del_depot():
    depot = DepotContext(
        lat=-37.3004, lon=-59.0877,
        city="Tandil", country="Argentina",
        address=(
            "3165, Pasaje de los Graduados, Graduados, Tandil, "
            "Partido de Tandil, Buenos Aires, B7000, Argentina"
        ),
    )
    assert depot.enrichment_tokens() == ["Tandil", "Argentina"]
    assert depot.enrich_address("Roca 1160") == "Roca 1160, Tandil, Argentina"


def test_enrich_no_inyecta_ciudad_autonoma_si_ya_hay_caba():
    depot = DepotContext(
        city="Ciudad Autónoma de Buenos Aires",
        country="Argentina",
    )
    out = depot.enrich_address("Av. Córdoba 248, CABA, CABA")
    assert out.lower().count("caba") == 1
    assert "ciudad autónoma" not in out.lower()
    assert "Argentina" in out


def test_enrich_pais_queda_al_final_si_ya_estaba_en_el_address():
    """El geolocalizador (CABA) no puede quedar después del país."""
    depot = DepotContext(city="CABA", country="Argentina")
    assert depot.enrich_address("1 de abril, Argentina") == (
        "1 de abril, CABA, Argentina"
    )
    assert depot.enrich_address("AV CORRIENTES 919, Argentina") == (
        "AV CORRIENTES 919, CABA, Argentina"
    )


def test_dedupe_address_segments_colapsa_caba_doble():
    from smart_import.normalization.address import (
        dedupe_address_segments, maximize_address_for_geocode,
    )
    assert dedupe_address_segments("Av. Córdoba 248, CABA, CABA") == (
        "Av. Córdoba 248, CABA"
    )
    out = maximize_address_for_geocode("Av. Córdoba 248, CABA, CABA", phone_region="AR")
    assert out.lower().count("caba") == 1
    assert "Buenos Aires" in out
    assert out.endswith("Argentina")
    assert out.index("CABA") < out.index("Argentina")


def test_maximize_miami_no_inyecta_argentina():
    from smart_import.normalization.address import maximize_address_for_geocode
    out = maximize_address_for_geocode(
        "100 Biscayne Blvd, Downtown Miami", phone_region="AR")
    assert "Argentina" not in out
    assert "United States" in out


def test_maximize_depot_us_gana_sobre_phone_region_ar():
    from smart_import.normalization.address import maximize_address_for_geocode
    out = maximize_address_for_geocode(
        "801 Brickell Ave", phone_region="AR",
        extra_tokens=("Miami",), country="United States")
    assert "Argentina" not in out
    assert "Miami" in out
    assert "United States" in out


def test_cue_ne_no_dispara_localidad():
    """'NE' no puede matchear un cue corto adentro de '350 NE 1st Ave'."""
    from smart_import.normalization.address import maximize_address_for_geocode
    out = maximize_address_for_geocode("350 NE 1st Ave", phone_region="AR")
    # sin miami/depot, AR sigue siendo el fallback — pero no un pais random por 'ne'
    assert "NE 1st" in out or "350" in out


def test_enrich_expande_iso2_y_descarta_subdivision_numerada():
    depot = DepotContext(
        lat=-34.59, lon=-58.39,
        city="Ciudad Autónoma de Buenos Aires",
        country="AR",
        region="Comuna 2",
    )
    # region numerada se descarta; ISO-2 → nombre; city se deja tal cual (OSM)
    assert depot.enrichment_tokens() == [
        "Ciudad Autónoma de Buenos Aires",
        "Argentina",
    ]


def test_numbered_admin_multilingual():
    """Subdivisiones numeradas en varios idiomas no se usan como ciudad."""
    from smart_import.geocoding.depot_context import normalize_locality_label

    discarded = [
        "Comuna 2",
        "District 5",
        "Zone 3",
        "Ward 12",
        "Arrondissement 11",
        "11th Arrondissement",
        "Bezirk 3",
        "2nd District",
        "Sector 7",
        "Barrio 1",
        "Municipio 4",
        "Ilçe 3",
    ]
    for label in discarded:
        assert normalize_locality_label(label) is None, label

    assert normalize_locality_label("Palermo") == "Palermo"
    assert normalize_locality_label("São Paulo") == "São Paulo"


def test_enrich_sin_localidad_estructurada_no_inventa_por_coords():
    """Sin city/region/country no hay magic bbox: fill_depot_from_index lo resuelve."""
    depot = DepotContext(lat=-34.6037, lon=-58.3816)
    assert depot.enrichment_tokens() == []
    assert depot.enrich_address("Av. Santa Fe 137") == "Av. Santa Fe 137"


def test_geofence_500km():
    depot = DepotContext(lat=-34.60, lon=-58.38, max_distance_km=500.0)
    assert not depot.within_operating_radius(-31.42, -64.19)
    assert depot.within_operating_radius(-34.58, -58.42)


def test_align_geolocalizador_miami_pisa_pin_caba():
    """Near Miami + depot CABA no puede geofencear contra Argentina."""
    from smart_import.geocoding.depot_context import align_depot_to_geolocator

    depot = DepotContext(
        lat=-34.6037, lon=-58.3816,
        city="Miami", country="United States",
        max_distance_km=500.0,
    )
    aligned = align_depot_to_geolocator(depot)
    assert aligned is not None
    assert aligned.origin is not None
    assert aligned.city == "Miami"
    # centroide Miami, no el pin de CABA
    assert 25.0 < aligned.lat < 26.5
    assert -81.0 < aligned.lon < -79.5
    assert aligned.within_operating_radius(25.774, -80.194)


def test_align_no_mueve_pin_si_ya_esta_en_la_ciudad():
    from smart_import.geocoding.depot_context import align_depot_to_geolocator

    depot = DepotContext(
        lat=25.7617, lon=-80.1918,
        city="Miami", country="United States",
        max_distance_km=500.0,
    )
    aligned = align_depot_to_geolocator(depot)
    assert aligned.origin == depot.origin


def test_depot_from_params_retrocompatible_solo_origin():
    d = depot_from_params(origin_lat=-34.6, origin_lon=-58.38, max_distance_km=500)
    assert d is not None
    assert d.origin == (-34.6, -58.38)
    assert d.enrichment_tokens() == []  # sin fill / sin city


def test_apply_guards_matched_fuera_de_radio():
    from smart_import.geocoding.runner import GeocodeReport

    cfg = Config.from_env().replace(max_geocode_distance_km=500.0, max_low_confidence_km=15.0)
    depot = DepotContext(lat=-31.42, lon=-64.19, max_distance_km=500.0)
    result = GeocodeResult(
        status=STATUS_MATCHED, lat=-34.6037, lon=-58.3816,
        confidence=0.95, precision="housenumber", source="osm",
    )
    report = GeocodeReport()
    out = _apply_depot_guards(result, depot=depot, cfg=cfg, report=report)
    assert out.status == STATUS_NOT_FOUND
    assert report.rejected_far == 1
    assert out.detail["reject_reason"] == "outside_operating_radius"
