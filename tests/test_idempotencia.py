"""Una tarea entregada dos veces no puede hacer el trabajo dos veces.

La cola es at-least-once y redeliverea tambien cuando se corta el canal ANTES
del acuse, con el trabajo ya terminado y guardado. El lock de ejecucion
(`claim_run`) cubre a los duplicados SIMULTANEOS, pero no a este caso: el
mensaje puede reaparecer horas despues, cuando el lock ya se solto solo.

El dano concreto que evita esto: `run_normalize_job` reescribe `report` entero y
devuelve el estado a `normalized`. Si el usuario ya geocodifico, un normalize
repetido borra el resultado del geocode del estado del job — el CSV con
coordenadas sigue en disco, pero el job dice que nunca se geocodifico.
"""
import logging

import pytest

from smart_import.artifacts.local import LocalArtifactStore
from smart_import.config import Config
from smart_import.jobs import COMPLETED, NORMALIZED, JobStore
from smart_import.queue.envelope import geocode_task, normalize_task
from smart_import.worker.handlers import WorkerContext
from smart_import.worker.tasks import JobDesaparecido, run_task


@pytest.fixture
def ctx(tmp_path):
    cfg = Config.from_env().replace(role="worker", work_dir=str(tmp_path))
    return WorkerContext(cfg=cfg, store=JobStore(tmp_path),
                         artifacts=LocalArtifactStore(tmp_path),
                         logger=logging.getLogger("test"))


@pytest.fixture
def espia(monkeypatch):
    """Cuenta cuantas veces se llamo de verdad al pipeline."""
    llamadas = {"normalize": 0, "geocode": 0}

    def falso_normalize(_ctx, job, *a, **k):
        llamadas["normalize"] += 1
        job.report = {"rows_output": 10, "needs_geocode": 4}
        job.normalized_path = "artifact://x/flat"
        job.touch(NORMALIZED)
        _ctx.store.save(job)
        return job.as_dict()

    def falso_geocode(_ctx, job_id, *a, **k):
        llamadas["geocode"] += 1
        job = _ctx.store.get(job_id)
        job.geocoded_path = "artifact://x/geocoded"
        job.report = {**job.report, "needs_geocode": 0}
        job.touch(COMPLETED)
        _ctx.store.save(job)

    monkeypatch.setattr("smart_import.worker.tasks.run_normalize_job", falso_normalize)
    monkeypatch.setattr("smart_import.worker.tasks.run_geocode_job", falso_geocode)
    return llamadas


def test_el_mismo_normalize_dos_veces_se_hace_una(ctx, espia):
    job = ctx.store.create("entregas.csv", "vepathos_flat_v1")
    tarea = normalize_task(job.id)

    run_task(ctx, tarea)
    run_task(ctx, tarea)                     # el acuse se perdio, no el trabajo

    assert espia["normalize"] == 1


def test_un_normalize_repetido_no_borra_el_geocode_ya_hecho(ctx, espia):
    """El dano real que motiva la guarda."""
    job = ctx.store.create("entregas.csv", "vepathos_flat_v1")
    normalizar = normalize_task(job.id)
    run_task(ctx, normalizar)

    geocodificar = geocode_task(job.id, origin_lat=-34.6, origin_lon=-58.4)
    run_task(ctx, geocodificar)
    assert ctx.store.get(job.id).status == COMPLETED

    run_task(ctx, normalizar)                # redelivery del normalize original

    despues = ctx.store.get(job.id)
    assert despues.status == COMPLETED, "el normalize repetido pisó el geocode"
    assert despues.geocoded_path
    assert despues.report["needs_geocode"] == 0


def test_un_reintento_legitimo_si_rehace_el_trabajo(ctx, espia):
    """Otro `task_id` es otra tarea: `PUT /mapping` depende de esto.

    Si la guarda mirara solo "el job ya tiene salida", corregir el mapping y
    re-normalizar dejaria de funcionar.
    """
    job = ctx.store.create("entregas.csv", "vepathos_flat_v1")
    run_task(ctx, normalize_task(job.id))
    run_task(ctx, normalize_task(job.id))    # otra tarea, mismo job

    assert espia["normalize"] == 2


def test_una_tarea_que_murio_a_la_mitad_se_rehace(ctx, espia, monkeypatch):
    """La marca sola no alcanza: se escribe ANTES de trabajar.

    Sin exigir tambien un estado terminal, una tarea que se cayo despues de
    marcarse nunca se volveria a hacer y el job quedaria a medias para siempre.
    """
    job = ctx.store.create("entregas.csv", "vepathos_flat_v1")
    tarea = normalize_task(job.id)
    real = __import__("smart_import.worker.tasks", fromlist=["x"]).run_normalize_job
    primera = {"si": True}

    def se_cae_la_primera_vez(_ctx, job, *a, **k):
        if primera["si"]:
            primera["si"] = False
            job.normalize_task_id = tarea.task_id
            _ctx.store.save(job)                   # se marco...
            raise RuntimeError("se corto la luz")  # ...pero no termino
        return real(_ctx, job, *a, **k)

    monkeypatch.setattr("smart_import.worker.tasks.run_normalize_job",
                        se_cae_la_primera_vez)
    with pytest.raises(RuntimeError):
        run_task(ctx, tarea)
    assert espia["normalize"] == 0

    run_task(ctx, tarea)                      # el redelivery SI tiene que rehacerlo
    assert espia["normalize"] == 1


def test_el_mismo_geocode_dos_veces_se_hace_una(ctx, espia):
    job = ctx.store.create("entregas.csv", "vepathos_flat_v1")
    run_task(ctx, normalize_task(job.id))
    tarea = geocode_task(job.id, origin_lat=-34.6, origin_lon=-58.4)

    run_task(ctx, tarea)
    run_task(ctx, tarea)

    assert espia["geocode"] == 1


def test_un_job_borrado_no_revive(ctx, espia):
    job = ctx.store.create("entregas.csv", "vepathos_flat_v1")
    tarea = normalize_task(job.id)
    ctx.store.delete(job.id)

    with pytest.raises(JobDesaparecido):
        run_task(ctx, tarea)
    assert espia["normalize"] == 0


def test_una_operacion_antigua_no_pisa_un_mapping_posterior(ctx, espia):
    job = ctx.store.create('entregas.csv', 'vepathos_flat_v1')
    old, latest = normalize_task(job.id), normalize_task(job.id)
    job.operation_task_id = latest.task_id
    ctx.store.save(job)
    run_task(ctx, latest)
    run_task(ctx, old)
    assert espia['normalize'] == 1
    assert ctx.store.get(job.id).normalize_task_id == latest.task_id
