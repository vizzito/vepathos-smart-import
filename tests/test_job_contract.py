"""El contrato de `Job.as_dict()`, congelado.

Este archivo es un alambre-trampa para la migracion a estado distribuido. Hoy el
`Job` es un objeto en memoria que la API muta y lee en el mismo proceso, asi que
nada verifica que su forma sobreviva un ida y vuelta. Cuando el estado pase a
Redis, el `Job` se va a serializar y reconstruir, y un campo que se pierda en el
camino NO rompe nada de forma visible: simplemente la UI deja de ver un boton, o
una barra de progreso se queda en cero.

El caso mas caro es `capabilities`. Lo setea la API al crear el job y
`next_actions` lo lee con `.get("geocoding", True)`: si la serializacion lo
pierde, el default `True` hace REAPARECER el boton de geolocalizar en un
despliegue que tiene el geocoding apagado, y el usuario recibe un 503 al
apretarlo. Un default permisivo convierte una perdida de datos en una promesa
falsa, y por eso tiene test propio.
"""
import json
from dataclasses import fields

from smart_import.jobs import (
    COMPLETED, GEOCODE_FAILED, GEOCODING, NEEDS_REVIEW, NORMALIZED, Job,
)

#: Claves que la web consume de `as_dict()`. Sacar una es romper al cliente.
CLAVES_JOB = {
    "job_id", "status", "busy", "filename", "schema", "timezone",
    "created_at", "updated_at", "report", "progress", "geocode", "error",
    "next_actions", "urls", "poll_after_ms",
}

#: Claves del snapshot de avance (poll y SSE comparten esta forma).
CLAVES_PROGRESS = {
    "job_id", "status", "phase", "message", "done", "total", "pct", "eta_s",
    "busy", "detail", "updated_at", "error",
}

CLAVES_URLS = {
    "self", "events", "preview", "download_flat", "download_nested",
    "download_geocoded", "geocode", "mapping",
}


def _job(**kw) -> Job:
    job = Job(id="imp_contrato0001", filename="entregas.xlsx", **kw)
    job.report = {"rows_output": 10, "needs_geocode": 4, "deliveries": 10}
    return job


def test_as_dict_expone_exactamente_las_claves_del_contrato():
    assert set(_job().as_dict()) == CLAVES_JOB


def test_progress_expone_exactamente_las_claves_del_contrato():
    assert set(_job().progress_snapshot()) == CLAVES_PROGRESS


def test_urls_expone_exactamente_las_claves_del_contrato():
    assert set(_job().urls()) == CLAVES_URLS


def test_el_progress_del_as_dict_es_el_mismo_snapshot():
    """La web lee `progress` de adentro del job o `/progress` suelto, indistinto."""
    job = _job()
    assert job.as_dict()["progress"] == job.progress_snapshot()


def test_geocoding_apagado_no_ofrece_la_accion_de_geocodificar():
    """El caso que un default permisivo rompe en silencio.

    Con `capabilities={"geocoding": False}` el boton NO se dibuja, aunque el job
    tenga filas sin coordenadas. Si una serializacion futura pierde el campo,
    `next_actions` vuelve a ofrecerlo y este test es el que avisa.
    """
    job = _job()
    job.status = NORMALIZED
    job.normalized_path = "/tmp/normalized.csv"
    job.capabilities = {"geocoding": False}
    assert job.needs_geocode == 4
    assert "geocode" not in {a["action"] for a in job.next_actions()}

    job.capabilities = {"geocoding": True}
    assert "geocode" in {a["action"] for a in job.next_actions()}


def test_mientras_esta_ocupado_no_se_ofrece_ninguna_accion():
    job = _job()
    job.status = GEOCODING
    assert job.busy is True
    assert job.next_actions() == []


