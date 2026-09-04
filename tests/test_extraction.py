"""Extraccion de columnas compuestas con el modelo.

Los tests NO cargan el modelo: se mockea la generacion. Lo que se verifica es la
capa que rodea al modelo, que es donde esta el riesgo: que no alucine, que no
corrompa acentos y que su ausencia no rompa nada.
"""
import pytest

from smart_import.config import Config
from smart_import.extraction import CompositeExtractor, find_composite_column

BLOB = "Ana Rodríguez - Av. Corrientes 944, Buenos Aires - tel 1136251563"


def _extractor(**over):
    base = Config.from_env().__dict__.copy()
    base.update(over)
    return CompositeExtractor(Config(**base))


def test_usa_el_formato_nativo_de_nuextract():
    """No es un chat: es template + texto. Con prompt de instruccion el modelo
    devuelve el schema en lugar de extraer."""
    prompt = _extractor()._prompt("texto de prueba")
    assert "<|input|>" in prompt and "<|output|>" in prompt
    assert "### Template:" in prompt and "### Text:" in prompt
    assert '"customer_name": ""' in prompt


def test_solo_se_aceptan_las_claves_del_template():
    ex = _extractor()
    parsed = ex._parse('{"customer_name": "Ana", "campo_inventado": "x", "phone": "123"}')
    assert parsed == {"customer_name": "Ana", "phone": "123"}


@pytest.mark.parametrize("raw", ["no es json", "{roto", '["lista"]', ""])
def test_salida_no_parseable_se_descarta_sin_romper(raw):
    assert _extractor()._parse(raw) == {}


def test_lo_que_no_esta_en_la_fuente_se_descarta():
    """Extraer no es generar: un valor que no aparece en el texto es alucinacion."""
    ex = _extractor()
    verified = ex._verify(
        {"customer_name": "Ana Rodríguez", "phone": "1136251563",
         "address": "Calle Que No Existe 1"}, BLOB)
    assert "address" not in verified
    assert verified["phone"] == "1136251563"


def test_recupera_los_acentos_que_el_modelo_corrompe():
    """El 0.5B devuelve 'Rodrñez'; el texto correcto esta en la fuente."""
    ex = _extractor()
    verified = ex._verify({"customer_name": "Ana Rodrñez"}, BLOB)
    assert verified.get("customer_name") == "Ana Rodríguez"


def test_devuelve_el_span_original_no_el_del_modelo():
    ex = _extractor()
    verified = ex._verify({"address": "av. corrientes 944, buenos aires"}, BLOB)
    assert verified["address"] == "Av. Corrientes 944, Buenos Aires"   # mayusculas de la fuente


def test_sin_modelo_no_se_rompe_nada():
    ex = _extractor(model="modelo/que-no-existe")
    result = ex.run([BLOB, BLOB])
    assert result.failed == 2
    assert result.values == [{}, {}]
    assert any("no esta disponible" in w for w in result.warnings)


def test_respeta_el_tope_de_filas():
    """~1.4 s por fila: sin tope, un archivo grande corre por horas."""
    ex = _extractor(model="modelo/que-no-existe")
    result = ex.run([BLOB] * 100, max_rows=5)
    assert result.rows == 5
    assert any("5 de 100" in w for w in result.warnings)


def test_extrae_de_verdad_con_un_modelo_mockeado(monkeypatch):
    ex = _extractor()
    monkeypatch.setattr(ex, "_ensure_model", lambda: object())
    monkeypatch.setattr(ex, "_generate", lambda text: {
        "customer_name": "Ana Rodriguez",          # sin tilde, como lo devuelve el 0.5B
        "address": "Av. Corrientes 944, Buenos Aires",
        "phone": "1136251563",
    })
    result = ex.run([BLOB])
    assert result.extracted == 1
    values = result.values[0]
    assert values["customer_name"] == "Ana Rodríguez"      # tilde recuperada de la fuente
    assert values["phone"] == "1136251563"
    for value in values.values():
        assert value in BLOB                              # todo sale del texto original


def test_encuentra_la_columna_compuesta_en_el_report():
    report = {"mapping": {
        "Zona": {"target": "zone", "evidence": "alias 'zona'"},
        "Datos entrega": {"target": "address",
                          "evidence": "texto largo | la columna mezcla varios campos (...)"},
    }}
    assert find_composite_column(report) == "Datos entrega"
    assert find_composite_column({"mapping": {}}) is None
