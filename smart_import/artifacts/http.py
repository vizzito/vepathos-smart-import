"""Artefactos que viven en el disco de OTRO proceso, alcanzados por HTTP.

Es el backend del rol worker. Los mismos tres verbos que el local, con la
diferencia que importa: el trabajo se hace en un scratch de esta maquina y los
bytes se devuelven al rol api, que es quien sirve las descargas al usuario.

    reserve → una ruta en el scratch. El pipeline no se entera de nada.
    publish → PUT de esos bytes a la api. Devuelve una URI, no una ruta: la
              ruta local del worker no significa nada del otro lado.
    resolve → GET a la api, guardado en el scratch. Lo ya bajado no se re-baja.

El scratch se borra SIEMPRE (`discard`, o el context manager `trabajando_en`).
Un worker no acumula nada: si el disco crece, es un bug.

Elegir HTTP contra el rol api en vez de un volumen compartido o un object store
es lo que permite prender un worker en cualquier maquina —incluida una laptop
detras de NAT— sin abrir puertos ni montar nada: todas las conexiones son
salientes.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import random
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from ..execution import token_for, ExecutionLost
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .base import (
    PARCIAL, RAW, ArtifactStore, artifact_uri, local_path, relative_path,
)

#: Cuantas veces se reintenta un GET/PUT antes de dar la tarea por fallida. La
#: api puede estar reiniciandose por un deploy: que un worker pierda una tarea
#: por eso seria devolverla a la cola por nada.
REINTENTOS = 3
ESPERA_BASE_S = 0.5

#: Segundos para transferir un artefacto. Generoso: son archivos, no requests
#: interactivos, y del otro lado puede haber un ADSL.
TIMEOUT_S = 120.0


class ArtifactTransferError(RuntimeError):
    """No se pudo mover un artefacto."""


class ArtifactRejected(ArtifactTransferError):
    """La api dijo que no (4xx). Reintentar da exactamente lo mismo.

    Un token vencido o un job que ya no existe no se arreglan esperando: es
    mejor que el worker falle en el primer intento y la tarea vuelva a la cola.
    """


def default_scratch() -> Path:
    """Donde trabaja un worker que no eligio carpeta.

    El temporal del sistema es lo correcto para algo que se borra siempre y que
    nadie mas lee: no hay que provisionar nada para prender un worker.
    """
    return Path(tempfile.gettempdir()) / "smart-import-scratch"


class HttpArtifactStore(ArtifactStore):
    def __init__(self, base_url: str, token: str, scratch_dir: str | Path,
                 *, client=None, reintentos: int = REINTENTOS, max_bytes: int = 100 * 1024 * 1024):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._scratch = Path(scratch_dir)
        self._reintentos = max(1, reintentos)
        self._max_bytes = max_bytes
        self._cliente = client
        self._propio = client is None
        self._client_lock = threading.Lock()

    # ---------------------------------------------------------- scratch

    def dir_for(self, job_id: str) -> Path:
        root = self._scratch / job_id
        from ..execution import current
        execution = current.get()
        token = execution.token if execution and execution.job_id == job_id else None
        return root / "attempts" / token if token else root

    def reserve(self, job_id: str, kind: str, *, filename: str | None = None) -> Path:
        path = self.dir_for(job_id) / relative_path(kind, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def discard(self, job_id: str) -> None:
        """Borra el scratch del job. Idempotente."""
        shutil.rmtree(self.dir_for(job_id), ignore_errors=True)

    @contextmanager
    def trabajando_en(self, job_id: str) -> Iterator[Path]:
        """Garantiza que el scratch se borre pase lo que pase.

        Exito, excepcion, timeout o cancelacion: el `finally` corre igual. Es la
        unica forma de que un worker de larga vida no se llene de archivos de
        jobs que ya nadie mira.
        """
        try:
            yield self.dir_for(job_id)
        finally:
            self.discard(job_id)

    # ---------------------------------------------------------- transporte

    def _http(self):
        with self._client_lock:
            if self._cliente is None:
                try:
                    import httpx
                except ImportError as exc:                          # pragma: no cover
                    raise RuntimeError(
                        "el rol worker intercambia archivos con la api por HTTP y "
                        "necesita httpx. Instalalo con: pip install -e '.[queue]'") from exc
                self._cliente = httpx.Client(base_url=self._base_url, timeout=TIMEOUT_S)
            return self._cliente

    def _url(self, job_id: str, kind: str) -> str:
        return f"{self._base_url}/internal/jobs/{job_id}/artifacts/{kind}"

    @property
    def _headers(self) -> dict[str, str]:
        from ..execution import current
        headers = {"X-Smart-Import-Token": self._token}
        execution = current.get()
        if execution is not None:
            headers["X-Smart-Import-Execution"] = token_for(execution.job_id)
        return headers

    def _con_reintentos(self, que: str, hacer):
        """Reintenta con backoff. El ultimo error se propaga con contexto."""
        ultimo: Exception | None = None
        for intento in range(self._reintentos):
            try:
                return hacer()
            except (ArtifactRejected, ExecutionLost):
                raise                       # insistir da lo mismo
            except Exception as exc:        # timeouts, conexion, DNS, 5xx
                ultimo = exc
                if intento + 1 < self._reintentos:
                    wait = getattr(exc, "retry_after_s", None)
                    time.sleep(min(30., wait if wait is not None else
                                   ESPERA_BASE_S * (2 ** intento) * random.uniform(.8, 1.2)))
        raise ArtifactTransferError(f"{que}: {ultimo}") from ultimo

    # ---------------------------------------------------------- verbos

    def publish(self, job_id: str, kind: str, path: str | Path) -> str:
        origen = Path(path)

        def subir():
            with open(origen, "rb") as fh:
                r = self._http().put(self._url(job_id, kind), content=fh,
                                     headers=self._headers)
            _fallar_si(r, f"la api rechazo el {kind} de {job_id}")
            return r

        self._con_reintentos(f"no se pudo publicar {kind} de {job_id}", subir)
        return artifact_uri(job_id, kind)

    def resolve(self, job_id: str, kind: str, ref: str | Path | None) -> Path | None:
        # Ya bajado: dentro de una tarea el mismo artefacto se pide mas de una
        # vez y no tiene sentido traerlo dos veces.
        ya = self._en_scratch(job_id, kind, ref)
        if ya is not None:
            return ya

        def bajar():
            from ..atomic import output_path
            deadline = time.monotonic() + TIMEOUT_S
            with self._http().stream("GET", self._url(job_id, kind), headers=self._headers) as r:
                if r.status_code == 404:
                    return None
                _fallar_si(r, f"la api nego el {kind} de {job_id}")
                destino = self.reserve(job_id, kind,
                                       filename=_nombre_servido(r) or _nombre_de(ref, kind))
                size = 0
                with output_path(destino) as partial, open(partial, "wb") as fh:
                    for chunk in r.iter_bytes(chunk_size=65536):
                        token_for(job_id)
                        size += len(chunk)
                        if self._max_bytes and size > self._max_bytes:
                            raise ArtifactRejected("Artifact exceeds transfer limit")
                        if time.monotonic() > deadline:
                            raise ArtifactTransferError("Artifact transfer deadline exceeded")
                        fh.write(chunk)
                return destino

        return self._con_reintentos(f"no se pudo traer {kind} de {job_id}", bajar)

    def _en_scratch(self, job_id: str, kind: str, ref) -> Path | None:
        """Lo que ya se bajo, si esta.

        El `raw` se busca por carpeta y no por nombre: quien lo pide no sabe
        como se llama —el nombre lo eligio el usuario y lo dice la api al
        servirlo— y con el nombre equivocado se bajaria de nuevo cada vez.
        """
        if kind == RAW:
            archivos = sorted(p for p in (self.dir_for(job_id) / "raw").glob("*")
                              if p.suffix != PARCIAL)
            return archivos[0] if archivos else None
        destino = self.dir_for(job_id) / relative_path(kind)
        return destino if destino.exists() else None

    def delete(self, job_id: str) -> None:
        """Solo lo de esta maquina.

        Los archivos que sirve la api los borra la api, con su TTL y su
        `DELETE /imports/{id}`: un worker no decide que se deja de ofrecer.
        """
        self.discard(job_id)

    def close(self) -> None:
        """Cierra el cliente si es propio. Un cliente prestado lo cierra quien lo presto."""
        if self._propio and self._cliente is not None:
            self._cliente.close()
            self._cliente = None


def _fallar_si(respuesta, que: str) -> None:
    """Un 4xx es una negativa; un 5xx es la api teniendo un mal momento."""
    codigo = respuesta.status_code
    if codigo < 400:
        return
    transient = codigo in (408, 429) or codigo >= 500
    exc = (ArtifactTransferError if transient else ArtifactRejected)(f"{que}: HTTP {codigo}")
    raw = getattr(respuesta, "headers", {}).get("retry-after", "")
    if transient and raw:
        try:
            delay = float(raw)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                delay = 0.
        if __import__("math").isfinite(delay):
            exc.retry_after_s = max(0., min(30., delay))
    raise exc


def _nombre_de(ref: str | Path | None, kind: str) -> str | None:
    """El nombre del `raw` sale de la referencia; el resto es canonico."""
    if kind != RAW:
        return None
    origen = local_path(ref)
    return origen.name if origen else None


def _nombre_servido(respuesta) -> str | None:
    """El nombre que dijo la api en el Content-Disposition.

    Importa para el `raw`: el pipeline elige el lector por la extension, asi que
    bajar `entregas.xlsx` como `upload` lo rompe.

    Se le pasa por `.name` porque es un dato que llega por la red: aunque del
    otro lado haya un proceso nuestro, un nombre con `../` no puede terminar
    escribiendo fuera del scratch.
    """
    cabecera = respuesta.headers.get("content-disposition", "")
    for parte in cabecera.split(";"):
        clave, sep, valor = parte.strip().partition("=")
        if sep and clave.lower() == "filename":
            return Path(valor.strip().strip('"')).name or None
    return None
