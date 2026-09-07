"""Geocode contra PBF/indice real. Se salta si no hay datos locales.

  pytest -q -m real_geo
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.real_geo

from smart_import.config import Config
from smart_import.geocoding.pbf_registry import PbfRegistry


def _pbf_root() -> Path | None:
    cfg = Config.from_env()
    for candidate in (cfg.pbf_dir, os.getenv("ROUTE_OPTIMIZER_DATA", "")):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return None


@pytest.fixture(scope="module")
def pbf_root():
    root = _pbf_root()
    if root is None:
        pytest.skip("sin PBF local (SMART_IMPORT_PBF_DIR / ROUTE_OPTIMIZER_DATA)")
    return root


def test_registry_resuelve_caba(pbf_root):
    reg = PbfRegistry.scan(pbf_root)
    if not reg.entries:
        pytest.skip(f"sin .osm.pbf bajo {pbf_root}")
    hit = reg.resolve(lat=-34.6037, lon=-58.3816, zone_hint="CABA")
    assert hit is not None
    assert hit.path.exists()
