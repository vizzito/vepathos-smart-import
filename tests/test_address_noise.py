"""Variantes ruidosas de direcciones para probar parser/enhance."""
from smart_import.tools.address_corpus import oa_row_to_record
from smart_import.tools.address_noise import (
    expand_records_with_noise,
    _variants_level1,
    _variants_level3,
    _variants_level5,
)
import random


def _defensa_record():
    return oa_row_to_record(
        {
            "street": "Defensa",
            "number": "800",
            "city": "Ciudad Autónoma de Buenos Aires",
            "region": "CABA",
            "postcode": "C1065",
            "_lat_f": "-34.6170623",
            "_lng_f": "-58.3716318",
            "hash": "oa_caba_001",
        },
        source="fixture:argentina",
        style="oa_default",
        country="AR",
    )


def test_level1_solo_calle_y_altura():
    rec = _defensa_record()
    variants = _variants_level1(rec, random.Random(0))
    assert variants
    assert all("800" in v and "Defensa" in v for v in variants)
    assert all("C1065" not in v for v in variants)
    assert all("Argentina" not in v for v in variants)


def test_level3_permuta_ciudad_pais():
    rec = _defensa_record()
    variants = _variants_level3(rec, random.Random(1), max_permutations=4)
    assert len(variants) >= 2
    assert any("argentina" in v.lower() for v in variants)
    assert any("Ciudad Autónoma" in v for v in variants)


def test_level5_incluye_direccion_canonica():
    rec = _defensa_record()
    variants = _variants_level5(rec, random.Random(2), max_permutations=4)
    assert rec.address in variants


def test_expand_conserva_coords_y_address_clean():
    rec = _defensa_record()
    out = expand_records_with_noise(
        [rec], level=2, seed=42, cumulative=False, max_permutations=2)
    assert len(out) >= 1
    for row in out:
        assert row.lat == rec.lat and row.lng == rec.lng
        assert row.extra.get("address_clean") == rec.address
        assert row.extra.get("noise_level") == 2
        assert row.address != rec.address or "C1065" in row.address


def test_noise_usa_nombre_del_pais_en_el_mix():
    rec = oa_row_to_record(
        {"street": "Rådhusgata", "number": "25", "city": "OSLO",
         "postcode": "0158", "_lat_f": "59.9109", "_lng_f": "10.7370",
         "hash": "no1"},
        source="oa", style="oa_default", country="NO",
    )
    variants = _variants_level3(rec, random.Random(0), max_permutations=4)
    assert any("norway" in v.lower() for v in variants)


def test_noise_rate_invalido():
    import pytest
    rec = _defensa_record()
    with pytest.raises(ValueError):
        expand_records_with_noise([rec], level=1, rate=0.0)
