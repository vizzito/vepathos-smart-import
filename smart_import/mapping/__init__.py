from .base import ColumnMapping, MappingResult, SchemaMapper
from .mapper import RuleSchemaMapper

__all__ = ["ColumnMapping", "MappingResult", "SchemaMapper", "RuleSchemaMapper", "build_mapper"]


def build_mapper(config=None):
    """Devuelve el mapper segun SMART_IMPORT_AI_ENABLED.

    Si la IA esta habilitada pero el modelo no carga, se degrada a reglas con un
    warning: la ausencia del modelo NUNCA rompe un import.
    """
    from ..config import Config
    cfg = config or Config.from_env()
    if not cfg.ai_enabled:
        return RuleSchemaMapper(cfg)
    from .ai import AISchemaMapper
    return AISchemaMapper(cfg)
