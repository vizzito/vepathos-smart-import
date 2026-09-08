"""Lo que viaja por la cola es un contrato entre dos versiones del codigo.

Durante un deploy escalonado conviven mensajes viejos con consumidores nuevos y
al reves. Estos tests fijan las dos mitades de esa tolerancia: lo que se ignora
y lo que se rechaza de plano.
"""
import json

import pytest

from smart_import.queue.envelope import (
    GEOCODE, MAX_BYTES, NORMALIZE, InvalidTask, Task, geocode_task, normalize_task,
)


def test_ida_y_vuelta():
    task = normalize_task("imp_abc", phone_region="AR", diagnostics=True)
    vuelta = Task.from_bytes(task.as_bytes())

    assert vuelta == task
    assert vuelta.params["phone_region"] == "AR"
    assert vuelta.attempt == 1


def test_una_clave_que_no_conocemos_no_rompe_nada():
    """La escribio una version mas nueva; este consumidor la ignora y trabaja."""
    cuerpo = json.dumps({"type": NORMALIZE, "job_id": "imp_1",
                         "prioridad_del_futuro": 7}).encode()

    task = Task.from_bytes(cuerpo)

    assert task.type == NORMALIZE
    assert task.attempt == 1, "el default tiene que cubrir lo que falta"


@pytest.mark.parametrize("cuerpo", [
    b"no soy json",
    b"[]",
    b'"un string"',
    json.dumps({"type": "borrar_todo", "job_id": "imp_1"}).encode(),
    json.dumps({"type": NORMALIZE}).encode(),
    json.dumps({"type": NORMALIZE, "job_id": ""}).encode(),
    json.dumps({"type": NORMALIZE, "job_id": "imp_1", "params": "no"}).encode(),
    json.dumps({"type": NORMALIZE, "job_id": "imp_1", "attempt": 0}).encode(),
])
def test_lo_que_no_se_entiende_se_rechaza(cuerpo):
    """`InvalidTask` es la señal de "a la DLQ": reintentarlo daria lo mismo."""
    with pytest.raises(InvalidTask):
        Task.from_bytes(cuerpo)


def test_por_la_cola_no_viajan_archivos():
    """El alambre-trampa: si alguien mete el CSV en el mensaje, revienta aca."""
    gordo = normalize_task("imp_1", contenido="x" * (MAX_BYTES + 1))

    with pytest.raises(InvalidTask, match="no archivos|viajan referencias"):
        gordo.as_bytes()


def test_un_mensaje_gigante_recibido_tampoco_se_procesa():
    with pytest.raises(InvalidTask):
        Task.from_bytes(b'{"type": "normalize", "job_id": "x", "params": {"a": "'
                        + b"y" * MAX_BYTES + b'"}}')


def test_el_reintento_es_otra_tarea_con_el_mismo_id():
    """El `task_id` sobrevive para poder seguir una tarea entre reintentos."""
    original = geocode_task("imp_9", origin_lat=-34.6)

    siguiente = original.reintento("redis no respondio")

    assert siguiente.attempt == 2
    assert siguiente.task_id == original.task_id
    assert siguiente.error == "redis no respondio"
    assert original.attempt == 1, "la tarea original no se muta"
    assert siguiente.type == GEOCODE
