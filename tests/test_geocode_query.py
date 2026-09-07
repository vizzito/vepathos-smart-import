"""Query de geocode: no muta address; compose + clean + enhance opt-in."""
from smart_import.geocoding.base import STATUS_LOW, STATUS_MATCHED, GeocodeResult
from smart_import.geocoding.depot_context import DepotContext
from smart_import.geocoding.query import (
    build_geocode_query, cleaned_query, is_weak_result, locality_tokens_from_row,
    pick_better, result_rank,
)


def test_locality_tokens_desde_fila_shopify_style():
    row = {
        "address": "Cabildo 3121",
        "city": "Belgrano",
        "country": "AR",
        "postcode": "1428",
    }
    tokens = locality_tokens_from_row(row)
    assert "Belgrano" in tokens
    assert tokens[-1] in ("Argentina", "AR")
    assert tokens.index("1428") < len(tokens) - 1


def test_locality_tokens_desde_fila_tiendanube():
    row = {
        "address": "Av. Rivadavia 4800",
        "locality": "Caballito",
        "city": "CABA",
        "zipcode": "1424",
        "country": "AR",
    }
    tokens = locality_tokens_from_row(row)
    assert tokens == ["Caballito", "CABA", "1424", "Argentina"]


def test_query_patricias_argentinas_no_es_el_pais():
    """La calle 'Patricias Argentinas' no debe ocultar ni adelantar el país."""
    from smart_import.normalization.address import already_present

    street = "Avenida Patricias Argentinas"
    assert already_present(street, "Argentina") is False
    assert already_present(f"{street}, Argentina", "Argentina") is True

    depot = DepotContext(city="CABA", country="Argentina")
    sent = build_geocode_query(street, depot=depot)
    assert sent == "Avenida Patricias Argentinas, CABA, Argentina"
    sent_con_pais = build_geocode_query(f"{street}, Argentina", depot=depot)
    assert sent_con_pais == "Avenida Patricias Argentinas, CABA, Argentina"


def test_query_compone_sin_pisar_display():
    depot = DepotContext(city="CABA", country="Argentina")
    display = "Cabildo 3121, Piso 12, Belgrano"
    query = build_geocode_query(display, row={"city": "Belgrano"}, depot=depot)
    assert display == "Cabildo 3121, Piso 12, Belgrano"  # intacto
    assert "Belgrano" in query
    assert query.endswith("Argentina")
    parts = [p.strip().casefold() for p in query.split(",")]
    assert parts.index("caba") < parts.index("argentina")


def test_cleaned_query_saca_piso_no_barrio():
    q = "Cabildo 3121, Piso 12, Belgrano, CABA"
    out = cleaned_query(q)
    assert out is not None
    assert "Piso" not in out
    assert "Belgrano" in out
    assert "Cabildo 3121" in out


def test_enhance_reescribe_road_house():
    depot = DepotContext(city="CABA", country="Argentina")
    display = "Cabildo 3121, Piso 12, Belgrano"
    plain = build_geocode_query(display, depot=depot, enhance=False)
    enhanced = build_geocode_query(display, depot=depot, enhance=True)
    assert "Piso" in plain or "piso" in plain.lower() or "Belgrano" in plain
    # enhance prioriza calle+altura; piso no deberia ser el ancla
    assert "3121" in enhanced
    assert "Cabildo" in enhanced or "cabildo" in enhanced.lower()


def test_pick_better_prefiere_housenumber():
    street = GeocodeResult(
        status=STATUS_MATCHED, confidence=0.90, precision="street",
        lat=-34.56, lon=-58.45)
    house = GeocodeResult(
        status=STATUS_LOW, confidence=0.80, precision="housenumber",
        lat=-34.55, lon=-58.46)
    assert result_rank(house)[0] > result_rank(street)[0]  # precision primero
    assert pick_better(street, house).precision == "housenumber"


def test_is_weak_street_match():
    r = GeocodeResult(status=STATUS_MATCHED, confidence=0.88, precision="street",
                      lat=-34.5, lon=-58.4)
    assert is_weak_result(r, review_band=0.75)
    strong = GeocodeResult(status=STATUS_MATCHED, confidence=0.95,
                           precision="housenumber", lat=-34.5, lon=-58.4)
    assert not is_weak_result(strong, review_band=0.75)
