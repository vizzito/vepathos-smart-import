"""La API no puede hacer I/O sincrono desde el event loop.

Este archivo fija una leccion que el servicio ya aprendio por las malas —el
docstring de `health` la cuenta— y que la migracion a estado distribuido volvio
a poner en riesgo, ahora por el lado de la LECTURA.

Con los jobs en memoria, mirar el estado era gratis. Con el estado en Redis cada
mirada es una llamada de red, y hay dos caminos que miran muchas veces por
segundo: la espera de `POST /imports?wait=` (cada 200 ms) y los SSE (cada 500 ms
POR CLIENTE). Hechas desde la corrutina bloquean el loop entero, y con el loop
bloqueado no se atiende nada: ni /health, ni el polling, ni los otros SSE.
"""
import asyncio
import importlib
import threading
import time

import pytest

from smart_import.jobs import JobStore

api_module = importlib.import_module("smart_import.api.app")


@pytest.fixture
def store_espia(tmp_path, monkeypatch):
    """Un store que anota desde que thread lo leyeron y cuantas veces."""
    class Espia(JobStore):
        def __init__(self, raiz):
            super().__init__(raiz)
            self.threads: set[int] = set()
            self.lecturas = 0
            self.demora = 0.0

        def get(self, job_id):
            self.threads.add(threading.get_ident())
            self.lecturas += 1
            if self.demora:
                time.sleep(self.demora)
            return super().get(job_id)

    espia = Espia(tmp_path)
    monkeypatch.setattr(api_module, "store", espia)
    monkeypatch.setattr(api_module, "_job_read_cache", {})
    return espia


def test_leer_un_job_no_corre_en_el_event_loop(store_espia):
    """La lectura va al threadpool: si no, cada tick congela el servicio."""
    job = store_espia.create("x.csv", "vepathos_flat_v1")

    async def escenario():
        del store_espia.threads
        store_espia.threads = set()
        await api_module._leer_job(job.id)
        return threading.get_ident()

    hilo_del_loop = asyncio.run(escenario())
    assert store_espia.threads, "no se leyo nada"
    assert hilo_del_loop not in store_espia.threads, (
        "el store se leyo desde el thread del event loop")


def test_muchos_espectadores_del_mismo_job_leen_una_sola_vez(store_espia):
    """Diez SSE sobre el mismo import no son diez llamadas a Redis por tick.

    Sin esto, la carga de LECTURA sola alcanza para degradar el servicio: el
    techo de conexiones permite muchos mas SSE abiertos de los que el loop
    puede atender si cada uno trae su propia llamada de red.
    """
    job = store_espia.create("x.csv", "vepathos_flat_v1")

    async def escenario():
        await asyncio.gather(*[api_module._leer_job(job.id) for _ in range(10)])

    asyncio.run(escenario())
    assert store_espia.lecturas <= 2, (
        f"{store_espia.lecturas} lecturas para 10 espectadores del mismo job")


def test_el_cache_caduca_y_no_congela_el_estado(store_espia, monkeypatch):
    """Coalescer no puede volverse mentir: 100 ms y se vuelve a preguntar."""
    job = store_espia.create("x.csv", "vepathos_flat_v1")
    monkeypatch.setattr(api_module, "JOB_READ_CACHE_S", 0.0)

    async def escenario():
        await api_module._leer_job(job.id)
        await api_module._leer_job(job.id)

    asyncio.run(escenario())
    assert store_espia.lecturas == 2


def test_el_cache_no_crece_sin_techo(store_espia, monkeypatch):
    """Un pico de jobs distintos no deja el dict grande para siempre."""
    monkeypatch.setattr(api_module, "JOB_READ_CACHE_MAX", 8)

    async def escenario():
        for i in range(40):
            await api_module._leer_job(f"imp_inexistente{i}")

    asyncio.run(escenario())
    assert len(api_module._job_read_cache) <= 8
