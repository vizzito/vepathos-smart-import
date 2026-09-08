"""Parsing y scoring de direcciones detras de la abstraccion `AddressParser`."""
import pytest

from smart_import.addresses import (
    AddressCandidateScorer, EnhancingAddressParser, HeuristicAddressParser,
    HybridAddressParser, LibpostalAddressParser, ParsedAddress,
    build_address_parser, describe_parsers, is_installed,
)
from smart_import.config import Config

ACCEPT = Config.from_env().address_accept_threshold


@pytest.fixture(scope="module")
def scorer():
    return AddressCandidateScorer()


@pytest.fixture(scope="module")
def parser():
    return HeuristicAddressParser()


# ---------- scoring ----------

@pytest.mark.parametrize("texto", ["Salutos", "Despacho", "Hola chicos!", "Gracias"])
def test_la_basura_puntua_cero(scorer, texto):
    assert scorer.score(texto).score == 0.0


@pytest.mark.parametrize("texto", [
    "Av. Corrientes 100 en CABA", "Darwin 1395 Villa Crespo",
    "Lavalle 507 Microcentro", "11 de Septiembre Nro 1913",
    "Av. Elcano 2098 esquina Superi, barrio Colegiales",
    "23 MG Road, Bengaluru 560001",
])
def test_una_direccion_de_verdad_supera_el_umbral(scorer, texto):
    assert scorer.score(texto).score >= ACCEPT


@pytest.mark.parametrize("localidad", ["Belgrano", "Palermo", "Mumbai"])
def test_una_localidad_sola_no_es_una_direccion(scorer, localidad):
    """Puede ser un barrio; no es un destino preciso."""
    assert scorer.score(localidad).score < ACCEPT


def test_el_score_viene_con_sus_razones(scorer):
    result = scorer.score("Av. Corrientes 100, CABA")
    assert result.score > 0
    razones = " | ".join(result.evidence)
    assert "numero de puerta" in razones and "token de via" in razones


def test_el_gate_de_geocoding_usa_el_mismo_scorer(scorer):
    assert scorer.is_geocodable("Av. Corrientes 100, CABA", ACCEPT)
    assert not scorer.is_geocodable("Salutos", ACCEPT)


# ---------- parser heuristico ----------

@pytest.mark.parametrize("texto,esperado", [
    ("Av. Corrientes 100", {"road": "Av. Corrientes", "house_number": "100"}),
    ("Malabia 1136, Palermo", {"road": "Malabia", "house_number": "1136",
                               "suburb": "Palermo"}),
    ("Av. Cordoba 248 piso 3 B", {"road": "Av. Cordoba", "house_number": "248",
                                  "unit": "3 B"}),
    ("11 de Septiembre Nro 1913", {"road": "11 de Septiembre",
                                   "house_number": "1913"}),
    ("23 MG Road, Bengaluru 560001", {"road": "MG Road", "house_number": "23",
                                      "postcode": "560001"}),
    # Fase 0: ordinales EN, BR con coma, todos los segmentos, via numerada
    ("1171 1st Ave, Seattle", {"road": "1st Ave", "house_number": "1171"}),
    ("350 NE 1st Ave", {"road": "NE 1st Ave", "house_number": "350"}),
    ("1200 NW 7th Ave", {"road": "NW 7th Ave", "house_number": "1200"}),
    ("Brickell 350 NE 1st Ave", {"road": "NE 1st Ave", "house_number": "350"}),
    ("Av. Paulista, 1578", {"road": "Av. Paulista", "house_number": "1578"}),
    ("Apt 4B, 350 5th Ave", {"road": "5th Ave", "house_number": "350",
                             "unit": "4B"}),
    ("Calle 50 nro 1234", {"road": "Calle 50", "house_number": "1234"}),
    ("Yerbal, CABA, Argentina", {"road": "Yerbal"}),
    ("Av. Cabildo, CABA", {"road": "Av. Cabildo"}),
    # Unidad SIN etiqueta pegada a la altura. `_unit_re` exige prefijo, asi que
    # estas quedaban sin unit; y en texto libre el segmento cortaba el span.
    ("Av Cabildo 900 1A", {"road": "Av Cabildo", "house_number": "900",
                           "unit": "1A"}),
    ("Av Corrientes 1800 2B", {"road": "Av Corrientes", "house_number": "1800",
                               "unit": "2B"}),
    ("Av Corrientes 1800 PB", {"road": "Av Corrientes", "house_number": "1800",
                               "unit": "PB"}),
    ("100 Biscayne Blvd 2B, Miami", {"road": "Biscayne Blvd",
                                     "house_number": "100", "unit": "2B"}),
])
def test_componentes_reconocidos(parser, texto, esperado):
    components = parser.parse(texto).components
    for name, value in esperado.items():
        assert components.get(name) == value, components


@pytest.mark.parametrize("texto", [
    "Av Cabildo 174, 2 u",          # paqueteria: 2 unidades
    "Av Cabildo 174, 3 kg",
    "Av Cabildo 174, 2 cajas",
    "Ruta 2 km 5",
])
def test_la_paqueteria_no_se_confunde_con_una_unidad(parser, texto):
    """La unidad sin etiqueta se acepta SOLO con la letra pegada al numero.

    '2 u' y '3 kg' viven en el mismo lugar de la frase que un '2B', y son
    cantidad de bultos o peso. Aceptar la forma separada las metia como unit.
    """
    assert not parser.parse(texto).get("unit")


