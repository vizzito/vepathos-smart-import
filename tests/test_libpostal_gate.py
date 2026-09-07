"""Gate de libpostal: cuando se llama y cuando NO.

libpostal es un paso de calidad opcional. Estos tests no requieren la libreria
instalada: usan un enhancer falso que cuenta llamadas.
"""
from __future__ import annotations

import pytest

from smart_import.addresses import (
    EnhancingAddressParser, HeuristicAddressParser, ParsedAddress,
    build_address_parser, describe_parsers, enhancement_reason,
    needs_enhancement,
)
from smart_import.config import Config


class CountingEnhancer(HeuristicAddressParser):
    """Finge ser libpostal: siempre 'mejora' road/city y cuenta llamadas."""

    name = "fake-libpostal"

    def __init__(self):
        super().__init__()
        self.calls = 0

    def available(self) -> bool:
        return True

    def parse(self, text, context=None):
        self.calls += 1
        return ParsedAddress(
            text=text, parser=self.name,
            components={"road": "ENHANCED-ROAD", "city": "EnhancedCity",
                        "house_number": "99"},
        )


# ---------- predicado del gate ----------

@pytest.mark.parametrize("texto,razon", [
    ("Flat 14B, Shanti Nagar, Near Hanuman Temple, Mumbai 400069", "missing_road"),
    ("Plot No. 42, Sector 18, Noida 201301", "missing_road"),
    ("Manzana 12 Casa 5, Barrio Norte", "missing_road"),
    ("Cyber Towers, Hitech City, Hyderabad 500081", "missing_road"),
])
def test_casos_que_SI_piden_enhancer(texto, razon):
    parsed = HeuristicAddressParser().parse(texto)
    assert needs_enhancement(parsed) is True
    assert enhancement_reason(parsed) == razon


def test_gate_detecta_road_sospechosa_sintetica():
    """Cuando el primario SI pone basura en road, el gate pide correccion."""
    from smart_import.addresses import ParsedAddress
    bad = ParsedAddress(text="x", components={"road": "Plot No", "house_number": "42"})
    assert enhancement_reason(bad) == "suspicious_road"


@pytest.mark.parametrize("texto", [
    "Av. Corrientes 100, CABA",
    "Malabia 1136, Palermo",
    "23 MG Road, Bengaluru 560001",
    "1171 1st Ave, Seattle",
    "Av. Paulista, 1578, Sao Paulo",
    "Apt 4B, 350 5th Ave, New York",
    "Calle 50 nro 1234, Medellin",
    "Yerbal, CABA, Argentina",
    "Av. Cabildo, CABA",
])
def test_casos_que_NO_piden_enhancer(texto):
    """Despues de Fase 0 el heuristico alcanza: no gastar libpostal."""
    parsed = HeuristicAddressParser().parse(texto)
    assert needs_enhancement(parsed) is False, (
        f"gate abrio sobre {texto!r}: road={parsed.get('road')!r} "
        f"reason={enhancement_reason(parsed)!r}")


# ---------- EnhancingAddressParser respeta el gate ----------

def test_enhancer_no_se_llama_si_el_heuristico_ya_resolvio():
    fake = CountingEnhancer()
    parser = EnhancingAddressParser(HeuristicAddressParser(), fake)
    parsed = parser.parse("Av. Corrientes 100, CABA")
    assert fake.calls == 0
    assert parser.enhancer_skips == 1
    assert parser.last_outcome == "skip"
    assert parsed.get("road") == "Av. Corrientes"
    assert parsed.parser == "heuristic"          # sin enhance, queda el primario


def test_calle_suelta_caba_no_pide_libpostal():
    """Yerbal / Zelada son calles, no barrios. El heuristico las resuelve."""
    parsed = HeuristicAddressParser().parse("Yerbal, CABA, Argentina")
    assert parsed.get("road") == "Yerbal"
    assert needs_enhancement(parsed) is False


def test_barrio_conocido_no_se_roba_como_calle():
    parsed = HeuristicAddressParser().parse("Palermo, CABA")
    assert parsed.get("road") in ("", None)
    assert needs_enhancement(parsed) is True


def test_enhancer_se_llama_si_falta_road():
    fake = CountingEnhancer()
    parser = EnhancingAddressParser(HeuristicAddressParser(), fake)
    parsed = parser.parse(
        "Flat 14B, Shanti Nagar, Near Hanuman Temple, Mumbai 400069")
    assert fake.calls == 1
    assert parser.last_outcome == "helped"
    assert parser.helped == 1
    assert parsed.parser == "enhanced"
    assert parsed.get("road") == "ENHANCED-ROAD"
    assert parsed.get("city") == "EnhancedCity"
    assert any("enhancer consultado" in e for e in parsed.evidence)


def test_enhancer_noop_si_solo_aporta_localidad():
    """city/country no cambian el geocode: es ruido, no 'helped'."""
    class CityOnly:
        name = "fake-libpostal"

        def available(self):
            return True

        def parse(self, text, context=None):
            return ParsedAddress(
                text=text, parser=self.name,
                components={"city": "CABA", "country": "Argentina"},
            )

    parser = EnhancingAddressParser(HeuristicAddressParser(), CityOnly())
    parsed = parser.parse("Manzana 12 Casa 5, Barrio Norte")
    assert parser.enhancer_calls == 1
    assert parser.last_outcome == "noop"
    assert parser.helped == 0
    assert parser.noop == 1
    assert parsed.get("road") in ("", None)
    stats = parser.stats()
    assert stats["helped"] == 0 and stats["noop"] == 1


