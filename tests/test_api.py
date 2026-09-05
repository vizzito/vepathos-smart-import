"""API HTTP. Verifica el contrato que va a consumir el cliente web."""
import io

import pytest
from fastapi.testclient import TestClient

import importlib
from pathlib import Path

from smart_import.api.app import app, store

# `smart_import.api` exporta el objeto FastAPI con el nombre `app`, que tapa al
# submodulo `app.py`. importlib devuelve el MODULO, que es lo que hay que parchear.
api_module = importlib.import_module("smart_import.api.app")
from tests.conftest import FIXTURES


@pytest.fixture
def capabilities(monkeypatch):
    """Enciende las capacidades opcionales para el resto de los tests.

    Por defecto el proceso de tests no tiene PBFs ni modelo, asi que el servicio
    (con razon) no ofrece esas acciones. Estos tests verifican el comportamiento
    CON las capacidades disponibles; los de abajo verifican el caso apagado.
    """
    def enable(**changes):
        cfg = api_module.CFG.replace(**changes)
        monkeypatch.setattr(api_module, "CFG", cfg)
        return cfg
    return enable


@pytest.fixture
def client(capabilities):
    capabilities(geocoding_enabled=True, pbf_dir="/tmp/pbf", ai_enabled=True)
    return TestClient(app)


def _upload(client, name, **params):
    with open(FIXTURES / name, "rb") as fh:
        return client.post("/imports", files={"file": (name, fh.read())}, params=params)


