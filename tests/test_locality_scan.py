"""Capa de localidad: de que ciudad habla el archivo.

No decide: junta evidencia. Lo que se prueba es que la evidencia sea correcta,
que los falsos positivos ('Entrega', 'Despacho') no entren, y que un homonimo
sin pais quede marcado para preguntar.
"""
import pytest

from smart_import.locality import (
    ACCEPT_CONFIDENCE, SUGGEST_CONFIDENCE, detect_locality,
)
from smart_import.locality.scan import _header_labels, _tail_labels

MIAMI_PASTE = """Deliveries for today (Miami):

1. Ana Rivera | 100 Biscayne Blvd, Downtown, Miami, FL | (305) 555-0100
2. James Lopez | 200 Ocean Dr, South Beach, Miami Beach, FL | 1 package
3. Maria Gomez | 350 NE 1st Ave, Brickell, Miami, FL | 1 large box
"""


def _geonames_or_skip():
    from smart_import.geocoding.city_lookup import city_matches
    if not city_matches("Miami", "US"):
        pytest.skip("falta data/geonames/cities15000.txt")


# --------------------------------------------------------------- sin evidencia

def test_sin_filas_ni_documento_no_inventa():
    evidence = detect_locality()
    assert evidence.best is None
    assert evidence.needs_user_input is True
    assert "no se detecto" in evidence.reason


def test_sin_ciudad_la_ui_tiene_que_pedirla():
    """Direcciones sueltas sin ciudad: no se adivina, se pregunta."""
    rows = [{"address": "Dufau 1418"}, {"address": "Sarmiento 451"}]
    evidence = detect_locality(rows=rows)
    assert evidence.best is None
    assert evidence.needs_user_input is True


# --------------------------------------------------------------- columnas

def test_columna_city_manda_y_no_necesita_geonames():
    """Un pueblo chico no esta en cities15000 y sigue siendo la ciudad."""
    rows = [{"city": "Rauch", "region": "Buenos Aires", "country": "Argentina",
             "address": "Belgrano 120"}] * 6
    evidence = detect_locality(rows=rows)
    best = evidence.best
    assert best is not None
    assert best.city == "Rauch"
    assert best.region == "Buenos Aires"
    assert best.country == "Argentina"
    assert "rows" in best.sources
    # todas las filas de acuerdo: sugerencia fuerte
    assert best.confidence >= ACCEPT_CONFIDENCE
    assert evidence.needs_user_input is False


def test_una_sola_fila_de_muchas_no_alcanza():
    rows = [{"city": "Tandil", "address": "Dufau 1418"}]
    rows += [{"address": f"Calle {i} 100"} for i in range(9)]
    evidence = detect_locality(rows=rows)
    best = evidence.best
    assert best is not None and best.city == "Tandil"
    assert best.confidence < ACCEPT_CONFIDENCE
    assert evidence.needs_user_input is True


def test_postcode_y_region_salen_de_la_moda():
    rows = [{"city": "Tandil", "region": "Buenos Aires", "postcode": "B7000",
             "country": "AR", "address": "Dufau 1418"}] * 4
    best = detect_locality(rows=rows).best
    assert best.postcode == "B7000"
    assert best.country == "Argentina"     # ISO-2 expandido


def test_una_sola_fila_no_vale_como_archivo_entero():
    """Regresión: 1 de 1 daba ratio 1.0 y salía con la fuerza de 55 filas."""
    _geonames_or_skip()
    rows = [{"address": "Dufau 1418, Belgrano"}]
    evidence = detect_locality(rows=rows)
    best = evidence.best
    assert best is not None and best.support == 1
    assert best.confidence < ACCEPT_CONFIDENCE
    assert evidence.needs_user_input is True


def test_confirmar_hasta_que_la_evidencia_sea_fuerte():
    """La franja sugerencia-pero-confirmar tiene que pedir input (el user eligio 'ask')."""
    _geonames_or_skip()
    evidence = detect_locality(document=MIAMI_PASTE)
    assert SUGGEST_CONFIDENCE <= evidence.best.confidence < ACCEPT_CONFIDENCE
    assert evidence.needs_user_input is True
    assert "confirmar" in evidence.reason


def test_comuna_2_y_cp_suelto_no_son_ciudad():
    rows = [{"city": "Comuna 2", "address": "Callao 100"},
            {"city": "1425", "address": "Santa Fe 3253"}]
    assert detect_locality(rows=rows).best is None


# --------------------------------------------------------------- direcciones

def test_ciudad_desde_la_cola_de_la_direccion():
    """Sin columna city: '…, Miami, FL' repetido es evidencia fuerte."""
    _geonames_or_skip()
    rows = [{"address": "100 Biscayne Blvd, Downtown, Miami, FL"},
            {"address": "350 NE 1st Ave, Brickell, Miami, FL"},
            {"address": "801 Brickell Ave, Brickell, Miami, FL"}]
    best = detect_locality(rows=rows).best
    assert best is not None
    assert best.city == "Miami"
    assert "address" in best.sources
    assert best.support == 3


def test_la_cola_cuenta_una_vez_por_fila():
    """'Brickell, Miami, FL' no puede sumar dos veces la misma fila."""
    _geonames_or_skip()
    rows = [{"address": "350 NE 1st Ave, Brickell, Miami, FL"}]
    evidence = detect_locality(rows=rows)
    assert sum(c.support for c in evidence.candidates) <= len(rows)


