"""API HTTP. Verifica el contrato que va a consumir el cliente web."""
import io

import pytest
from fastapi.testclient import TestClient

from smart_import.api.app import app, store
from tests.conftest import FIXTURES


@pytest.fixture
def client():
    return TestClient(app)


def _upload(client, name, **params):
    with open(FIXTURES / name, "rb") as fh:
        return client.post("/imports", files={"file": (name, fh.read())}, params=params)


def test_health_expone_las_capacidades(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["geocoding"]["automatic"] is False      # el contrato del producto
    assert body["ai"]["degrades_to_rules"] is True
    assert "vepathos_flat_v1" in body["schemas"]


def test_import_normaliza_y_devuelve_el_mapping(client):
    body = _upload(client, "es_headers_raros.xlsx").json()
    assert body["status"] in ("normalized", "needs_mapping_review")
    mapping = {c: m["target"] for c, m in body["report"]["mapping"].items()}
    assert mapping["Dest."] == "address"
    assert mapping["Cant bultos"] == "quantity"
    assert body["report"]["deliveries"] > 0


def test_import_nunca_geocodifica_solo(client):
    """La accion de geocodificar se OFRECE, no se ejecuta."""
    body = _upload(client, "es_sin_coords.csv").json()
    assert body["report"]["needs_geocode"] == 40
    assert body["report"]["valid_rows"] == 0
    assert body["geocode"] is None                     # no se toco nada
    actions = {a["action"] for a in body["next_actions"]}
    assert "geocode" in actions


def test_ofrece_extraer_solo_si_hay_columna_compuesta(client):
    compuesto = _upload(client, "merged_field.csv").json()
    limpio = _upload(client, "ref_us_seattle.xlsx").json()
    assert "extract" in {a["action"] for a in compuesto["next_actions"]}
    assert "extract" not in {a["action"] for a in limpio["next_actions"]}


def test_rechaza_extensiones_no_soportadas(client):
    r = client.post("/imports", files={"file": ("virus.exe", io.BytesIO(b"MZ"))})
    assert r.status_code == 415


def test_rechaza_archivos_demasiado_grandes(client, monkeypatch):
    from smart_import import config as config_mod
    monkeypatch.setenv("SMART_IMPORT_MAX_FILE_MB", "0.0001")
    r = client.post("/imports", files={"file": ("big.csv", b"a,b\n" + b"1,2\n" * 20000)})
    assert r.status_code == 413


def test_descarga_plano_y_anidado(client):
    job_id = _upload(client, "ref_us_seattle.xlsx").json()["job_id"]

    flat = client.get(f"/imports/{job_id}/download", params={"format": "flat"})
    assert flat.status_code == 200
    assert flat.text.splitlines()[0].startswith("delivery_id,lat,lng,address")

    nested = client.get(f"/imports/{job_id}/download", params={"format": "nested"})
    assert nested.status_code == 200
    doc = nested.json()
    assert "addresses" in doc and len(doc["addresses"]) == 50
    assert doc["addresses"][0]["packages"]


def test_corregir_el_mapping_re_normaliza(client):
    job_id = _upload(client, "preamble_dirty.xlsx").json()["job_id"]
    body = client.put(f"/imports/{job_id}/mapping",
                      json={"Codigo interno": "reference"}).json()
    assert body["report"]["mapping"]["Codigo interno"]["target"] == "reference"
    assert body["report"]["mapping"]["Codigo interno"]["method"] == "manual"


def test_geocode_requiere_saber_que_region_usar(client):
    job_id = _upload(client, "es_sin_coords.csv").json()["job_id"]
    r = client.post(f"/imports/{job_id}/geocode")
    assert r.status_code == 400
    assert "depot" in r.json()["detail"]


def test_no_se_puede_geocodificar_sin_normalizar(client):
    r = client.post("/imports/imp_inexistente/geocode", params={"origin_lat": 0, "origin_lon": 0})
    assert r.status_code == 404


def test_preview_para_la_ui(client):
    job_id = _upload(client, "ref_ar_orders.csv").json()["job_id"]
    body = client.get(f"/imports/{job_id}/preview", params={"limit": 3}).json()
    assert len(body["rows"]) == 3
    assert "delivery_id" in body["columns"]
    assert body["mapping"]


def test_borrar_un_job(client):
    job_id = _upload(client, "tabs.tsv").json()["job_id"]
    assert client.delete(f"/imports/{job_id}").status_code == 204
    assert client.get(f"/imports/{job_id}").status_code == 404
    assert store.get(job_id) is None
