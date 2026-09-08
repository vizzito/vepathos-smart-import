"""Bateria de lenguaje de paqueteria sobre el corpus de `package_cases.py`.

Dos cosas a la vez:

  * un test por caso, para que una mejora en un idioma no rompa otro,
  * un reporte de cobertura por idioma y por patron, que es lo que dice
    *donde* conviene meter mano despues:

        pytest tests/test_package_battery.py::test_reporte_de_cobertura -s
"""
from collections import defaultdict

import pytest

from smart_import.packages import parse_packages
from tests.package_cases import CATALOG, PackageCase

#: tolerancia de peso: 10 g o 0.2%, lo que sea mayor (las conversiones de
#: libras y onzas no dan redondo)
ABS_TOLERANCE = 0.01
REL_TOLERANCE = 0.002


def mismatches(case: PackageCase) -> list[str]:
    """Que NO coincide entre lo que el caso declara y lo que el parser leyo."""
    parse = parse_packages(case.text)
    out: list[str] = []

    if case.empty:
        if not parse.is_empty:
            out.append(f"tendria que estar vacio: quantity={parse.quantity} "
                       f"weight={parse.weight_total_kg} dims={parse.dimensions}")
        return out

    if case.quantity is not None and parse.quantity != case.quantity:
        out.append(f"quantity {parse.quantity} != {case.quantity}")
    for field in ("weight_total_kg", "weight_per_unit_kg", "volume_cm3"):
        expected = getattr(case, field)
        actual = getattr(parse, field)
        if expected is None:
            continue
        if actual is None or abs(actual - expected) > max(
                ABS_TOLERANCE, expected * REL_TOLERANCE):
            out.append(f"{field} {actual} != {expected}")
    if case.packaging and parse.packaging != case.packaging:
        out.append(f"packaging {parse.packaging} != {case.packaging}")
    if case.dimensions and parse.dimensions != case.dimensions:
        out.append(f"dimensions {parse.dimensions} != {case.dimensions}")
    return out


@pytest.mark.parametrize("case", CATALOG, ids=lambda c: c.id)
def test_caso(case: PackageCase):
    problems = mismatches(case)
    assert not problems, f"{case.text!r} -> " + "; ".join(problems)


def test_ningun_falso_positivo():
    """Lo mas caro de esta capa: leer un bulto donde hay una calle.

    Se chequea aparte del parametrizado para que el numero quede visible: si
    algun dia baja, es porque el vocabulario se abrio de mas.
    """
    negatives = [c for c in CATALOG if c.empty]
    assert len(negatives) >= 20, "la bateria anti-falso-positivo no puede achicarse"
    failed = [(c.id, c.text) for c in negatives if mismatches(c)]
    assert not failed, f"encontro bultos donde no hay: {failed}"


# ---------- de la frase al schema, por el pipeline entero ----------

#: una direccion por idioma, para que el segmento sea una entrega de verdad
ADDRESSES = {
    "es": "Av. Corrientes 1234, CABA",
    "en": "350 5th Ave, New York",
    "pt": "Rua Augusta 1500, Sao Paulo",
    "fr": "12 Rue de Rivoli, Paris",
    "it": "Via Roma 25, Milano",
    "de": "Hauptstrasse 12, Berlin",
    "nl": "Damrak 1, Amsterdam",
}

END_TO_END = [c for c in CATALOG
              if not c.empty and (c.quantity is not None
                                  or c.weight_total_kg is not None)]


@pytest.fixture(scope="module")
def free_text_extractor():
    from smart_import.config import Config
    from smart_import.extraction.context import ExtractionContext
    from smart_import.extraction.free_text import FreeTextExtractor
    return FreeTextExtractor(Config.from_env(), ExtractionContext(phone_region="AR"))


@pytest.mark.parametrize("case", END_TO_END, ids=lambda c: c.id)
def test_la_frase_llega_al_schema(free_text_extractor, case: PackageCase):
    """El parser puede tener razon y el pipeline perderlo igual.

    Aca la frase viaja con una direccion al lado: si el peel de bultos se come
    la calle, o si la direccion se come los bultos, esto lo cachea.
    """
    address = ADDRESSES[case.lang]
    record = free_text_extractor.run_value(f"{address}, {case.text}")

    if case.quantity is not None:
        assert record.get("quantity") == case.quantity
    if case.weight_total_kg is not None:
        # El schema guarda el peso de UN bulto, siempre: es lo que se relee
        # cuando el flat vuelve a entrar como tabular despues de geocodificar.
        expected = case.weight_per_unit_kg
        if expected is None:
            expected = (case.weight_total_kg / case.quantity if case.quantity
                        else case.weight_total_kg)
        assert record.get("weight_kg") == pytest.approx(
            expected, abs=ABS_TOLERANCE, rel=REL_TOLERANCE), \
            "weight_kg del schema es el peso por bulto"
    # la direccion tiene que sobrevivir al peel de bultos
    street = address.split(",")[0].split()
    assert any(token in (record.get("address") or "") for token in street), \
        f"la capa de bultos se comio la direccion: {record.get('address')!r}"


def test_reporte_de_cobertura(capsys):
    """No falla nunca: imprime el mapa. Correr con -s para verlo."""
    by_tag: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_lang: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    failures: list[tuple[PackageCase, list[str]]] = []

    for case in CATALOG:
        problems = mismatches(case)
        ok = 0 if problems else 1
        for tag in case.tags or ("sin-tag",):
            by_tag[tag][0] += ok
            by_tag[tag][1] += 1
        by_lang[case.lang][0] += ok
        by_lang[case.lang][1] += 1
        if problems:
            failures.append((case, problems))

    total = len(CATALOG)
    with capsys.disabled():
        print(f"\n=== paqueteria: {total - len(failures)}/{total} casos ===")
        print("  idioma  " + "  ".join(
            f"{lang}={hits}/{n}" for lang, (hits, n) in sorted(by_lang.items())))
        print("  patrones:")
        for tag, (hits, n) in sorted(by_tag.items(),
                                     key=lambda kv: (kv[1][0] / kv[1][1], kv[0])):
            mark = "" if hits == n else "   <-- hay margen"
            print(f"    {tag:<22} {hits}/{n}{mark}")
        for case, problems in failures:
            print(f"    ! [{case.id}] {case.text!r}: {'; '.join(problems)}")
