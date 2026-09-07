"""Que parser de direcciones usa este despliegue.

El flujo normal es siempre el heuristico. Si `SMART_IMPORT_LIBPOSTAL_ENABLED=true`
y la libreria esta instalada, se agrega libpostal como *paso de calidad* (solo
cuando el gate dice que hace falta). No es un cambio de estrategia: es un
agregado opcional.

`SMART_IMPORT_ADDRESS_PARSER=libpostal` queda para benchmarks (medir libpostal
solo). En produccion la flag que manda es `LIBPOSTAL_ENABLED`.
"""
from __future__ import annotations

from ..logging_setup import get_logger, stage
from .base import AddressParser
from .enhance import EnhancingAddressParser
from .heuristic import HeuristicAddressParser
from .hybrid import HybridAddressParser
from .libpostal_parser import LibpostalAddressParser, is_installed, is_loaded

HEURISTIC = "heuristic"
LIBPOSTAL = "libpostal"
HYBRID = "hybrid"
ENHANCED = "enhanced"

_log = get_logger("addresses")
#: una linea por modo por proceso (evita spam en /health y por-request)
_announced: set[str] = set()


def _announce(mode: str, message: str) -> None:
    if mode in _announced:
        return
    _announced.add(mode)
    stage(_log, "ADDRESS", message)


def build_address_parser(config=None, locales: tuple[str, ...] | None = None) -> AddressParser:
    """Heuristico siempre; libpostal solo como enhancer si la flag lo pide."""
    strategy = (getattr(config, "address_parser", HEURISTIC) or HEURISTIC).lower()
    enabled = bool(getattr(config, "libpostal_enabled", False))
    heuristic = HeuristicAddressParser(locales)

    if not enabled:
        _announce("off", "parser=heuristic  libpostal=off (SMART_IMPORT_LIBPOSTAL_ENABLED=false)")
        return heuristic
    if not is_installed():
        _announce("missing",
                  "parser=heuristic  libpostal=off (flag on pero libreria ausente; "
                  "brew install libpostal && pip install -e '.[libpostal]')")
        return heuristic

    if strategy == LIBPOSTAL:
        _announce("libpostal", "parser=libpostal  (modo benchmark)")
        return LibpostalAddressParser()

    if strategy == HYBRID:
        _announce("hybrid", "parser=hybrid  libpostal=completa-huecos")
        return HybridAddressParser(heuristic, LibpostalAddressParser())

    _announce("enhanced",
              "parser=enhanced  libpostal=on-demand "
              "(entra solo si falta road o road sospechosa)")
    return EnhancingAddressParser(heuristic, LibpostalAddressParser(), locales)


def describe_parsers(config=None) -> dict:
    """Para /health: que hay instalado y que se esta usando."""
    active = build_address_parser(config)
    return {
        "active": active.name,
        "libpostal_installed": is_installed(),
        "libpostal_loaded": is_loaded(),
        "libpostal_enabled": bool(getattr(config, "libpostal_enabled", False)),
        "libpostal_as_enhancer": (
            bool(getattr(config, "libpostal_enabled", False)) and is_installed()
            and active.name in (ENHANCED, HYBRID)
        ),
    }
