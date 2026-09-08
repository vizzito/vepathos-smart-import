"""El contrato del almacen de artefactos.

Hoy hay un solo backend y estos asserts parecen triviales. Existen porque son
los que van a correr, sin tocarlos, contra el backend que intercambia bytes por
HTTP: si `reserve` / `publish` / `resolve` no se comportan igual en los dos, la
API va a servir 409 por archivos que si estan (o peor, servir el equivocado).

El caso que mas facil se rompe es el nested: `normalize` lo escribe en
`normalized/` y el geocode lo REGENERA en `geocoded/`. Los dos quedan guardados
en el mismo campo del `Job`, asi que la referencia tiene que mandar sobre el
layout — si no, despues de geocodificar la UI se descarga el nested viejo, sin
una sola coordenada.
"""
import pytest

from smart_import.artifacts import (
    FLAT, GEOCODED, GEOCODED_NESTED, NESTED, RAW, REPORT, LocalArtifactStore,
    UnknownKind, make_artifact_store, relative_path,
)
from smart_import.config import Config


@pytest.fixture
def almacen(tmp_path):
    return LocalArtifactStore(tmp_path)


def test_la_factory_devuelve_el_backend_local(tmp_path):
    """El default es el comportamiento de siempre: disco de este proceso."""
    almacen = make_artifact_store(Config().replace(work_dir=str(tmp_path)))
    assert isinstance(almacen, LocalArtifactStore)
    assert almacen.work_dir == tmp_path


def test_el_layout_es_el_que_escribe_el_pipeline():
    """`run_normalize` deriva el nested y el report del stem de la salida flat.

    Si estos nombres se desalinean, `publish` guarda una ruta y el archivo real
    queda en otra: nadie falla, simplemente no se encuentra nunca.
    """
    assert str(relative_path(FLAT)) == "normalized/normalized.csv"
    assert str(relative_path(NESTED)) == "normalized/normalized.nested.json"
    assert str(relative_path(REPORT)) == "normalized/normalized.report.json"
    assert str(relative_path(GEOCODED)) == "geocoded/geocoded.csv"
    assert str(relative_path(GEOCODED_NESTED)) == "geocoded/geocoded.nested.json"
    assert str(relative_path(RAW, "entregas.xlsx")) == "raw/entregas.xlsx"


def test_un_artefacto_desconocido_falla_fuerte():
    with pytest.raises(UnknownKind):
        relative_path("planilla_magica")


def test_reserve_deja_el_directorio_listo_para_escribir(almacen):
    """El pipeline hace `open(output, "w")` a secas: el padre tiene que existir."""
    destino = almacen.reserve("imp_1", FLAT)
    assert destino.parent.is_dir()
    destino.write_text("delivery_id\nA\n", encoding="utf-8")
    assert almacen.exists("imp_1", FLAT, str(destino))


def test_lo_que_no_se_escribio_no_existe(almacen):
    almacen.reserve("imp_1", FLAT)          # reservar no crea el archivo
    assert almacen.resolve("imp_1", FLAT, None) is None
    assert almacen.exists("imp_1", FLAT, None) is False


def test_la_referencia_manda_sobre_el_layout(almacen, tmp_path):
    """El nested regenerado vive en `geocoded/`, no donde dice el layout."""
    suelto = tmp_path / "afuera" / "geocoded.nested.json"
    suelto.parent.mkdir()
    suelto.write_text("{}", encoding="utf-8")

    assert almacen.resolve("imp_1", NESTED, str(suelto)) == suelto


def test_publish_devuelve_una_referencia_que_resolve_entiende(almacen):
    """El ida y vuelta completo: se escribe, se publica, se lee."""
    destino = almacen.reserve("imp_1", GEOCODED)
    destino.write_text("delivery_id,lat,lng\nA,-34.6,-58.4\n", encoding="utf-8")

    ref = almacen.publish("imp_1", GEOCODED, destino)

    leido = almacen.resolve("imp_1", GEOCODED, ref)
    assert leido is not None
    assert leido.read_text(encoding="utf-8").startswith("delivery_id")


def test_los_jobs_no_se_pisan_entre_si(almacen):
    a = almacen.reserve("imp_a", FLAT)
    b = almacen.reserve("imp_b", FLAT)
    assert a != b


def test_delete_borra_todo_y_es_idempotente(almacen):
    almacen.reserve("imp_1", FLAT).write_text("x", encoding="utf-8")
    almacen.reserve("imp_1", RAW, filename="e.csv").write_text("x", encoding="utf-8")

    almacen.delete("imp_1")
    assert not almacen.dir_for("imp_1").exists()
    almacen.delete("imp_1")                 # sobre un job que ya no esta
