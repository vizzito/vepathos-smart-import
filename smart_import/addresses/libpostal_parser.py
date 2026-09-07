"""libpostal como backend de parsing. OPCIONAL y apagado por defecto.

libpostal NO es un geocoder: no produce coordenadas. Solo descompone y normaliza
direcciones internacionales. Es una dependencia pesada (libreria C + ~2 GB de
datos), asi que:

  * el import es lazy: con la flag apagada este modulo no toca `postal`
  * `available()` dice la verdad sin romper nada
  * el servicio arranca igual si no esta

Instalacion: ver `docs/libpostal.md`.
"""
from __future__ import annotations

from functools import lru_cache

from .base import AddressParser, ParsedAddress

#: etiqueta de libpostal -> componente nuestro
LIBPOSTAL_MAP = {
    "house_number": "house_number",
    "road": "road",
    "unit": "unit",
    "level": "level",
    "house": "building",
    "suburb": "suburb",
    "city_district": "neighbourhood",
    "city": "city",
    "state_district": "state",
    "state": "state",
    "postcode": "postcode",
    "country": "country",
    "near": "landmark",
}


@lru_cache(maxsize=1)
def _parse_address():
    """El callable de libpostal, o None si no esta instalado.

    OJO: importar `postal` carga ~2,6 GB de datos y tarda ~3 s. Solo se llama
    cuando de verdad se va a parsear, nunca para *preguntar* si esta instalado.
    """
    try:
        from postal.parser import parse_address           # type: ignore import-not-found
    except Exception:
        return None
    return parse_address


@lru_cache(maxsize=1)
def is_installed() -> bool:
    """Si el binding esta disponible, SIN cargar los datos.

    `import postal` reserva ~2,6 GB. `/health` pregunta esto en cada request: si
    lo respondieramos importando, un balanceador que pollea tumba el servicio —
    y lo haria incluso con SMART_IMPORT_LIBPOSTAL_ENABLED=false.
    """
    from importlib.util import find_spec
    try:
        return find_spec("postal.parser") is not None
    except (ImportError, ValueError):
        return False


def is_loaded() -> bool:
    """True solo si los datos YA se cargaron en este proceso.

    Es diagnostico puro: nunca puede disparar la carga ni romper si alguien
    reemplazo el callable (tests).
    """
    info = getattr(_parse_address, "cache_info", None)
    if info is None:
        return False
    return info().currsize > 0


class LibpostalAddressParser(AddressParser):
    """Delega en libpostal. Si no esta disponible devuelve una parse vacia."""

    name = "libpostal"

    def available(self) -> bool:
        return is_installed()

    def parse(self, text: str, context=None) -> ParsedAddress:
        raw = (text or "").strip()
        parse_address = _parse_address()
        if not raw or parse_address is None:
            return ParsedAddress(text=raw, parser=self.name,
                                 evidence=("libpostal no esta instalado",))

        components: dict[str, str] = {}
        evidence: list[str] = []
        for value, label in parse_address(raw):
            target = LIBPOSTAL_MAP.get(label)
            if not target or components.get(target):
                continue
            components[target] = value.strip()
            evidence.append(f"libpostal {label} -> {target}")
        return ParsedAddress(text=raw, components=components, parser=self.name,
                             evidence=tuple(evidence))
