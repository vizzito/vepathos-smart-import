from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
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
