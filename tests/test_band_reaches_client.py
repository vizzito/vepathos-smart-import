"""La banda tiene que llegar al cliente por el camino que el cliente usa.

El bug: `download?format=flat` y `preview` servian el CSV PREVIO al geocode. El
back calculaba `geocode_band` y confianza perfectamente, pero el cliente pedia el
artefacto viejo y recibia filas sin banda ni `%` — y pintaba todo igual.
"""
import csv
import io

import pytest
from fastapi.testclient import TestClient

from smart_import.api.app import app
from smart_import.geocoding.runner import DIAGNOSTIC_COLUMNS
from tests.conftest import FIXTURES


@pytest.fixture
def client():
    return TestClient(app)


def _subir(client, nombre="es_sin_coords.csv"):
    with open(FIXTURES / nombre, "rb") as fh:
        return client.post("/imports", files={"file": (nombre, fh.read())}).json()["job_id"]


def _geocodificado(job_id: str) -> bool:
    from smart_import.api.app import store
    job = store.get(job_id)
    return bool(job and job.geocoded_path)


def test_antes_de_geocodificar_no_hay_banda_que_mostrar(client):
    job = _subir(client)
    body = client.get(f"/imports/{job}/preview", params={"limit": 2}).json()
    assert body["source"] == "normalized"
    assert not [c for c in body["columns"] if c.startswith("geocode")]


def test_preview_sin_source_devuelve_el_resultado_vigente(client, monkeypatch, tmp_path):
    """`auto` es el default: si ya se geocodifico, se muestra eso."""
    from smart_import.api.app import store

    job_id = _subir(client)
    job = store.get(job_id)

    # simula la salida del geocode con las columnas que escribe el runner
    salida = tmp_path / "geocoded.csv"
    columnas = ["delivery_id", "address", "lat", "lng", *DIAGNOSTIC_COLUMNS]
    with open(salida, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(columnas)
        w.writerow(["001", "Av. Corrientes 100", "-34.6", "-58.37",
                    "matched", "1.000", "valid", "housenumber", "osm", "1.0", ""])
        w.writerow(["002", "Av. Cabildo 174", "-34.56", "-58.45",
                    "low_confidence", "0.822", "review", "street", "osm", "0.71", ""])
    job.geocoded_path = str(salida)
    job.geocoded_revision = job.normalized_revision

    body = client.get(f"/imports/{job_id}/preview", params={"limit": 5}).json()
    assert body["source"] == "geocoded"
    assert "geocode_band" in body["columns"]
    assert [r["geocode_band"] for r in body["rows"]] == ["valid", "review"]
    assert body["rows"][1]["geocode_confidence"] == "0.822"


def test_download_flat_sirve_el_csv_vigente(client, tmp_path):
    """`flat` = el CSV Vepathos actual. Tras geocodificar, ese es el geocodificado."""
    from smart_import.api.app import store

    job_id = _subir(client)
    job = store.get(job_id)
    salida = tmp_path / "geocoded.csv"
    salida.write_text(
        "delivery_id,address,lat,lng," + ",".join(DIAGNOSTIC_COLUMNS) + "\n"
        "001,Av. Corrientes 100,-34.6,-58.37,matched,1.000,valid,housenumber,osm,1.0,\n",
        encoding="utf-8")
    job.geocoded_path = str(salida)
    job.geocoded_revision = job.normalized_revision

    flat = client.get(f"/imports/{job_id}/download", params={"format": "flat"}).text
    assert "geocode_band" in flat.splitlines()[0]

    # y sigue existiendo la forma de pedir el previo al geocode
    previo = client.get(f"/imports/{job_id}/download", params={"format": "normalized"}).text
    assert "geocode_band" not in previo.splitlines()[0]


def test_el_csv_geocodificado_es_superset_del_normalizado(client, tmp_path):
    """Servir el geocodificado como `flat` no puede perder columnas del schema."""
    from smart_import.api.app import store

    job_id = _subir(client)
    job = store.get(job_id)
    normalizado = set(next(csv.reader(open(job.normalized_path, encoding="utf-8"))))

    salida = tmp_path / "geocoded.csv"
    columnas = [*sorted(normalizado), "lat", "lng", *DIAGNOSTIC_COLUMNS]
    salida.write_text(",".join(columnas) + "\n", encoding="utf-8")
    job.geocoded_path = str(salida)
    job.geocoded_revision = job.normalized_revision

    flat = client.get(f"/imports/{job_id}/download", params={"format": "flat"}).text
    servidas = set(next(csv.reader(io.StringIO(flat))))
    assert normalizado <= servidas


def test_todas_las_columnas_de_banda_estan_en_el_contrato():
    """Si alguien agrega una columna al runner, tiene que estar en el contrato."""
    assert "geocode_band" in DIAGNOSTIC_COLUMNS
    assert "geocode_confidence" in DIAGNOSTIC_COLUMNS
    assert "geocode_status" in DIAGNOSTIC_COLUMNS