def test_la_altura_no_se_parte_cuando_hay_una_unidad_atras(parser):
    """Regresion: la altura completa, no su primer digito.

    Es el peor error posible del extractor porque no pierde un dato: pone un pin
    con confianza en la direccion equivocada. 'Av Corrientes 1' esta a treinta
    cuadras de 'Av Corrientes 1800'.
    """
    for texto in ("Av Corrientes 1800 2B", "Av Corrientes 1800 2B, Palermo, CABA",
                  "Av Cabildo 900 1A, Belgrano, CABA"):
        parsed = parser.parse(texto)
        assert parsed.get("house_number") in ("1800", "900"), (
            f"{texto!r} -> altura {parsed.get('house_number')!r}: {parsed.components}")


def test_no_asume_calle_mas_numero(parser):
    """Una direccion india es unit + suburb + landmark + city + pincode."""
    parsed = parser.parse(
        "Flat 14B, Shanti Nagar, Near Hanuman Temple, Andheri East, Mumbai 400069")
    assert parsed.get("unit") == "14B"
    assert parsed.get("postcode") == "400069"
    assert parsed.get("landmark") == "Hanuman Temple"
    assert parsed.is_precise


def test_una_altura_en_el_medio_no_es_un_codigo_postal(parser):
    assert parser.parse("Darwin 1395 Villa Crespo").get("postcode") == ""


def test_la_parse_conserva_el_texto_original(parser):
    texto = "Av. Pueyrredon 359, Recoleta"
    assert parser.parse(texto).text == texto


# ---------- abstraccion ----------

def test_el_default_es_heuristico_y_no_necesita_nada():
    parser = build_address_parser(Config.from_env())
    assert parser.name == "heuristic"
    assert parser.available()


def test_libpostal_apagado_no_rompe_nada():
    parser = LibpostalAddressParser()
    assert parser.available() is is_installed()
    parsed = parser.parse("Av. Corrientes 100")
    assert isinstance(parsed, ParsedAddress)
    assert parsed.text == "Av. Corrientes 100"


def test_con_la_flag_prendida_pero_sin_libreria_se_degrada_a_reglas(monkeypatch):
    from smart_import.addresses import factory as factory_mod
    monkeypatch.setattr(factory_mod, "is_installed", lambda: False)
    cfg = Config.from_env().replace(address_parser="enhanced", libpostal_enabled=True)
    parser = build_address_parser(cfg)
    assert parser.available()
    assert parser.name == "heuristic"
    assert parser.parse("Av. Corrientes 100").get("house_number") == "100"


def test_el_hibrido_completa_lo_que_falta_sin_pisar_lo_resuelto():
    class Fake(HeuristicAddressParser):
        name = "fake"

        def parse(self, text, context=None):
            return ParsedAddress(text=text, parser="fake",
                                 components={"road": "OTRA", "city": "Mumbai"})

    hybrid = HybridAddressParser(HeuristicAddressParser(), Fake())
    parsed = hybrid.parse("Av. Corrientes 100")
    assert parsed.get("road") == "Av. Corrientes"      # el primario manda
    assert parsed.get("city") == "Mumbai"              # el secundario completa
    assert parsed.parser == "hybrid"


def test_el_enhancer_es_un_agregado_no_un_reemplazo():
    class Fake(HeuristicAddressParser):
        name = "fake"

        def available(self):
            return True

        def parse(self, text, context=None):
            return ParsedAddress(text=text, parser="fake",
                                 components={"road": "OTRA", "city": "Mumbai"})

    enhanced = EnhancingAddressParser(HeuristicAddressParser(), Fake())
    # Con road buena: no consulta al enhancer
    good = enhanced.parse("Av. Corrientes 100")
    assert good.get("road") == "Av. Corrientes"
    assert good.parser == "heuristic"
    # Sin road: consulta y completa
    weak = enhanced.parse("Near Hanuman Temple, Mumbai 400069")
    assert weak.parser == "enhanced"
    assert weak.get("city") == "Mumbai"


def test_health_puede_reportar_que_parser_esta_activo():
    described = describe_parsers(Config.from_env())
    assert described["active"] == "heuristic"
    assert described["libpostal_enabled"] is False
    assert described["libpostal_as_enhancer"] is False
    assert isinstance(described["libpostal_installed"], bool)


# ---------- una ciudad pegada a una altura es una calle ----------

@pytest.mark.parametrize("address, no_debe_aparecer", [
    ("Av Callao 1219, Argentina", "Peru"),
    ("Asuncion 2135, Argentina", "Paraguay"),
    ("Albania 4557, Argentina", "United States"),
    ("Lavalle 3690, Buenos Aires", "Slovakia"),
    ("Bolivia 200, Buenos Aires", "Bolivia,"),
])
def test_la_calle_no_arrastra_el_pais_de_su_homonima(address, no_debe_aparecer):
    """Media ciudad del mundo comparte nombre con una calle de otra.

    El gazetteer no las distingue; la posicion si: nadie escribe la ciudad
    pegada al numero de puerta. Sin esto la query sale a buscar la direccion
    al pais equivocado.
    """
    from smart_import.normalization.address import maximize_address_for_geocode
    salida = maximize_address_for_geocode(address, phone_region="AR")
    assert no_debe_aparecer not in salida, salida


@pytest.mark.parametrize("address, debe_aparecer", [
    ("100 Biscayne Blvd, Downtown Miami", "United States"),
    ("200 Ocean Dr, South Beach, Miami Beach, FL", "United States"),
    ("14 De Julio 840, B7000 Tandil, Buenos Aires", "Argentina"),
    ("Av Corrientes 100, Palermo, CABA", "Argentina"),
])
def test_la_ciudad_de_verdad_sigue_enriqueciendo(address, debe_aparecer):
    """La regla mira el vecino inmediato: un CPA ('B7000') no es una altura."""
    from smart_import.normalization.address import maximize_address_for_geocode
    assert debe_aparecer in maximize_address_for_geocode(address, phone_region="AR")
