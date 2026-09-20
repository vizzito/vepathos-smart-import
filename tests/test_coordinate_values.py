"""Coordenadas con coma o con punto: las dos formas, con cualquier cantidad de decimales."""

import pytest

from smart_import.normalization.values import coerce, to_coordinate


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("-37,321830750", -37.32183075),   # Excel es-AR
        ("-37.321830750", -37.32183075),
        ("-37,321", -37.321),              # 3 decimales: NO son miles
        ("-59,128", -59.128),
        ("-34,6", -34.6),
        (" -34.6037 ", -34.6037),
        (-37.5, -37.5),
        ("", None),
        ("sin dato", None),
    ],
)
def test_una_coordenada_se_lee_igual_con_coma_o_con_punto(raw, expected):
    assert to_coordinate(raw) == expected


def test_solo_lat_y_lng_cambian_de_regla():
    # En un peso o un monto, tres digitos tras la coma siguen siendo miles.
    assert coerce("1,250", "float", "weight_kg") == 1250.0
    assert coerce("1,250", "float", "lat") == 1.25
    assert coerce("-58,381", "float", "lng") == -58.381
    assert coerce("1,250", "float") == 1250.0
