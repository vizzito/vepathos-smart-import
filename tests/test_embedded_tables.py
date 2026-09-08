"""Una tabla adentro de un texto libre se lee como tabla; una lista, no.

El riesgo de esta capa es exactamente uno: confundir la primera entrega de una
lista escrita a mano con un header y comersela. Media suite de abajo son casos
que TIENEN que seguir siendo texto libre.
"""
import pytest

from smart_import.config import Config
from smart_import.detection.blocks import blank_spans, find_table_blocks
from smart_import.extraction.context import ExtractionContext
from smart_import.extraction.free_text import FreeTextExtractor


@pytest.fixture
def extractor(schema):
    return FreeTextExtractor(Config.from_env(),
                             ExtractionContext(phone_region="AR"), schema=schema)


LISTA = """\
Lista de entregas para hoy (Buenos Aires):

1. Ana Pérez | Av. Corrientes 100, Palermo, CABA | 11 4000-1000 | 2 paquetes
2. Juan López | Av. Santa Fe 137, Palermo, CABA | 1 paquete 2.5kg
3. María Gómez | Av. Cabildo 174, Belgrano, CABA | 1 caja grande 6kg
"""

TABLA = """\
Domicilio Entrega,Cliente,Cel,Bultos,Peso kg,Referencia
"Av. Santa Fe 475, Buenos Aires",Sofia López,1160485121,2,2.75,Dejar en porteria
"Callao 2862, Buenos Aires",Carlos Díaz,1177939607,3,3.44,Timbre 3B
"Maipú 1271, Buenos Aires",Ana Pérez,1160528350,1,2.74,Timbre 3B
"Lavalle 507, Buenos Aires",Ana Fernández,1171244036,1,3.59,
"""


# ---------- (1) deteccion estructural ----------

def test_encuentra_el_bloque_con_su_header():
    blocks = find_table_blocks(LISTA + "\n" + TABLA)
    assert len(blocks) == 1
    block = blocks[0]
    assert block.delimiter == ","
    assert block.header[0] == "Domicilio Entrega"
    assert len(block.rows) == 4


def test_una_lista_vinetada_nunca_es_un_bloque():
    assert find_table_blocks(LISTA) == []


def test_texto_corto_no_alcanza():
    assert find_table_blocks("Ana, Corrientes 100\nJuan, Santa Fe 200") == []


def test_blank_spans_no_mueve_los_offsets():
    document = LISTA + "\n" + TABLA
    block = find_table_blocks(document)[0]
    masked = blank_spans(document, [block.span])
    assert len(masked) == len(document)
    assert masked.count("\n") == document.count("\n")
    assert "Sofia López" not in masked
    assert "Ana Pérez" in masked          # la de la lista sigue estando


# ---------- (2) el schema es el que confirma ----------

def test_una_lista_con_pipes_sin_header_no_se_come_la_primera_entrega(extractor):
    """El caso que rompe todo si la capa se guia solo por la forma."""
    document = """\
Ana Pérez | Av. Corrientes 100, Palermo | 11 4000-1000 | 2 paquetes
Juan López | Av. Santa Fe 137, Palermo | 11 4000-1001 | 1 paquete
María Gómez | Av. Cabildo 174, Belgrano | 11 4000-1002 | 3 cajas
Carlos Ruiz | Av. Rivadavia 211, Caballito | 11 4000-1003 | 1 sobre
"""
    # estructuralmente parece una tabla...
    assert find_table_blocks(document), "el candidato estructural existe"
    # ...pero el header no nombra ningun campo, asi que no se acepta
    result = extractor.run_document(document)
    assert not result.tables
    names = {r.get("customer_name") for r in result.records}
    assert "Ana Pérez" in names, "la primera entrega no se puede perder"
    assert len(result.records) == 4


def test_header_sin_campo_de_destino_no_se_acepta(extractor):
    document = """\
Producto,Cantidad,Precio,Deposito
Tornillos,10,150,A
Tuercas,20,90,B
Arandelas,30,45,C
Clavos,40,60,D
"""
    result = extractor.run_document(document)
    assert not result.tables


