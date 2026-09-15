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
    # OpenAddresses CABA: comas internas = nombre de via, no barrio
    ("CALDERON DE LA BARCA, PEDRO, 3645",
     {"road": "CALDERON DE LA BARCA PEDRO", "house_number": "3645"}),
    ("3645, CALDERON DE LA BARCA, PEDRO",
     {"road": "CALDERON DE LA BARCA PEDRO", "house_number": "3645"}),
    ("FLORES, VENANCIO, Gral., 185",
     {"road": "FLORES VENANCIO Gral", "house_number": "185"}),
    ("185 FLORES, VENANCIO, Gral.",
     {"road": "FLORES VENANCIO Gral", "house_number": "185"}),
    ("PENA, DAVID, DR., 4256",
     {"road": "PENA DAVID DR", "house_number": "4256"}),
    ("PLAZA, VICTORINO DE LA, DR., 1857",
     {"road": "PLAZA VICTORINO DE LA DR", "house_number": "1857"}),
    ("VICTORICA, BENJAMIN, GENERAL, AV., 2373",
     {"road": "VICTORICA BENJAMIN GENERAL AV", "house_number": "2373"}),
    ("HERRERA, LUIS A., de, 3474",
     {"road": "HERRERA LUIS A de", "house_number": "3474"}),
    ("URIBURU JOSE E., Pr, 576",
     {"road": "URIBURU JOSE E Pr", "house_number": "576"}),
    # No OA-compound: el 1er segmento ya es calle+altura; 1425 es CP
    ("Av. Pueyrredon 359, Recoleta, 1425",
     {"road": "Av. Pueyrredon", "house_number": "359", "postcode": "1425"}),
    # Grilla EN (NYC OA): via numerada, cardinal sin ordinal, altura con guion
    ("68 ST, 1445", {"road": "68 ST", "house_number": "1445"}),
    ("1445 68 ST", {"road": "68 ST", "house_number": "1445"}),
    ("E 2 ST, 2114", {"road": "E 2 ST", "house_number": "2114"}),
    ("2114 E 2 ST", {"road": "E 2 ST", "house_number": "2114"}),
    ("5 AVE, 4704", {"road": "5 AVE", "house_number": "4704"}),
    ("4704 5 AVE", {"road": "5 AVE", "house_number": "4704"}),
    ("BCH 26 ST, 195", {"road": "BCH 26 ST", "house_number": "195"}),
    ("150 PL, 6-19", {"road": "150 PL", "house_number": "6-19"}),
    ("JAMAICA AVE, 108-15", {"road": "JAMAICA AVE", "house_number": "108-15"}),
    ("108-15 JAMAICA AVE", {"road": "JAMAICA AVE", "house_number": "108-15"}),
    ("121-06 LIBERTY AVE", {"road": "LIBERTY AVE", "house_number": "121-06"}),
    ("164 ST, 42-21", {"road": "164 ST", "house_number": "42-21"}),
    ("BROADWAY, 366, 11211",
     {"road": "BROADWAY", "house_number": "366", "postcode": "11211"}),
    ("JAMAICA AVE, 108-15, 11418",
     {"road": "JAMAICA AVE", "house_number": "108-15", "postcode": "11418"}),
    ("164 ST, 42-21, 11358",
     {"road": "164 ST", "house_number": "42-21", "postcode": "11358"}),
    ("150 PL, 6-19, 11357",
     {"road": "150 PL", "house_number": "6-19", "postcode": "11357"}),
    # Cardinal = palabra entera: no robar la 's' de 'states' ni la 'N' de Brighton
    ("united states 219 ST 116-41",
     {"road": "219 ST", "house_number": "116-41"}),
    ("BRIGHTON 7 ST, 2918",
     {"road": "BRIGHTON 7 ST", "house_number": "2918"}),
    ("2918 BRIGHTON 7 ST",
     {"road": "BRIGHTON 7 ST", "house_number": "2918"}),
    ("BAY 40 ST, 245",
     {"road": "BAY 40 ST", "house_number": "245"}),
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


# ---------- GeoNames: un apellido que tambien es ciudad no cambia el pais ----------

