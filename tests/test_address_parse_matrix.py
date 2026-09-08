"""Parser + compose + query de geocode: muchas formas de entrada.

Cubre lo que termina en el esquema (`address`, `house_number`, ciudad) y lo que
se le manda al geocoder (con y sin depot). Sin PBF: es texto, no metros.
"""
from __future__ import annotations

import pytest

from smart_import.addresses import HeuristicAddressParser
from smart_import.geocoding.address import parse
from smart_import.geocoding.depot_context import DepotContext, depot_from_params
from smart_import.geocoding.query import build_geocode_query
from smart_import.normalization.address import compose_address_from_parts

PARSER = HeuristicAddressParser()


# ---------- heuristic: road + altura + extras ----------

@pytest.mark.parametrize("texto,road,house", [
    ("Av. Corrientes 100", "Av. Corrientes", "100"),
    ("AV CORRIENTES 919", "AV CORRIENTES", "919"),
    ("Av. Rivadavia 4800, Caballito", "Av. Rivadavia", "4800"),
    ("Av. Díaz Vélez 4200", "Av. Díaz Vélez", "4200"),
    ("Av. Juan de Garay 03845", "Av. Juan de Garay", "03845"),
    ("11 de Septiembre 1735", "11 de Septiembre", "1735"),
    ("11 de septiembre 1735, CABA", "11 de septiembre", "1735"),
    ("24 de Noviembre 1234", "24 de Noviembre", "1234"),
    ("Malabia 1136, Palermo", "Malabia", "1136"),
    ("Defensa 800", "Defensa", "800"),
    ("Thames 1800", "Thames", "1800"),
    ("Maipú 400, Buenos Aires", "Maipú", "400"),
    ("Av. del Libertador 900", "Av. del Libertador", "900"),
    ("Av. Alicia Moreau de Justo 200", "Av. Alicia Moreau de Justo", "200"),
    ("350 NE 1st Ave", "NE 1st Ave", "350"),
    ("350 NE 1st Ave, Miami", "NE 1st Ave", "350"),
    ("1200 NW 7th Ave, Miami, United States", "NW 7th Ave", "1200"),
    ("801 Brickell Ave, Miami", "Brickell Ave", "801"),
    ("100 Biscayne Blvd, Downtown Miami", "Biscayne Blvd", "100"),
    ("200 Ocean Dr, Miami", "Ocean Dr", "200"),
    ("1500 Collins Ave Miami Beach", "Collins Ave Miami Beach", "1500"),
    ("1171 1st Ave, Seattle", "1st Ave", "1171"),
    ("Apt 4B, 350 5th Ave", "5th Ave", "350"),
    ("Av. Paulista, 1578", "Av. Paulista", "1578"),
    ("23 MG Road, Bengaluru 560001", "MG Road", "23"),
    ("Calle 50 nro 1234", "Calle 50", "1234"),
    ("Tucumán 00875", "Tucumán", "00875"),
])
def test_heuristic_aisla_calle_y_altura(texto, road, house):
    got = PARSER.parse(texto).components
    assert got.get("road") == road, got
    assert got.get("house_number") == house, got


@pytest.mark.parametrize("texto,house", [
    ("350 NE 1st Ave", "350"),
    ("NE 1st Ave 350, Miami", "350"),
    ("Brickell 350 NE 1st Ave", "350"),
])
def test_ordinal_us_no_es_altura(texto, house):
    """'1st' no puede resolverse a house=1."""
    assert PARSER.parse(texto).get("house_number") == house
    assert parse(texto).house_number == house


@pytest.mark.parametrize("texto", [
    "CABA",
    "Miami",
    "Palermo",
    "Hola chicos",
    "",
])
def test_sin_calle_no_inventa_altura(texto):
    assert not PARSER.parse(texto).get("house_number")


@pytest.mark.parametrize("texto,road,house", [
    ("mensajero1 Av Corrientes 1800 2B", "Av Corrientes", "1800"),
    ("@mensajero1 llevar a Av Corrientes 1800", "Av Corrientes", "1800"),
    ("movil3 Av Cabildo 900, Belgrano, CABA", "Av Cabildo", "900"),
    ("Pedido123 Av Corrientes 1800", "Av Corrientes", "1800"),
    ("zona2 entregar en Av Santa Fe 3100", "Av Santa Fe", "3100"),
    ("Ruta1 Av Corrientes 1800", "Av Corrientes", "1800"),
    ("T1 Av Corrientes 1800", "Av Corrientes", "1800"),
    ("sector5 Av Cabildo 900", "Av Cabildo", "900"),
    ("ruta1 350 NE 1st Ave, Miami", "NE 1st Ave", "350"),
])
def test_una_palabra_terminada_en_digito_no_es_la_altura(texto, road, house):
    """El peor error posible: pin CONFIADO en la direccion equivocada.

    El lookbehind de los patrones number+road era `(?<!\\d)`, o sea que la
    altura solo tenia prohibido venir pegada a otro digito, no a una letra. El
    '1' de 'mensajero1' pasaba, armaba el candidato 'Av Corrientes' + '1',
    empataba en boost con el candidato correcto (los dos traen el token 'av') y
    el desempate por posicion se lo daba al falso, que empieza antes.

    'Av Corrientes 1' esta a treinta cuadras de 'Av Corrientes 1800', y sale con
    la misma confianza. Dispara con todo lo que abunda en un paste de despacho:
    ids de mensajero, moviles, rutas, zonas, numeros de pedido.
    """
    parsed = PARSER.parse(texto)
    assert parsed.get("house_number") == house, parsed.components
    assert parsed.get("road") == road, parsed.components