def test_el_schema_apagado_deja_todo_como_antes():
    """Sin schema la capa no corre: mismo comportamiento que siempre."""
    sin_schema = FreeTextExtractor(Config.from_env(),
                                   ExtractionContext(phone_region="AR"))
    result = sin_schema.run_document(LISTA + "\n" + TABLA)
    assert not result.tables


# ---------- (3) lo que el bloque rescata ----------

def test_el_bloque_recupera_nombre_referencia_y_bultos(extractor):
    result = extractor.run_document(LISTA + "\n" + TABLA)
    assert len(result.tables) == 1
    assert len(result.records) == 7          # 3 de la lista + 4 de la tabla

    sofia = next(r for r in result.records if r.get("customer_name") == "Sofia López")
    assert sofia.get("reference") == "Dejar en porteria"
    assert sofia.get("phone") == "+541160485121", "mismo formato que el texto libre"
    assert sofia.get("quantity") == "2"
    assert "Av. Santa Fe 475" in sofia.get("address")


def test_el_nombre_no_queda_dentro_de_la_direccion(extractor):
    result = extractor.run_document(LISTA + "\n" + TABLA)
    for record in result.records:
        address = record.get("address") or ""
        assert "Sofia López" not in address
        assert '"' not in address, "la comilla del CSV no llega al address"


def test_el_orden_del_documento_se_respeta(extractor):
    result = extractor.run_document(LISTA + "\n" + TABLA)
    ids = [r.get("delivery_id") for r in result.records]
    assert ids[:3] == ["1", "2", "3"], "la lista numerada conserva su numero"
    assert ids[3:] == ["004", "005", "006", "007"]


def test_el_peso_de_la_celda_no_se_toca(extractor):
    """La celda dice el peso del bulto y el schema guarda exactamente eso."""
    result = extractor.run_document(TABLA)
    sofia = next(r for r in result.records if r.get("customer_name") == "Sofia López")
    assert float(sofia.get("weight_kg")) == 2.75
    una_sola = next(r for r in result.records if r.get("customer_name") == "Ana Pérez")
    assert float(una_sola.get("weight_kg")) == 2.74


def test_una_columna_de_id_gana_sobre_la_posicion(extractor):
    document = """\
Pedido,Direccion,Cliente,Bultos
VP-100,"Av. Corrientes 100, CABA",Ana Perez,2
VP-101,"Av. Santa Fe 200, CABA",Juan Lopez,1
VP-102,"Av. Cabildo 300, CABA",Maria Gomez,3
VP-103,"Callao 400, CABA",Carlos Ruiz,1
"""
    result = extractor.run_document(document)
    assert result.tables
    assert [r.get("delivery_id") for r in result.records] == [
        "VP-100", "VP-101", "VP-102", "VP-103"]


@pytest.mark.parametrize("delimiter", [";", "\t", "|"])
def test_otros_delimiters(extractor, delimiter):
    header = delimiter.join(["Direccion", "Cliente", "Telefono", "Bultos"])
    rows = [delimiter.join([f"Av. Corrientes {n}00, CABA", f"Cliente {n}",
                            f"11400010{n:02d}", "2"]) for n in range(1, 5)]
    result = extractor.run_document("Hola, va la lista:\n\n" + "\n".join([header, *rows]))
    assert len(result.tables) == 1
    assert len(result.records) == 4


def test_una_tabla_pura_con_saludo_arriba(extractor):
    """El clasificador global la manda a free_text por el saludo; la capa la salva."""
    result = extractor.run_document("Buenas! te paso las de hoy:\n\n" + TABLA)
    assert len(result.tables) == 1
    assert len(result.records) == 4


def test_dos_bloques_en_el_mismo_documento(extractor):
    result = extractor.run_document(TABLA + "\n\nY estas otras:\n\n" + TABLA)
    assert len(result.tables) == 2
    assert len(result.records) == 8
