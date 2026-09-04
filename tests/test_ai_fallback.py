"""La IA es opcional. Si falta o falla, el import tiene que seguir funcionando.

Es el invariante que hace desplegable esto: el modelo no puede ser un punto
unico de falla de una funcionalidad que se vende como opcional.
"""
import pytest

from smart_import.config import Config
from smart_import.mapping import build_mapper
from smart_import.mapping.ai import AISchemaMapper
from smart_import.mapping.base import MappingResult
from smart_import.mapping.mapper import RuleSchemaMapper
from smart_import.readers import read_any
from tests.conftest import FIXTURES


def _cfg(**over):
    base = Config.from_env().__dict__.copy()
    base.update(over)
    return Config(**base)


def test_por_defecto_no_se_usa_ia():
    assert isinstance(build_mapper(_cfg(ai_enabled=False)), RuleSchemaMapper)


def test_con_flag_activo_se_usa_el_mapper_de_ia():
    assert isinstance(build_mapper(_cfg(ai_enabled=True)), AISchemaMapper)


def test_si_el_modelo_no_carga_el_resultado_de_reglas_sobrevive(schema):
    """Sin transformers/torch instalados el import NO puede romperse."""
    mapper = AISchemaMapper(_cfg(ai_enabled=True, model="modelo/que-no-existe"))
    # merged_field deja columnas dudosas, asi que SI se intenta usar el modelo
    result = mapper.detect(read_any(FIXTURES / "merged_field.csv"), schema)

    assert isinstance(result, MappingResult)
    assert not result.ai_used
    assert result.mapping["Zona"].target == "zone"          # las reglas siguen valiendo
    assert any("modelo" in w for w in result.warnings)


def test_no_se_invoca_el_modelo_si_las_reglas_alcanzan(schema, monkeypatch):
    """Un archivo limpio no debe pagar el costo de una inferencia."""
    mapper = AISchemaMapper(_cfg(ai_enabled=True))

    def explode(*args, **kwargs):
        raise AssertionError("no habia que llamar al modelo")

    monkeypatch.setattr(mapper, "_ask_model", explode)
    result = mapper.detect(read_any(FIXTURES / "ref_us_seattle.xlsx"), schema)
    assert not result.needs_review and not result.ai_used


def test_respuesta_invalida_del_modelo_se_descarta(schema, monkeypatch):
    """Si el modelo inventa un campo inexistente, se ignora sin romper nada."""
    mapper = AISchemaMapper(_cfg(ai_enabled=True))
    monkeypatch.setattr(mapper, "_ask_model",
                        lambda *a, **k: {"Datos entrega": "campo_que_no_existe"})
    result = mapper.detect(read_any(FIXTURES / "merged_field.csv"), schema)
    assert all(m.target in schema.fields for m in result.mapping.values())


@pytest.mark.parametrize("raw,expected", [
    ('{"a": "address"}', {"a": "address"}),
    ('Claro! Aca va:\n{"a": "address"}\nEspero que sirva', {"a": "address"}),
    ("no es json", {}),
    ("{roto", {}),
    ('["no", "es", "un", "objeto"]', {}),
])
def test_parseo_tolerante_de_la_respuesta(raw, expected):
    assert AISchemaMapper._parse(raw) == expected


def test_la_sugerencia_de_la_ia_va_a_revision(schema, monkeypatch):
    """Lo que dice el modelo es una sugerencia, no un hecho: siempre se revisa."""
    mapper = AISchemaMapper(_cfg(ai_enabled=True))
    monkeypatch.setattr(mapper, "_ask_model", lambda *a, **k: {"Datos entrega": "reference"})
    result = mapper.detect(read_any(FIXTURES / "merged_field.csv"), schema)
    assert result.ai_used
    assert result.mapping["Datos entrega"].method == "ai"
    assert result.needs_review
    assert any(a["column"] == "Datos entrega" for a in result.ambiguous)


def test_el_modelo_solo_ve_muestras_no_el_archivo_entero(schema):
    """El prompt no puede escalar con la cantidad de filas."""
    mapper = AISchemaMapper(_cfg(ai_enabled=True, sample_rows=20))
    table = read_any(FIXTURES / "merged_field.csv")
    rules_result = RuleSchemaMapper(mapper.config).detect(table, schema)
    pending = mapper._pending(rules_result, table, schema)
    prompt = mapper._prompt(table, schema, pending, rules_result)

    assert len(prompt) < 8000
    filas = table.rows
    assert len(filas) > 20
    assert str(filas[-1][0]) not in prompt      # la ultima fila no viaja al modelo
