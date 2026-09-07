"""Segmentacion de texto libre: donde cortar, sin decidir todavia que es cada cosa."""
from smart_import.detection.segmenter import FreeTextSegmenter


def test_viñetas_dan_una_entrega_por_item(free_text):
    doc = free_text["whatsapp_12"]
    result = FreeTextSegmenter().split(doc)
    assert result.strategy == "bullet"
    assert len(result.segments) == 12
    assert len(result.preamble) == 1
    assert len(result.footer) == 2
    assert "Hola chicos" in result.preamble[0].text
    assert {s.text for s in result.footer} == {"Salutos,", "Despacho"}


def test_sin_viñetas_tambien_encuentra_las_doce(free_text):
    """Doce lineas pegadas entre dos lineas en blanco son doce registros."""
    result = FreeTextSegmenter().split(free_text["whatsapp_12_nobullets"])
    textos = [s.text for s in result.segments]
    assert sum(1 for t in textos if "Perez" in t or "Lopez" in t) == 2
    assert len(result.segments) == 14        # 12 entregas + preambulo + pie


def test_el_texto_original_se_preserva_byte_a_byte(free_text):
    doc = free_text["whatsapp_12"]
    for segment in FreeTextSegmenter().split(doc).all_segments:
        assert doc[segment.start:segment.end] == segment.text


def test_una_direccion_en_varios_renglones_no_se_parte():
    """Lineas cortas y sin numeros son continuacion, no registros nuevos."""
    doc = ("Pedido 1\n\nRahul Sharma\nFlat 14B\nMumbai\n\n"
           "Pedido 2\n\nPriya Patel\nMG Road\nBengaluru\n")
    result = FreeTextSegmenter().split(doc)
    assert result.strategy == "block"
    assert any("Rahul Sharma\nFlat 14B\nMumbai" == s.text for s in result.segments)


def test_sin_separador_cada_linea_es_un_candidato(free_text):
    result = FreeTextSegmenter().split(free_text["false_positives"])
    assert result.strategy == "line"
    assert len(result.segments) == 10


def test_el_numero_de_lista_queda_disponible_como_id():
    doc = "1) Ana Perez, Av. Corrientes 100\n2) Juan Lopez, Av. Santa Fe 137\n" \
          "3) Maria Gomez, Av. Cabildo 174\n"
    segments = FreeTextSegmenter().split(doc).segments
    assert [s.list_number for s in segments] == ["1", "2", "3"]
    assert segments[0].body.startswith("Ana Perez")     # sin la marca de lista
    assert segments[0].text.startswith("1)")            # el original no se toca


def test_documento_vacio_no_rompe():
    result = FreeTextSegmenter().split("")
    assert result.segments == [] and result.strategy == "empty"
