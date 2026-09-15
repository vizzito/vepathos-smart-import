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


# ---------- nivel 6: planilla de despacho ----------

def _rec6(street, number, country):
    from smart_import.tools.address_corpus import CorpusRecord
    return CorpusRecord(address=f"{street} {number}", lat=-37.3, lng=-59.1,
                        street=street, number=number, country=country)


def test_nivel6_produce_las_formas_de_una_planilla_real():
    import random
    from smart_import.tools.address_noise import _variants_level6

    out = dict((kind, text) for text, kind in _variants_level6(
        _rec6("General Rudecindo Alvarado", "471", "AR"), random.Random(3), max_permutations=2))
    assert out["surname"] == "alvarado 471"
    assert out["truncate"] == "General Rudecindo Alv 471"
    assert out["glued"] == "General Rudecindo Alvarado471"
    assert out["caps"] == "GENERAL RUDECINDO ALVARADO 471"
    assert "typo" in out and out["typo"] != "General Rudecindo Alvarado 471"


def test_nivel6_ingles_no_toma_el_tipo_de_via_como_apellido():
    import random
    from smart_import.tools.address_noise import _variants_level6

    out = dict((kind, text) for text, kind in _variants_level6(
        _rec6("Deer Park Drive", "34", "ZA"), random.Random(1), max_permutations=2))
    assert out["surname"] == "34 park drive"
    assert "glued" not in out                  # '34 Deer Park Drive' no se pega


def test_nivel6_marca_el_tipo_de_ruido_en_cada_fila():
    rec = _rec6("Trabajadores Municipales", "1723", "AR")
    out = expand_records_with_noise([rec], level=6, seed=1)
    kinds = {r.extra["noise_kind"] for r in out}
    assert {"surname", "truncate", "typo"} <= kinds
    assert all(r.lat == rec.lat for r in out)  # la verdad no se toca