def test_la_calle_no_se_lee_como_ciudad():
    """El primer segmento es la calle: 'Miami Ave' no convierte en ciudad."""
    rows = [{"address": "Miami Ave 100"}] * 3
    assert detect_locality(rows=rows).best is None


# --------------------------------------------------------------- encabezado

def test_encabezado_entre_parentesis_detecta_miami():
    """El caso del paste real: 'Deliveries for today (Miami)'."""
    _geonames_or_skip()
    evidence = detect_locality(document=MIAMI_PASTE)
    best = evidence.best
    assert best is not None
    assert best.city == "Miami"
    assert "header" in best.sources
    # solo encabezado: se pre-carga el selector, pero se pregunta
    assert best.confidence >= SUGGEST_CONFIDENCE
    assert best.confidence < ACCEPT_CONFIDENCE


def test_encabezado_mas_filas_es_mas_fuerte_que_solo_encabezado():
    _geonames_or_skip()
    rows = [{"city": "Miami", "region": "FL", "country": "US",
             "address": "100 Biscayne Blvd"}] * 3
    solo_header = detect_locality(document=MIAMI_PASTE).best
    con_filas = detect_locality(rows=rows, document=MIAMI_PASTE).best
    assert con_filas.confidence > solo_header.confidence
    assert set(con_filas.sources) >= {"header", "rows"}
    assert detect_locality(rows=rows, document=MIAMI_PASTE).needs_user_input is False


def test_el_pais_se_completa_desde_geonames():
    """Sin columna country, el ISO de GeoNames alcanza para enriquecer."""
    _geonames_or_skip()
    rows = [{"address": "100 Biscayne Blvd, Downtown, Miami, FL"}] * 3
    best = detect_locality(rows=rows).best
    assert best.iso == "US"
    assert best.country and best.country.casefold() != "none"


def test_encabezado_con_etiqueta_explicita():
    _geonames_or_skip()
    doc = "Reparto\nCity: Miami\n\n1. Ana | 100 Biscayne Blvd\n"
    best = detect_locality(document=doc).best
    assert best is not None and best.city == "Miami"


def test_el_encabezado_termina_en_el_primer_item():
    """Un telefono '(305) 555-0100' de la fila 1 no es un candidato."""
    labels = _header_labels(MIAMI_PASTE)
    assert any("Miami" in label for label in labels)
    assert not any("305" in label for label in labels)


def test_palabras_de_logistica_no_son_ciudad():
    """examples/free-text/false_positives.txt: nada de esto es una localidad."""
    doc = ("Hola\nGracias\nSaludos\nDespacho\nPendiente\nUrgente\nCliente\n"
           "Entrega\n\n1. Ana | Dufau 1418\n")
    evidence = detect_locality(document=doc)
    detectadas = {(c.city or "").casefold() for c in evidence.candidates}
    assert not detectadas & {"despacho", "entrega", "cliente", "urgente",
                             "pendiente", "gracias", "saludos", "hola"}


# --------------------------------------------------------------- ambiguedad

def test_homonimo_sin_pais_se_pregunta():
    """'Cordoba' existe en AR y en ES: no se elige, se marca para preguntar."""
    from smart_import.geocoding.city_lookup import city_matches
    isos = {h.iso for h in city_matches("Cordoba")}
    if len(isos) < 2:
        pytest.skip("cities15000 no lista Cordoba en mas de un pais")

    rows = [{"city": "Cordoba", "address": "Colon 100"}] * 5
    evidence = detect_locality(rows=rows)
    best = evidence.best
    assert best.is_homonym
    assert best.lat is None and best.lon is None      # no se elige centroide
    assert evidence.needs_user_input is True
    assert "falta el pais" in evidence.reason


def test_el_pais_de_las_filas_corta_el_homonimo():
    _geonames_or_skip()
    rows = [{"city": "Cordoba", "country": "AR", "address": "Colon 100"}] * 5
    best = detect_locality(rows=rows).best
    assert best.is_homonym is False
    assert best.iso == "AR"
    assert best.lat is not None


def test_dos_ciudades_empatadas_quedan_ambiguas():
    rows = ([{"city": "Tandil", "address": "Dufau 1418"}] * 3
            + [{"city": "Azul", "address": "Burgos 200"}] * 3)
    evidence = detect_locality(rows=rows)
    assert evidence.ambiguous is True
    assert evidence.needs_user_input is True
    assert len(evidence.candidates) >= 2


def test_mayoria_clara_gana_sobre_una_excepcion():
    rows = ([{"city": "Tandil", "address": "Dufau 1418"}] * 9
            + [{"city": "Azul", "address": "Burgos 200"}])
    evidence = detect_locality(rows=rows)
    assert evidence.best.city == "Tandil"
    assert evidence.ambiguous is False


# --------------------------------------------------------------- serializacion

def test_as_dict_tiene_lo_que_la_ui_necesita():
    rows = [{"city": "Tandil", "region": "Buenos Aires", "country": "AR",
             "address": "Dufau 1418"}] * 3
    data = detect_locality(rows=rows).as_dict()
    assert set(data) >= {"best", "candidates", "rows_total", "country",
                         "needs_user_input", "ambiguous", "reason"}
    assert data["best"]["city"] == "Tandil"
    assert data["rows_total"] == 3
    import json
    json.dumps(data)      # tiene que poder ir al report


def test_helpers_de_segmentos():
    assert _tail_labels("350 NE 1st Ave, Brickell, Miami, FL") == [
        "Brickell", "Miami", "FL"]
    assert _tail_labels("Dufau 1418") == []