@pytest.mark.parametrize("address, no_debe_aparecer", [
    ("Juan Lopez Gorriti 4500, 3B", "Philippines"),
    ("Pedro Castro Juan B Justo 4500", "Brazil"),
    ("Carlos Rodriguez Rivadavia 5000, 3B", "Philippines"),
    ("Av Cabildo y Juramento", "Chile"),
    ("Lavalle 3690, Martin Diaz", "Slovakia"),
])
def test_una_pista_de_geonames_adentro_de_la_calle_no_es_localidad(address, no_debe_aparecer):
    """GeoNames trae 32 mil ciudades: Lopez, Castro y Rodriguez son ciudades.

    El nombre del cliente pegado a la calle ('Juan Lopez Gorriti 4500') le
    agregaba 'Philippines' a la query y el geocoder vetaba los candidatos
    argentinos. Sin coma que la aisle, una ciudad de GeoNames no es localidad.
    """
    from smart_import.normalization.address import maximize_address_for_geocode
    salida = maximize_address_for_geocode(address, phone_region="AR")
    assert no_debe_aparecer not in salida, salida
    assert salida.endswith("Argentina"), salida


def test_lopez_pegado_a_la_calle_termina_en_argentina():
    from smart_import.normalization.address import maximize_address_for_geocode
    assert maximize_address_for_geocode(
        "Juan Lopez Gorriti 4500, 3B", phone_region="AR",
    ) == "Juan Lopez Gorriti 4500, 3B, Argentina"


@pytest.mark.parametrize("address, debe_aparecer", [
    ("Main St 123, Springfield, IL 62701", "United States"),
    ("Rua Augusta 1500, Campinas SP 13010", "Brazil"),
    ("10 Park Row, Leeds LS1 5HD", "United Kingdom"),
])
def test_una_ciudad_de_geonames_en_su_segmento_sigue_enriqueciendo(address, debe_aparecer):
    """En su propio segmento (con CP o sigla al lado) sigue siendo la ciudad.

    Springfield, Campinas y Leeds no estan en el JSON curado: solo GeoNames las conoce.
    """
    from smart_import.normalization.address import maximize_address_for_geocode
    assert debe_aparecer in maximize_address_for_geocode(address, phone_region="AR")


@pytest.mark.parametrize("address, phone_region, esperado", [
    # homonimas: el pais explicito tambien tiene una ciudad con ese nombre
    ("Av Massey 100, Lincoln", "AR", "Av Massey 100, Lincoln, Argentina"),
    ("San Martin 200, Colon", "AR", "San Martin 200, Colon, Argentina"),
    # 'rd' es tipo de via, no sigla: el segmento es una calle, no Elgin (Illinois)
    ("Dunedin, Elgin Rd 114", "NZ", "Dunedin, Elgin Rd 114, New Zealand"),
    # 'Mexico' tambien es un pueblo de Filipinas en GeoNames; el segmento es el pais
    ("Avenida Juarez 100, Mexico", None, "Avenida Juarez 100, Mexico"),
])
def test_una_ciudad_de_geonames_en_su_segmento_no_inventa_otro_pais(
        address, phone_region, esperado):
    """Medido sobre 49.748 direcciones de los corpus con su pais verdadero: sin
    estas tres reglas, sacar la pista de adentro de la calle dejaba ganar a una
    homonima de otro pais (Scarborough → Reino Unido, Tala → Egipto)."""
    from smart_import.normalization.address import maximize_address_for_geocode
    assert maximize_address_for_geocode(address, phone_region=phone_region) == esperado


def test_una_ciudad_de_geonames_sin_coma_no_pisa_el_pais_explicito():
    """Cerrando la direccion sin coma es evidencia debil: completa, no pisa.

    'Gorriti 4500 Juan Lopez' tiene la misma forma que '123 Main St Springfield'.
    Con phone_region o depot manda el pais explicito; sin ninguno, la ciudad
    completa el pais.
    """
    from smart_import.normalization.address import maximize_address_for_geocode

    assert maximize_address_for_geocode(
        "Gorriti 4500 Juan Lopez", phone_region="AR",
    ) == "Gorriti 4500 Juan Lopez, Argentina"
    assert maximize_address_for_geocode(
        "Gorriti 4500 Juan Lopez", country="Argentina",
    ) == "Gorriti 4500 Juan Lopez, Argentina"
    assert maximize_address_for_geocode(
        "123 Main St Springfield", phone_region="US",
    ) == "123 Main St Springfield, United States"
    assert maximize_address_for_geocode(
        "123 Main St Springfield",
    ) == "123 Main St Springfield, United States"


# ---------- un solo pais por query ----------

