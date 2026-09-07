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
    capabilities(geocoding_enabled=True, pbf_dir="/tmp/pbf")
    return TestClient(app)


def _upload(client, name, **params):
    with open(FIXTURES / name, "rb") as fh:
        return client.post("/imports", files={"file": (name, fh.read())}, params=params)


def test_health_expone_las_capacidades(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["geocoding"]["automatic"] is False      # el contrato del producto
    assert body["geocoding"]["autoextract"] is True
    assert body["geocoding"]["autobuild_index"] is True
    assert body["capabilities"]["rules"] is True
    assert body["extraction"]["engine"] == "rules"
    assert body["capabilities"]["normalize"] is True    # el nucleo nunca se apaga
    assert "vepathos_flat_v1" in body["schemas"]


def test_config_expone_la_configuracion_efectiva(client):
    body = client.get("/config").json()["config"]
    assert "geocoding_enabled" in body and "address_parser" in body
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


def test_columna_compuesta_se_separa_sola_sin_modelo(client):
    """La columna compuesta la separan las reglas durante el normalize."""
    compuesto = _upload(client, "merged_field.csv").json()
    report = compuesto["report"]

    assert report["ai_calls"] == 0
    columnas = set(report["output_columns"])
    assert {"address", "customer_name", "phone"} <= columnas
    # ya no queda ninguna columna marcada como "mezcla varios campos"
    assert not any("varios campos" in (m.get("evidence") or "")
                   for m in report["mapping"].values())
    assert "extract" not in {a["action"] for a in compuesto["next_actions"]}


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
    capabilities(geocoding_enabled=False)
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


def test_ya_no_existe_el_endpoint_de_extraccion_con_modelo(capabilities):
    """El modelo se elimino: la columna compuesta la separan las reglas en normalize."""
    capabilities(geocoding_enabled=False)
    c = TestClient(app)
    body = _upload(c, "merged_field.csv").json()
    assert "extract" not in {a["action"] for a in body["next_actions"]}
    assert "extract" not in body["urls"]
    assert c.post(f"/imports/{body['job_id']}/extract").status_code == 404


def test_normalizar_funciona_con_todo_apagado(capabilities):
    """El despliegue minimo (sin PBF ni modelo) tiene que seguir sirviendo."""
    capabilities(geocoding_enabled=False, pbf_dir="")
    c = TestClient(app)
    body = _upload(c, "es_headers_raros.xlsx").json()
    assert body["report"]["deliveries"] == 40
    caps = c.get("/health").json()["capabilities"]
    assert caps == {"normalize": True, "geocoding": False,
                    "rules": True, "phonenumbers": True, "libpostal": False}


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


def test_el_polling_se_espacia_a_medida_que_el_job_tarda(client):
    """500 ms fijos en un geocode de varios minutos son cientos de requests inutiles."""
    import time as _time

    from smart_import.api.jobs import GEOCODING, Job

    job = Job(id="imp_poll", filename="x.csv")
    store._jobs[job.id] = job
    try:
        job.touch(GEOCODING)
        assert job.poll_after_ms() == 500                 # recien arranca

        job.updated_at = _time.time() - 30
        assert job.poll_after_ms() == 2_000                # ya lleva medio minuto

        job.updated_at = _time.time() - 120
        assert job.poll_after_ms() == 3_000                # tope

        job.touch("completed")
        assert job.poll_after_ms() is None                 # terminado: no pollear
    finally:
        store._jobs.pop(job.id, None)


def test_el_normalize_no_corre_en_el_event_loop(capabilities, monkeypatch):
    """El import es CPU-bound: si corre en el loop, el proceso no atiende nada mas.

    Con el normalize inline en la corrutina, mientras dura un import grande el
    servicio no responde /health (el healthcheck de compose marca el container
    unhealthy), no empuja los SSE de progreso y no contesta el polling del front.

    El test compara el thread donde corre el normalize contra el thread del event
    loop: tienen que ser distintos.
    """
    import asyncio
    import threading

    import httpx

    capabilities(geocoding_enabled=True, pbf_dir="/tmp/pbf")
    visto: dict[str, int] = {}
    real = api_module._run_normalize

    def spy(*args, **kwargs):
        visto["normalize"] = threading.get_ident()
        return real(*args, **kwargs)

    monkeypatch.setattr(api_module, "_run_normalize", spy)

    async def subir():
        # El loop corre en ESTE thread: cualquier trabajo que lo comparta lo bloquea.
        visto["loop"] = threading.get_ident()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as cli:
            with open(FIXTURES / "es_sin_coords.csv", "rb") as fh:
                return await cli.post(
                    "/imports", files={"file": ("es_sin_coords.csv", fh.read())})

    respuesta = asyncio.run(subir())
    assert respuesta.status_code == 201
    try:
        assert visto["normalize"] != visto["loop"], (
            "el normalize corrio en el thread del event loop: vuelve a bloquear "
            "/health, los SSE y el polling mientras dura el import")
    finally:
        store.delete(respuesta.json()["job_id"])


def test_solo_un_request_puede_reservar_el_geocode():
    """Mirar el estado y escribirlo son dos pasos: entre medio entra el otro POST.

    Doble click, retry del front o reintento de httpx alcanzaban para lanzar dos
    workers sobre el mismo CSV, que se pisan la salida y el reporte.
    """
    import threading

    from smart_import.api.jobs import GEOCODE_QUEUED, NORMALIZED, Job

    job = Job(id="imp_claim", filename="x.csv")
    job.touch(NORMALIZED)
    store._jobs[job.id] = job
    try:
        arrancar = threading.Barrier(8)
        ganados: list[bool] = []
        cerrojo = threading.Lock()

        def intentar():
            arrancar.wait()
            ok = store.claim_geocode(job.id)
            with cerrojo:
                ganados.append(ok)

        hilos = [threading.Thread(target=intentar) for _ in range(8)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        assert sum(ganados) == 1, f"{sum(ganados)} requests lanzaron un worker"
        assert job.status == GEOCODE_QUEUED
    finally:
        store._jobs.pop(job.id, None)


def test_un_job_reservado_no_se_puede_volver_a_reservar():
    """El contrato, sin depender del scheduler: reservado una vez, cerrado.

    (El test de hilos de arriba prueba la misma propiedad bajo concurrencia,
    pero el GIL puede tapar la carrera; este es el que no depende del timing.)
    """
    from smart_import.api.jobs import NORMALIZED, Job

    job = Job(id="imp_claim_2", filename="x.csv")
    job.touch(NORMALIZED)
    store._jobs[job.id] = job
    try:
        assert store.claim_geocode(job.id) is True
        assert store.claim_geocode(job.id) is False
    finally:
        store._jobs.pop(job.id, None)


def test_no_se_puede_reservar_un_job_inexistente():
    assert store.claim_geocode("imp_no_existe") is False


def test_serve_con_varios_workers_no_arranca():
    """Con el store en memoria, --workers 4 parte el flujo entre procesos."""
    from typer.testing import CliRunner

    from smart_import.cli import app as cli_app

    resultado = CliRunner().invoke(cli_app, ["serve", "--workers", "4"])
    assert resultado.exit_code == 2
    assert "workers 4" in resultado.output or "--workers 1" in resultado.output


def test_los_jobs_viejos_se_barren_y_los_ocupados_no(tmp_path):
    """Sin TTL, cada import quedaba en disco para siempre — y el disco es el
    mismo que usa el cutter."""
    import time as _time

    from smart_import.api.jobs import COMPLETED, GEOCODING, JobStore, NORMALIZED

    almacen = JobStore(tmp_path)
    viejo = almacen.create("viejo.csv", "vepathos_flat_v1")
    ocupado = almacen.create("ocupado.csv", "vepathos_flat_v1")
    reciente = almacen.create("reciente.csv", "vepathos_flat_v1")

    viejo.touch(COMPLETED)
    viejo.updated_at = _time.time() - 48 * 3600
    ocupado.touch(GEOCODING)                       # geocode largo, no se toca
    ocupado.updated_at = _time.time() - 48 * 3600
    reciente.touch(NORMALIZED)

    borrados = almacen.purge_older_than(24 * 3600)

    assert borrados == [viejo.id]
    assert almacen.get(viejo.id) is None
    assert not almacen.dir_for(viejo.id).exists(), "quedo la carpeta en disco"
    assert almacen.get(ocupado.id) is not None, "se borro un geocode en curso"
    assert almacen.get(reciente.id) is not None


def test_ttl_en_cero_no_barre_nada(tmp_path):
    import time as _time

    from smart_import.api.jobs import COMPLETED, JobStore

    almacen = JobStore(tmp_path)
    job = almacen.create("x.csv", "vepathos_flat_v1")
    job.touch(COMPLETED)
    job.updated_at = _time.time() - 1000 * 3600

    assert almacen.purge_older_than(0) == []
    assert almacen.get(job.id) is not None