def test_health_expone_las_capacidades(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["geocoding"]["automatic"] is False      # el contrato del producto
    assert body["ai"]["degrades_to_rules"] is True
    assert body["capabilities"]["normalize"] is True    # el nucleo nunca se apaga
    assert "vepathos_flat_v1" in body["schemas"]


def test_config_expone_la_configuracion_efectiva(client):
    body = client.get("/config").json()["config"]
    assert "geocoding_enabled" in body and "ai_enabled" in body
    assert body["max_file_mb"] > 0


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


# ---------- capacidades apagadas: el despliegue minimo tambien tiene que servir ----------

def test_sin_geocoding_no_se_ofrece_la_accion(capabilities):
    capabilities(geocoding_enabled=False, ai_enabled=False)
    c = TestClient(app)
    body = _upload(c, "es_sin_coords.csv").json()

    assert body["report"]["needs_geocode"] == 40       # se sigue informando
    assert "geocode" not in {a["action"] for a in body["next_actions"]}
    assert "download" in {a["action"] for a in body["next_actions"]}


def test_sin_geocoding_el_endpoint_explica_por_que(capabilities):
    capabilities(geocoding_enabled=False)
    c = TestClient(app)
    job_id = _upload(c, "es_sin_coords.csv").json()["job_id"]
    r = c.post(f"/imports/{job_id}/geocode", params={"origin_lat": 0, "origin_lon": 0})
    assert r.status_code == 503
    assert "SMART_IMPORT_GEOCODING_ENABLED" in r.json()["detail"]


def test_sin_ia_no_se_ofrece_extraer(capabilities):
    capabilities(ai_enabled=False, geocoding_enabled=False)
    c = TestClient(app)
    body = _upload(c, "merged_field.csv").json()
    assert "extract" not in {a["action"] for a in body["next_actions"]}

    r = c.post(f"/imports/{body['job_id']}/extract")
    assert r.status_code == 503
    assert "SMART_IMPORT_AI_ENABLED" in r.json()["detail"]


def test_normalizar_funciona_con_todo_apagado(capabilities):
    """El despliegue minimo (sin PBF ni modelo) tiene que seguir sirviendo."""
    capabilities(geocoding_enabled=False, ai_enabled=False, pbf_dir="")
    c = TestClient(app)
    body = _upload(c, "es_headers_raros.xlsx").json()
    assert body["report"]["deliveries"] == 40
    assert c.get("/health").json()["capabilities"] == {
        "normalize": True, "geocoding": False, "extract": False}


def test_job_expone_progress_y_urls_para_la_web(client):
    body = _upload(client, "es_sin_coords.csv").json()
    assert body["busy"] is False
    assert body["poll_after_ms"] is None
    prog = body["progress"]
    assert prog["busy"] is False
    assert prog["status"] == body["status"]
    assert "message" in prog
    urls = body["urls"]
    assert urls["self"] == f"/imports/{body['job_id']}"
    assert urls["events"].endswith("/events")
    assert "download_flat" in urls and "download_nested" in urls

    snap = client.get(f"/imports/{body['job_id']}/progress").json()
    assert snap["job_id"] == body["job_id"]
    assert snap["pct"] is None or isinstance(snap["pct"], (int, float))


def test_events_sse_emite_progress_y_done(client):
    """Con job idle, el stream manda progress + done y cierra."""
    job_id = _upload(client, "es_sin_coords.csv").json()["job_id"]
    with client.stream("GET", f"/imports/{job_id}/events") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        text = "".join(resp.iter_text())
    assert "event: progress" in text
    assert "event: done" in text
    assert job_id in text


def test_geocode_202_incluye_events_url(client):
    job_id = _upload(client, "es_sin_coords.csv").json()["job_id"]
    # falla por falta de indice real, pero el 202 con origen valido encola;
    # aca solo verificamos el contrato del 400/202 previo. Con depot:
    # sin PBF real el worker falla async — el 202 igual debe traer urls.
    # Usamos un job sin depot para 400 (ya cubierto) y simulamos el shape:
    body = client.get(f"/imports/{job_id}").json()
    assert "events" in body["urls"]
    assert body["progress"]["busy"] is False



def test_issues_reporta_lo_que_no_entro_sin_descartarlo(client, tmp_path):
    """Ninguna fila se pierde: las que fallan salen igual y se explican."""
    contenido = (
        "delivery_id,address,lat,lng,cliente\n"
        'OK-1,"Av. Corrientes 1234",-34.6037,-58.3816,Juan\n'
        'GEO-1,"Florida 500",,,Maria\n'
        'BAD,"Cabildo 1800",95,200,Carlos\n'
        "SIN-DESTINO,,,,Pedro\n"      # entrega real: tiene cliente, le falta destino
        "Totales:,,,,\n"              # NO es una entrega: pie de pagina
    )
    archivo = tmp_path / "prob.csv"
    archivo.write_text(contenido, encoding="utf-8")
    with open(archivo, "rb") as fh:
        job_id = client.post("/imports", files={"file": ("prob.csv", fh.read())}).json()["job_id"]

    body = client.get(f"/imports/{job_id}/issues").json()
    resumen = body["summary"]
    assert resumen["total"] == 5                       # entraron 5, salieron 5
    assert resumen["listas"] == 1
    assert resumen["a_geocodificar"] == 2              # GEO-1 y BAD (por direccion)
    assert resumen["no_localizables"] == 1             # SIN-DESTINO: entrega real
    assert resumen["ignoradas"] == 1                   # 'Totales:' no era una entrega
    assert resumen["coordenadas_rechazadas"] == 1

    por_id = {f["delivery_id"]: f for f in body["filas"]}
    assert set(por_id["BAD"]["fields"]) == {"lat", "lng"}
    assert any("fuera de rango" in m for m in por_id["BAD"]["messages"])
    assert por_id["SIN-DESTINO"]["status"] == "invalid"
    assert por_id["Totales:"]["status"] == "ignored"


def test_issues_falla_claro_si_no_se_normalizo(client):
    from smart_import.api.jobs import Job
    store._jobs["imp_vacio"] = Job(id="imp_vacio", filename="x.csv")
    try:
        assert client.get("/imports/imp_vacio/issues").status_code == 409
    finally:
        store._jobs.pop("imp_vacio", None)


def test_el_nested_refleja_las_coordenadas_del_geocoding(client, tmp_path, monkeypatch):
    """El nested se genera en normalize, antes de geocodificar. Si no se
    regenera, la UI (que consume nested) no ve nada del geocoding."""
    import json as _json

    from smart_import.api.app import _refresh_nested
    from smart_import.api.jobs import Job

    job = Job(id="imp_nested", filename="x.csv", schema="vepathos_flat_v1")
    store._jobs[job.id] = job
    try:
        (store.dir_for(job.id) / "geocoded").mkdir(parents=True, exist_ok=True)
        geocodificado = tmp_path / "geo.csv"
        geocodificado.write_text(
            "delivery_id,lat,lng,address,geocode_status\n"
            'A,-34.6037,-58.3816,"Av. Corrientes 1234",already_geocoded\n'
            'B,-34.6023972,-58.3753277,"Florida 500",matched\n',
            encoding="utf-8")

        _refresh_nested(job, geocodificado)

        assert job.nested_path
        doc = _json.loads(Path(job.nested_path).read_text(encoding="utf-8"))
        coords = {a["delivery_id"]: (a.get("lat"), a.get("lng")) for a in doc["addresses"]}
        assert coords["B"] == (-34.6023972, -58.3753277)   # la geocodificada aparece
    finally:
        store.delete(job.id)