@pytest.mark.parametrize("address, phone_region, esperado", [
    # 'Brazil' (nombre de GeoNames) implica el pais aunque el preferido sea 'Brasil'
    ("Rua Augusta 1500, Campinas", "AR", "Rua Augusta 1500, Campinas, Brazil"),
    # el pais ya escrito manda sobre phone_region
    ("Carrera 7 45, Colombia", "AR", "Carrera 7 45, Colombia"),
    ("Av Corrientes 100, Argentina", "US", "Av Corrientes 100, Argentina"),
    ("guatemala, Bulevar Ensenada de San Isidro 13-55, gt", "GT",
     "Bulevar Ensenada de San Isidro 13-55, guatemala, gt"),
    # ...pero una calle con nombre de pais seguida de su altura no es el pais
    ("NICARAGUA, 4824", "AR", "4824, NICARAGUA, Argentina"),
    ("COSTA RICA, 6474BIS", "UY", "6474BIS, COSTA RICA, Uruguay"),
    # 'Georgia' es un estado: no alcanza para dejar de agregar el pais
    ("123 Main St, Georgia", "US", "123 Main St, Georgia, United States"),
    # el mismo pais con dos nombres va una sola vez
    ("Zapadla 763, Liberec, 46311", "CZ", "Zapadla 763, Liberec, 46311, Czech Republic"),
])
def test_la_query_lleva_un_solo_pais(address, phone_region, esperado):
    """'Campinas, Brazil, Argentina' y 'Colombia, Argentina' mandaban al geocoder
    dos paises a la vez. Medido sobre los corpus con phone_region=AR para
    direcciones extranjeras: las queries con 2+ paises bajaron de 6.413 a 203."""
    from smart_import.normalization.address import maximize_address_for_geocode
    assert maximize_address_for_geocode(address, phone_region=phone_region) == esperado


@pytest.mark.parametrize("address, phone_region, esperado", [
    ("Av Libertador 16000, San Isidro", "AR", "Av Libertador 16000, San Isidro, Argentina"),
    ("Av Canaval y Moreyra 390, San Isidro", "PE", "Av Canaval y Moreyra 390, San Isidro, Peru"),
    ("Av Canaval y Moreyra 390, San Isidro", None, "Av Canaval y Moreyra 390, San Isidro, Peru"),
    ("123 Main St, Paris, TX", "US", "123 Main St, Paris, TX, United States"),
    ("12 Rue de Rivoli, Paris", "AR", "12 Rue de Rivoli, Paris, France"),
    ("Via Roma 10, Palermo", "IT", "Via Roma 10, Palermo, Italy"),
    ("Malabia 1136, Palermo", "AR", "Malabia 1136, Palermo, CABA, Buenos Aires, Argentina"),
])
def test_una_pista_curada_no_pisa_una_homonima_del_pais_explicito(address, phone_region, esperado):
    """El JSON curado dice San Isidro → Peru, Paris → Francia, Palermo → CABA.
    Son ciertas salvo cuando el pais del depot/telefono tiene su propia ciudad
    con ese nombre: ahi el pais explicito gana. Sin pais explicito, el curado."""
    from smart_import.normalization.address import maximize_address_for_geocode
    assert maximize_address_for_geocode(address, phone_region=phone_region) == esperado


@pytest.mark.parametrize("address", [
    "Juan Lopez Gorriti 4500, 3B",            # GeoNames adentro de la calle
    "Main St 123, Springfield, IL 62701",     # GeoNames en su segmento
    "200 Ocean Dr, South Beach, Miami Beach, FL",   # frase curada al final
    "Av Colon 1234, Mar del Plata",           # frase curada pegada a la altura
    "12 Rue X, Saint-Denis",                  # frase con guion
    "ул. Тверская 1, санкт-петербург",        # frase sin token ASCII
    "Av Corrientes 100, Palermo, CABA",
    "Rua Augusta 1500, Campinas SP 13010",
])
def test_el_indice_de_pistas_da_lo_mismo_que_recorrer_todas_las_filas(address, monkeypatch):
    """`_candidate_rows` evita recorrer 32 mil filas por direccion (35 ms → <1 ms).
    Solo puede saltear filas que `_cue_matches` igual rechazaria."""
    from smart_import.normalization import address as mod
    from smart_import.resources import locality_expansion_rows

    rapido = [mod.maximize_address_for_geocode(address, phone_region=region)
              for region in ("AR", "US", None)]
    monkeypatch.setattr(mod, "_candidate_rows", lambda _tokens: list(locality_expansion_rows()))
    completo = [mod.maximize_address_for_geocode(address, phone_region=region)
                for region in ("AR", "US", None)]
    assert rapido == completo