@pytest.mark.parametrize("texto,house", [
    ("Nº1234 Calle Falsa", "1234"),
    ("N°1234 Calle Falsa", "1234"),
    ("Calle Falsa Nº1234", "1234"),
])
def test_el_marcador_de_numero_si_puede_estar_pegado_a_la_altura(texto, house):
    """`(?<!\\w)` a secas se comia esta forma: Python cuenta 'º' como \\w."""
    assert PARSER.parse(texto).get("house_number") == house


# ---------- compose al esquema (partes → address) ----------

@pytest.mark.parametrize("parts,needle", [
    ({"address": "Av. Rivadavia", "house_number": "4800", "city": "CABA",
      "country": "AR"}, "Av. Rivadavia 4800"),
    ({"address": "Av. Nazca", "house_number": "400", "zone": "Flores",
      "city": "CABA"}, "Nazca 400"),
    ({"address": "Defensa", "house_number": "800", "city": "CABA",
      "postcode": "1065"}, "Defensa 800"),
    ({"address": "NE 1st Ave", "house_number": "350", "city": "Miami",
      "country": "United States"}, "350 NE 1st Ave"),
    ({"address": "NW 7th Ave", "house_number": "1200", "city": "Miami",
      "country": "US"}, "1200 NW 7th Ave"),
    ({"address": "Av. Rivadavia 4800", "house_number": "4800",
      "city": "CABA"}, "Av. Rivadavia 4800"),
])
def test_compose_partes_al_esquema(parts, needle):
    out = compose_address_from_parts(parts)
    assert out is not None
    assert needle in out


def test_compose_us_altura_antes_del_ordinal():
    out = compose_address_from_parts({
        "address": "NE 1st Ave", "house_number": "350",
        "city": "Miami", "country": "United States",
    })
    assert out.index("350") < out.index("NE")
    assert out.endswith("United States")


def test_compose_ar_altura_despues_de_la_calle():
    out = compose_address_from_parts({
        "address": "Av. Corrientes", "house_number": "919",
        "city": "CABA", "country": "Argentina",
    })
    assert out.index("Corrientes") < out.index("919")


def test_compose_sin_calle_no_arma_solo_ciudad():
    assert compose_address_from_parts({"city": "CABA", "country": "AR"}) is None


def test_compose_no_duplica_caba_ni_argentina():
    out = compose_address_from_parts({
        "address": "Av. Cabildo 2400, CABA, Argentina",
        "city": "CABA", "country": "Argentina",
    })
    assert out.count("CABA") == 1
    assert out.count("Argentina") == 1
    assert out.index("CABA") < out.index("Argentina")


# ---------- query interna (depot / fila / faltantes) ----------

CABA = DepotContext(lat=-34.6037, lon=-58.3816, city="CABA", country="Argentina")
MIAMI = DepotContext(lat=25.77427, lon=-80.19366, city="Miami",
                     country="United States", region="FL")


@pytest.mark.parametrize("address,depot,row,must,must_not", [
    ("Av. Corrientes 919", CABA, None,
     ("Corrientes", "919", "CABA", "Argentina"), ()),
    ("Av. Corrientes 919, CABA", CABA, None,
     ("CABA", "Argentina"), ()),
    ("350 NE 1st Ave", MIAMI, None,
     ("350", "NE 1st", "Miami"), ("Argentina", "CABA")),
    ("350 NE 1st Ave, Miami, United States", MIAMI, None,
     ("Miami", "United States"), ("Argentina",)),
    ("Defensa 800", CABA, {"city": "CABA", "postcode": "1065"},
     ("Defensa", "800", "CABA"), ()),
    ("Thames 1800", CABA, {"zone": "Palermo", "city": "Buenos Aires"},
     ("Thames", "1800", "Palermo"), ()),
    ("Av. Rivadavia", CABA, {"house_number": "4800"},
     ("Rivadavia", "CABA"), ()),
    ("Maipú 400, Buenos Aires", None, None,
     ("Maipú", "400", "Buenos Aires"), ("CABA",)),
    ("100 Biscayne Blvd, Downtown Miami", MIAMI, None,
     ("Biscayne", "Miami"), ("Argentina",)),
    ("Avenida Patricias Argentinas, Argentina", CABA, None,
     ("Patricias Argentinas", "CABA", "Argentina"), ()),
])
def test_query_geocode_con_y_sin_depot(address, depot, row, must, must_not):
    sent = build_geocode_query(address, row=row, depot=depot)
    low = sent.casefold()
    for token in must:
        assert token.casefold() in low, (token, sent)
    for token in must_not:
        assert token.casefold() not in low, (token, sent)
    parts = [p.strip().casefold() for p in sent.split(",")]
    if "argentina" in parts and "caba" in parts:
        assert parts.index("caba") < parts.index("argentina"), sent


def test_query_no_inyecta_argentina_si_el_depot_es_miami():
    sent = build_geocode_query("350 NE 1st Ave", depot=MIAMI)
    assert "Argentina" not in sent
    assert "Miami" in sent


def test_query_depot_desde_params_usa_usa():
    depot = depot_from_params(
        origin_lat=25.77, origin_lon=-80.19,
        depot_city="Miami", depot_country="USA",
    )
    sent = build_geocode_query("801 Brickell Ave", depot=depot)
    assert "Brickell" in sent
    assert "Miami" in sent
    assert "Argentina" not in sent


def test_query_sin_depot_no_agrega_ciudad():
    sent = build_geocode_query("Av. Corrientes 919")
    assert sent == "Av. Corrientes 919"


def test_query_fila_sin_ciudad_no_rompe():
    sent = build_geocode_query("Chile 900", row={"customer_name": "Elena"})
    assert "Chile 900" in sent