def test_cada_accion_trae_action_y_href():
    """`next_actions` es el contrato de botones: sin href no se puede dibujar."""
    for estado in (NORMALIZED, NEEDS_REVIEW, COMPLETED, GEOCODE_FAILED):
        job = _job()
        job.status = estado
        job.normalized_path = "/tmp/normalized.csv"
        job.nested_path = "/tmp/normalized.nested.json"
        acciones = job.next_actions()
        assert acciones, f"{estado} no ofrece ninguna accion"
        for accion in acciones:
            assert accion["action"] and accion["href"], accion


def test_el_polling_para_cuando_el_job_deja_de_estar_ocupado():
    job = _job()
    job.status = GEOCODING
    assert job.poll_after_ms() is not None
    job.status = COMPLETED
    assert job.poll_after_ms() is None


# ------------------------------------------- el estado que guarda el store


def _job_con_todos_los_campos_puestos() -> Job:
    """Un job con NINGUN campo en su default.

    Asi el ida y vuelta no puede pasar por accidente: si un campo se pierde,
    vuelve como su default y no como el valor que se guardo.
    """
    job = Job(id="imp_estado0001", filename="entregas.xlsx", schema="otro_schema")
    job.status = GEOCODE_FAILED
    job.created_at = 1_700_000_000.0
    job.updated_at = 1_700_000_500.0
    job.phone_region = "AR"
    job.timezone = "America/Argentina/Buenos_Aires"
    job.raw_path = "/data/jobs/imp/raw/entregas.xlsx"
    job.normalized_path = "/data/jobs/imp/normalized/normalized.csv"
    job.nested_path = "/data/jobs/imp/geocoded/geocoded.nested.json"
    job.geocoded_path = "/data/jobs/imp/geocoded/geocoded.csv"
    job.report = {"rows_output": 40, "needs_geocode": 4}
    job.geocode_report = {"matched": 36, "not_found": 4}
    job.geocode_progress = {"phase": "failed", "done": 36, "total": 40}
    job.error = "sin cobertura PBF"
    job.op_started_at = 1_700_000_100.0
    job.capabilities = {"geocoding": False}
    return job


def test_ningun_campo_se_pierde_al_guardar_y_leer():
    """El ida y vuelta completo, campo por campo del dataclass.

    Se recorren con introspeccion a proposito: un campo nuevo entra solo a este
    test, sin que nadie tenga que acordarse de agregarlo.
    """
    original = _job_con_todos_los_campos_puestos()

    revivido = Job.from_state(json.loads(json.dumps(original.as_state())))

    for f in fields(Job):
        assert getattr(revivido, f.name) == getattr(original, f.name), f.name


def test_el_estado_guarda_lo_que_la_vista_de_la_api_no_tiene():
    """`as_dict()` es el contrato con la web y NO alcanza para reconstruir.

    Serializar la vista de la API pierde en silencio los campos internos: sin
    `capabilities` reaparece un boton que este despliegue no puede atender, y
    sin `op_started_at` la barra de progreso se queda sin ETA.
    """
    estado = _job_con_todos_los_campos_puestos().as_state()
    vista = _job_con_todos_los_campos_puestos().as_dict()

    for campo in ("capabilities", "phone_region", "op_started_at", "raw_path"):
        assert campo in estado
        assert campo not in vista


def test_una_clave_desconocida_no_rompe_la_lectura():
    """Durante un deploy conviven dos versiones leyendo el mismo Redis: la vieja
    tiene que poder leer lo que escribio la nueva."""
    estado = {**_job_con_todos_los_campos_puestos().as_state(),
              "campo_del_futuro": "algo"}

    assert Job.from_state(estado).id == "imp_estado0001"


def test_una_clave_que_falta_toma_el_default():
    """Y la nueva tiene que poder leer lo que escribio la vieja."""
    estado = _job_con_todos_los_campos_puestos().as_state()
    del estado["capabilities"]

    assert Job.from_state(estado).capabilities == {"geocoding": True}


def test_el_estado_es_serializable_a_json():
    """Lo que no entra en un JSON no se puede guardar en Redis."""
    json.dumps(_job_con_todos_los_campos_puestos().as_state())
