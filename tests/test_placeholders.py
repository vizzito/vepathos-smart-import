"""Los pastes que mostramos como ejemplo tienen que funcionar.

Si el usuario pega el placeholder de la UI (o un demo de `examples/`)
y no salen entregas, el producto se ve roto. No alcanza con que el job
no explote: cada stop del ejemplo tiene que tener nombre, direccion y tel.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from smart_import.pipeline import run_normalize
from tests.conftest import EXAMPLES, FREE_TEXT, SCHEMA

DAY = date(2026, 9, 6)


def _normalize(path: Path, phone_region: str):
    return run_normalize(path, SCHEMA, phone_region=phone_region, service_date=DAY)


def test_placeholder_ui_whatsapp_6_es_el_ejemplo_de_la_caja():
    """El texto corto de la UI (6 bullets + pie 'pegá un chat')."""
    gold = json.loads((FREE_TEXT / "whatsapp_placeholder_6.expected.json").read_text())
    result = _normalize(FREE_TEXT / "whatsapp_placeholder_6.txt", "AR")
    assert result.report["text_mode"] == "free_text"
    assert result.report["deliveries"] == gold["deliveries"]
    assert result.report["invalid_rows"] == 0

    ignorados = " | ".join(
        i["text"] for i in result.report["extraction"]["ignored_samples"]
    )
    for needle in gold["ignored_contains"]:
        assert needle in ignorados, (needle, ignorados)

    extraidos = [d.get("customer_name") for d in result.deliveries]
    assert extraidos == [r["customer_name"] for r in gold["records"]]

    for delivery, esperado in zip(result.deliveries, gold["records"]):
        addr = delivery.get("address") or ""
        assert esperado["address_contains"] in addr, (esperado["customer_name"], addr)
        assert delivery.get("phone") == esperado["phone"]
        hora = esperado["tw_end_hour"]
        if hora is None:
            continue
        window = delivery.get("time_window")
        assert window, esperado["customer_name"]
        assert window["end"].endswith(f"{hora:02d}:00")


@pytest.mark.parametrize("path,region,n,names", [
    (FREE_TEXT / "whatsapp_12.txt", "AR", 12,
     ["Ana Perez", "Juan Lopez", "Maria Gomez", "Carlos Ruiz",
      "Martin Castro", "Facundo Molina", "Nicolas Diaz"]),
    (FREE_TEXT / "whatsapp_12_nobullets.txt", "AR", 12,
     ["Ana Perez", "Facundo Molina"]),
    (FREE_TEXT / "paste_55_caba.txt", "AR", 55, ["Ana Pérez", "Facundo Molina"]),
    (EXAMPLES / "geocode-truth" / "miami_whatsapp_6.txt", "US", 6,
     ["Ana Rivera", "James Lopez", "Maria Gomez", "Carlos Ruiz",
      "Martin Castro", "Facundo Molina"]),
    (EXAMPLES / "force-ai" / "06_paste_ready.txt", "AR", 7,
     ["Laura Méndez", "Carla Benítez", "Emily Johnson"]),
    (EXAMPLES / "force-ai" / "01_dash_name_address_phone.csv", "AR", 5,
     ["Laura Méndez", "Diego Ruiz"]),
    (EXAMPLES / "force-ai" / "02_entregar_a_telefono.csv", "AR", 5, []),
    (EXAMPLES / "force-ai" / "04_marketplace_whatsapp.csv", "AR", 5, []),
    (EXAMPLES / "force-ai" / "05_pipe_messy_en_es.csv", "AR", 5, []),
], ids=[
    "whatsapp_12", "whatsapp_12_nobullets", "paste_55_caba",
    "miami_whatsapp_6", "force_ai_paste_ready", "force_ai_01",
    "force_ai_02", "force_ai_04", "force_ai_05",
])
def test_demos_publicados_salen_completos(path, region, n, names):
    result = _normalize(path, region)
    assert result.report["deliveries"] == n, path.name
    assert result.report["invalid_rows"] == 0, path.name
    got = [d.get("customer_name") for d in result.deliveries]
    for name in names:
        assert name in got, (path.name, name, got)
    for delivery in result.deliveries:
        assert delivery.get("address"), (path.name, delivery)
        assert delivery.get("phone"), (path.name, delivery.get("customer_name"))
