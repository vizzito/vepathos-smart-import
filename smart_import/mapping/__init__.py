from .base import ColumnMapping, MappingResult, SchemaMapper
from .mapper import RuleSchemaMapper

__all__ = ["ColumnMapping", "MappingResult", "SchemaMapper", "RuleSchemaMapper", "build_mapper"]


def build_mapper(config=None):
    """El mapeo de columnas es 100% deterministico.

    Se probo delegarlo a un modelo de extraccion y devolvia el schema del
    prompt en lugar de razonar sobre el: mapear headers es una tarea de
    instruccion, no de extraccion. Las reglas resuelven 15 de 16 fixtures.
    El modelo se usa en `smart_import.extraction`, que es su trabajo real.
    """
    from ..config import Config
    return RuleSchemaMapper(config or Config.from_env())
