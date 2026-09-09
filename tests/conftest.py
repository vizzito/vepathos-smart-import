import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
EXAMPLES = ROOT / "examples"
#: corpus de texto libre versionado (no se regenera: es el dataset de regresion)
FREE_TEXT = EXAMPLES / "free-text"
SCHEMA = ROOT / "schemas" / "vepathos_flat_v1.json"


#: Prefijos de todo lo que la app lee del entorno. Las dos ultimas no tienen
#: prefijo propio porque las comparte con el route-optimizer.
PREFIJOS_DE_CONFIG = ("SMART_IMPORT_", "GEOCODE", "AUTO_ACCEPT", "REVIEW_THRESHOLD",
                      "MAPPING_MIN", "ROUTE_OPTIMIZER", "RABBITMQ_", "REDIS_")


def _limpiar_entorno() -> list[str]:
    """Ningun test lee la config del shell que lo lanzo.

    El que opera un nodo termina con RABBITMQ_*, REDIS_* y SMART_IMPORT_* vivas
    en su terminal, y los tests que parten de `Config.from_env()` heredaban esos
    valores: la suite pasaba o fallaba segun quien la corriera y desde donde.
    Paso tres veces —el prefijo de las colas, la password del broker y los
    defaults del dataclass— antes de arreglarlo en un solo lugar.

    Corre al importar el conftest y no en un fixture porque varios modulos
    resuelven su `Config.from_env()` al importarse, o sea antes de que corra
    cualquier fixture. Un test que necesita una variable la setea con
    `monkeypatch.setenv`, que corre mucho despues.

    Escotilla: `SMART_IMPORT_TEST_USE_ENV=1` para el que esta depurando y
    quiere que su entorno mande.
    """
    if os.environ.get("SMART_IMPORT_TEST_USE_ENV") == "1":
        return []
    borradas = [n for n in os.environ if n.startswith(PREFIJOS_DE_CONFIG)]
    for nombre in borradas:
        del os.environ[nombre]
    return borradas


BORRADAS_DEL_ENTORNO = _limpiar_entorno()


@pytest.fixture(scope="session", autouse=True)
def ensure_fixtures():
    """Los fixtures generados no se versionan: se crean si faltan."""
    from smart_import.fixtures_gen import build_all
    if not (FIXTURES / "es_headers_raros.xlsx").exists():
        build_all(FIXTURES, rows=40)
    return FIXTURES


@pytest.fixture(scope="session")
def schema():
    from smart_import.schemas import TargetSchema
    return TargetSchema.load(SCHEMA)


@pytest.fixture(scope="session")
def free_text():
    """Los .txt del corpus de regresion, por nombre sin extension."""
    return {path.stem: path.read_text(encoding="utf-8")
            for path in sorted(FREE_TEXT.glob("*.txt"))}


@pytest.fixture(scope="session")
def expected_deliveries():
    """El gold del paste de WhatsApp: 12 entregas conocidas."""
    import json
    return json.loads((FREE_TEXT / "whatsapp_12.expected.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def ar_context():
    from smart_import.extraction.context import ExtractionContext
    return ExtractionContext(phone_region="AR")
