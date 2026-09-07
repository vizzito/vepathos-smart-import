"""Address por partes → compose → string unico (cualquier formato)."""
from __future__ import annotations

import json
from pathlib import Path

from smart_import.config import Config
from smart_import.mapping import build_mapper
from smart_import.normalization.address import (
    apply_composed_address, compose_address_from_parts, house_number_already_in_street,
)
from smart_import.normalization.row_normalizer import RowNormalizer, STATUS_NEEDS_GEOCODE
from smart_import.pipeline import run_normalize
from smart_import.readers import read_any
from smart_import.schemas import TargetSchema

SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "vepathos_flat_v1.json"


def test_compose_street_number_locality():
    values = {
        "address": "Av. Nazca",
        "house_number": "400",
        "zone": "Flores",
        "city": "CABA",
        "postcode": "1405",
        "country": "AR",
    }
    out = compose_address_from_parts(values)
    assert out == "Av. Nazca 400, Flores, CABA, 1405, AR"


def test_compose_us_altura_va_adelante():
    """'NE 1st Ave 350' hace que el parser coma el ordinal. US = house first."""
    out = compose_address_from_parts({
        "address": "NE 1st Ave",
        "house_number": "350",
        "city": "Miami",
        "country": "United States",
    })
    assert out.startswith("350 NE 1st Ave")
    assert out.index("350") < out.index("NE")
    assert out.endswith("United States")


def test_compose_no_duplica_altura_ya_en_calle():
    assert house_number_already_in_street("Av. Nazca 400", "400")
    out = compose_address_from_parts({
        "address": "Av. Nazca 400",
        "house_number": "400",
        "city": "CABA",
    })
    assert out is not None
    assert out.count("400") == 1


def test_compose_sin_calle_no_inventa():
    assert compose_address_from_parts({"city": "CABA", "postcode": "1405"}) is None


def test_apply_composed_enriquece():
    values = {"address": "Av. La Plata", "house_number": "800", "zone": "Caballito"}
    assert apply_composed_address(values) is True
    assert values["address"].startswith("Av. La Plata 800")
    assert "Caballito" in values["address"]


def test_tiendanube_json_mapea_partes_y_compone(tmp_path: Path):
    payload = {
        "orders": [
            {
                "id": 871254205,
                "number": 1005,
                "contact_name": "Diego Martínez",
                "contact_email": "diego@example.com",
                "weight": "0.600",
                "shipping_address": {
                    "address": "Av. Nazca",
                    "number": "400",
                    "locality": "Flores",
                    "city": "CABA",
                    "province": "CABA",
                    "zipcode": "1405",
                    "country": "AR",
                    "name": "Diego Martínez",
                },
                "products": [{"name": "Mochila", "quantity": "1", "weight": "0.600"}],
            },
            {
                "id": 871254203,
                "number": 1003,
                "contact_name": "Carlos Ruiz",
                "shipping_address": {
                    "address": "Av. La Plata",
                    "number": "800",
                    "locality": "Caballito",
                    "city": "CABA",
                    "zipcode": "1235",
                    "country": "AR",
                },
                "products": [{"name": "X", "quantity": "1", "weight": "0.4"}],
            },
        ]
    }
    path = tmp_path / "tienda.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    schema = TargetSchema.load(SCHEMA)
    table = read_any(path)
    mapping = build_mapper(Config.from_env()).detect(table, schema)

    by = mapping.by_target()
    assert by.get("house_number") == "shipping_address.number"
    assert by.get("city") in ("city", "shipping_address.city")
    assert by.get("postcode") in ("zipcode", "shipping_address.zipcode")
    # nro de ORDEN no debe ser la altura
    assert by.get("house_number") != "number"

    outcome = RowNormalizer(schema).run(table, mapping)
    assert len(outcome.rows) == 2
    diego = next(r for r in outcome.rows if r.values.get("customer_name") == "Diego Martínez")
    addr = diego.values["address"]
    assert "Nazca" in addr and "400" in addr
    assert "Flores" in addr or "CABA" in addr
    assert diego.status == STATUS_NEEDS_GEOCODE

    carlos = next(r for r in outcome.rows if r.values.get("customer_name") == "Carlos Ruiz")
    assert "La Plata" in carlos.values["address"] and "800" in carlos.values["address"]


def test_csv_partes_igual_compose(tmp_path: Path):
    csv = (
        "street,street_number,city,zipcode,customer_name\n"
        "Av. Directorio,2100,CABA,1406,Ana Lopez\n"
    )
    path = tmp_path / "parts.csv"
    path.write_text(csv, encoding="utf-8")
    result = run_normalize(path, schema_path=SCHEMA, emit=())
    assert result.outcome.rows
    row = result.outcome.rows[0]
    assert "Directorio" in row.values["address"]
    assert "2100" in row.values["address"]
