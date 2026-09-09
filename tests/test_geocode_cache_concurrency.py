"""La cache de geocode no puede serializar dos jobs.

Con el modo por defecto de sqlite y un unico commit al final del archivo, la
primera fila tomaba el lock RESERVED de la base y no lo soltaba hasta terminar:
un segundo job concurrente esperaba el timeout y moria con "database is
locked", que subia hasta dejar el job en GEOCODE_FAILED. La plantilla local
(local-dev.env.template) ya traia SMART_IMPORT_GEOCODE_WORKERS=2, o sea la
config de referencia estaba en
la condicion de falla.
"""
import sqlite3

from smart_import.geocoding.base import STATUS_MATCHED, GeocodeResult
from smart_import.geocoding.cache import GeocodeCache


def _result(lat: float = -34.6, lon: float = -58.4) -> GeocodeResult:
    return GeocodeResult(status=STATUS_MATCHED, lat=lat, lon=lon,
                         confidence=0.91, precision="housenumber", source="osm")


def test_la_cache_usa_wal(tmp_path):
    """Sin WAL la base entera se bloquea mientras dura una escritura."""
    cache = GeocodeCache(tmp_path / "geo.sqlite")
    try:
        modo = cache._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert modo.lower() == "wal", f"journal_mode={modo}: dos jobs se bloquean"
    finally:
        cache.close()


def test_cada_escritura_se_publica_sin_esperar_al_final(tmp_path):
    """Un commit unico al final retiene el lock durante todo el archivo."""
    path = tmp_path / "geo.sqlite"
    cache = GeocodeCache(path)
    try:
        for i in range(3):
            cache.put(f"Av. Corrientes {100 + i}", _result(), "ctx")

        # Otra conexion ya las ve, sin que el job haya terminado.
        otra = sqlite3.connect(path)
        try:
            n = otra.execute("SELECT COUNT(*) FROM geocode_cache").fetchone()[0]
        finally:
            otra.close()
        assert n == 3, "no se publicaron: la transaccion sigue abierta"
    finally:
        cache.close()


def test_dos_jobs_escriben_la_misma_cache_sin_romperse(tmp_path):
    """Es el escenario de SMART_IMPORT_GEOCODE_WORKERS=2 sobre un mismo cache."""
    path = tmp_path / "geo.sqlite"
    job_a = GeocodeCache(path)
    job_b = GeocodeCache(path)
    try:
        for i in range(6):
            job_a.put(f"Calle A {i}", _result(), "ctx")
            job_b.put(f"Calle B {i}", _result(-34.7, -58.5), "ctx")
        job_a.commit()
        job_b.commit()

        assert job_a.get("Calle A 5", "ctx") is not None
        assert job_b.get("Calle B 5", "ctx") is not None
        n = job_a._conn.execute("SELECT COUNT(*) FROM geocode_cache").fetchone()[0]
        assert n == 12, f"se perdieron escrituras: {n} de 12"
    finally:
        job_a.close()
        job_b.close()
