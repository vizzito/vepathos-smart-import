from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
EXAMPLES = ROOT / "examples"
#: corpus de texto libre versionado (no se regenera: es el dataset de regresion)
FREE_TEXT = EXAMPLES / "free-text"
SCHEMA = ROOT / "schemas" / "vepathos_flat_v1.json"


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