def test_enhancer_corrige_road_sospechosa_no_la_deja():
    class SuspiciousPrimary(HeuristicAddressParser):
        name = "suspicious"

        def parse(self, text, context=None):
            return ParsedAddress(
                text=text, parser=self.name,
                components={"road": "Plot No", "house_number": "42"},
            )

    fake = CountingEnhancer()
    parser = EnhancingAddressParser(SuspiciousPrimary(), fake)
    parsed = parser.parse("Plot No. 42, Sector 18, Noida")
    assert fake.calls == 1
    assert parsed.get("road") == "ENHANCED-ROAD"
    assert any("corrigio road" in e for e in parsed.evidence)


def test_enhancer_no_pisa_componentes_buenos():
    """Si el primario ya tiene unit/postcode, el enhancer solo completa huecos."""
    class Partial(HeuristicAddressParser):
        name = "partial"

        def parse(self, text, context=None):
            return ParsedAddress(
                text=text, parser=self.name,
                components={"unit": "14B", "postcode": "400069"},  # sin road
            )

    fake = CountingEnhancer()
    parser = EnhancingAddressParser(Partial(), fake)
    parsed = parser.parse("Flat 14B, Mumbai 400069")
    assert parsed.get("unit") == "14B"
    assert parsed.get("postcode") == "400069"
    assert parsed.get("road") == "ENHANCED-ROAD"


# ---------- flag de entorno ----------

def test_enhancer_rechaza_basura_tipica_de_libpostal():
    """libpostal mete 'flat 14b' como house_number y el nombre como building."""
    from smart_import.addresses.enhance import (
        _plausible_building, _plausible_house_number, _plausible_road,
    )
    assert _plausible_house_number("42")
    assert _plausible_house_number("plot no. 42")
    assert not _plausible_house_number("shanti")
    assert not _plausible_house_number("flat 14b")
    assert not _plausible_building("rahul sharma")
    assert _plausible_building("Cyber Towers")
    assert not _plausible_road("nagar near hanuman temple", None)

    cfg = Config.from_env().replace(libpostal_enabled=False, address_parser="enhanced")
    parser = build_address_parser(cfg)
    assert parser.name == "heuristic"
    described = describe_parsers(cfg)
    assert described["libpostal_enabled"] is False
    assert described["libpostal_as_enhancer"] is False


def test_flag_true_sin_libreria_degrada_a_heuristico(monkeypatch):
    from smart_import.addresses import factory as factory_mod
    monkeypatch.setattr(factory_mod, "is_installed", lambda: False)
    cfg = Config.from_env().replace(libpostal_enabled=True, address_parser="enhanced")
    parser = build_address_parser(cfg)
    assert parser.name == "heuristic"
    assert parser.parse("Av. Corrientes 100").get("house_number") == "100"


def test_flag_true_con_libreria_arma_enhancer():
    if not __import__("smart_import.addresses", fromlist=["is_installed"]).is_installed():
        pytest.skip("libpostal no instalado")
    cfg = Config.from_env().replace(libpostal_enabled=True, address_parser="enhanced")
    parser = build_address_parser(cfg)
    assert parser.name == "enhanced"


# ---------- costo de preguntar por libpostal ----------

def test_preguntar_si_libpostal_esta_no_carga_sus_datos():
    """`import postal` reserva ~2,6 GB y tarda ~3 s.

    `/health` pregunta esto en cada request. Si la respuesta se obtuviera
    importando, un balanceador polleando tumbaria el servicio — y lo haria
    incluso con SMART_IMPORT_LIBPOSTAL_ENABLED=false.
    """
    import inspect

    from smart_import.addresses import libpostal_parser

    fuente = inspect.getsource(libpostal_parser.is_installed)
    assert "find_spec" in fuente
    assert "_parse_address" not in fuente          # no dispara la carga


def test_health_no_carga_libpostal(monkeypatch):
    """El endpoint mas polleado del servicio no puede reservar GB."""
    from fastapi.testclient import TestClient

    from smart_import.addresses import libpostal_parser
    from smart_import.api.app import app

    llamadas = {"n": 0}
    real = libpostal_parser._parse_address

    def espia():
        llamadas["n"] += 1
        return real()

    monkeypatch.setattr(libpostal_parser, "_parse_address", espia)
    cliente = TestClient(app)
    for _ in range(5):
        assert cliente.get("/health").status_code == 200
    assert llamadas["n"] == 0, "health cargo los datos de libpostal"


def test_health_reporta_instalado_y_cargado_por_separado():
    """Son cosas distintas: instalado es barato de saber, cargado cuesta 2,6 GB."""
    from fastapi.testclient import TestClient

    from smart_import.api.app import app

    body = TestClient(app).get("/health").json()["extraction"]
    assert "libpostal_installed" in body
    assert isinstance(body["libpostal_loaded"], bool)
    # que /health no dispare la carga lo verifica el test del espia de arriba;
    # aca solo se comprueba que sean dos datos distintos y observables.
    assert body["libpostal_enabled"] is False
