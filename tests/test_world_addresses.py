"""Cobertura medible del heuristico por zona del mundo.

Cada caso declara:
  * region     — mercado / estilo de direccion
  * expected   — componentes que el heuristico DEBE sacar (Fase 0)
  * needs_libpostal — si el gate pediria enhancer (para la futura medicion)

Correr:
  pytest -q tests/test_world_addresses.py
  pytest -q tests/test_world_addresses.py -k india   # una zona
"""
from __future__ import annotations

import pytest

from smart_import.addresses import (
    HeuristicAddressParser, enhancement_reason, needs_enhancement,
)

# (region, texto, expected_components, needs_libpostal)
WORLD_CASES = [
    # ----- Argentina / LatAm tipico: heuristico basta -----
    ("AR", "Av. Corrientes 100, CABA",
     {"road": "Av. Corrientes", "house_number": "100"}, False),
    ("AR", "Malabia 1136, Palermo",
     {"road": "Malabia", "house_number": "1136", "suburb": "Palermo"}, False),
    ("AR", "11 de Septiembre Nro 1913",
     {"road": "11 de Septiembre", "house_number": "1913"}, False),
    ("AR", "Av. Pueyrredon 359, Recoleta, 1425",
     {"road": "Av. Pueyrredon", "house_number": "359", "postcode": "1425"}, False),
    ("AR", "Yerbal, CABA, Argentina",
     {"road": "Yerbal"}, False),
    ("AR", "Av. Cabildo, CABA",
     {"road": "Av. Cabildo"}, False),

    # ----- Colombia: via numerada -----
    ("CO", "Calle 50 nro 1234, Medellin",
     {"road": "Calle 50", "house_number": "1234"}, False),
    ("CO", "Carrera 7 # 45, Bogota",
     {"road": "Carrera 7", "house_number": "45"}, False),

    # ----- Brasil: coma entre calle y altura -----
    ("BR", "Av. Paulista, 1578, Sao Paulo",
     {"road": "Av. Paulista", "house_number": "1578"}, False),
    ("BR", "Av. Paulista, 1578",
     {"road": "Av. Paulista", "house_number": "1578"}, False),
    ("BR", "Rua Augusta, 1500, Sao Paulo",
     {"road": "Rua Augusta", "house_number": "1500"}, False),

    # ----- USA / EN: ordinales y unit delante -----
    ("US", "1171 1st Ave, Seattle",
     {"road": "1st Ave", "house_number": "1171"}, False),
    ("US", "716 1st Ave, Seattle",
     {"road": "1st Ave", "house_number": "716"}, False),
    ("US", "350 5th Avenue, New York",
     {"road": "5th Avenue", "house_number": "350"}, False),
    ("US", "Apt 4B, 350 5th Ave, New York",
     {"road": "5th Ave", "house_number": "350", "unit": "4B"}, False),
    ("US", "857 Yesler Way, Seattle",
     {"road": "Yesler Way", "house_number": "857"}, False),

    # ----- India: mixtas (algunas con calle, otras no) -----
    ("IN", "23 MG Road, Bengaluru 560001",
     {"road": "MG Road", "house_number": "23", "postcode": "560001"}, False),
    ("IN", "Flat 14B, Shanti Nagar, Near Hanuman Temple, Andheri East, Mumbai 400069",
     {"unit": "14B", "postcode": "400069", "landmark": "Hanuman Temple"}, True),
    ("IN", "Plot No. 42, Sector 18, Noida 201301",
     {"postcode": "201301"}, True),
    ("IN", "7th Floor, Cyber Towers, Hitech City, Hyderabad 500081",
     {"postcode": "500081"}, True),

    # ----- Mexico -----
    ("MX", "Av. Insurgentes Sur 1234, Ciudad de Mexico",
     {"road": "Av. Insurgentes Sur", "house_number": "1234"}, False),

    # ----- Chile -----
    ("CL", "Av. Providencia 2124, Santiago",
     {"road": "Av. Providencia", "house_number": "2124"}, False),

    # ----- Espania -----
    ("ES", "Calle Gran Via 28, Madrid",
     {"road": "Calle Gran Via", "house_number": "28"}, False),

    # ----- LatAm sin calle clara (manzana/lote): gate SI -----
    ("AR", "Manzana 12 Casa 5, Barrio Norte",
     {}, True),
    ("UY", "Solar 8 Manzana 3, Ciudad de la Costa",
     {}, True),
]


@pytest.fixture(scope="module")
def parser():
    return HeuristicAddressParser()


@pytest.mark.parametrize(
    "region,texto,expected,want_enhance",
    WORLD_CASES,
    ids=[f"{r}:{t[:40]}" for r, t, *_ in WORLD_CASES],
)
def test_componentes_por_zona(parser, region, texto, expected, want_enhance):
    parsed = parser.parse(texto)
    for name, value in expected.items():
        assert parsed.get(name) == value, (
            f"[{region}] {name}: got {parsed.get(name)!r} want {value!r} "
            f"in {parsed.components}")
    assert needs_enhancement(parsed) is want_enhance, (
        f"[{region}] gate={enhancement_reason(parsed)!r} "
        f"road={parsed.get('road')!r} components={parsed.components}")


def test_resumen_medible_por_region(parser):
    """Tabla compacta: cobertura de road y fraccion que pediria libpostal.

    No falla sola: es el artefacto para comparar antes/despues de Fase 0 y
    cuando se mida el microservicio. Si queres verla: pytest -s -k resumen.
    """
    by_region: dict[str, dict[str, int]] = {}
    for region, texto, _expected, _want in WORLD_CASES:
        bucket = by_region.setdefault(region, {
            "n": 0, "with_road": 0, "would_call_libpostal": 0,
        })
        parsed = parser.parse(texto)
        bucket["n"] += 1
        if parsed.get("road") and not needs_enhancement(parsed):
            bucket["with_road"] += 1
        if needs_enhancement(parsed):
            bucket["would_call_libpostal"] += 1

    lines = [f"{'region':6} {'n':>3} {'road_ok':>8} {'libpostal%':>10}"]
    for region in sorted(by_region):
        b = by_region[region]
        road_pct = 100.0 * b["with_road"] / b["n"]
        call_pct = 100.0 * b["would_call_libpostal"] / b["n"]
        lines.append(f"{region:6} {b['n']:>3} {road_pct:>7.0f}% {call_pct:>9.0f}%")
    print("\n" + "\n".join(lines))

    # Invariantes globales medibles (Fase 0)
    total = sum(b["n"] for b in by_region.values())
    calls = sum(b["would_call_libpostal"] for b in by_region.values())
    assert total >= 20
    # LatAm tipico + EN ordinales ya no deberían pedir enhancer
    assert by_region["AR"]["would_call_libpostal"] <= 1   # solo manzana
    assert by_region["US"]["would_call_libpostal"] == 0
    assert by_region["BR"]["would_call_libpostal"] == 0
    # India sigue siendo el hueco real
    assert by_region["IN"]["would_call_libpostal"] >= 2
    assert calls / total < 0.35   # enhancer es excepcion, no regla
