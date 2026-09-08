"""El contrato del almacen de artefactos: que archivos tiene un job y donde.

Hoy los archivos de un job viven en el disco del proceso que sirve HTTP, y el
codigo lo daba por sentado: `open(job.normalized_path)` desde el endpoint de
descarga solo funciona si el normalize corrio en esta maquina. Esa suposicion es
lo que ata el servicio a un solo nodo.

El contrato tiene tres verbos, y el orden importa:

    reserve  → ruta LOCAL donde escribir. El pipeline sigue siendo ruta-entra /
               ruta-sale: no se entera de nada.
    publish  → el artefacto ya esta escrito; dejalo disponible para quien sirve
               HTTP. Devuelve la referencia que se guarda en el `Job`.
    resolve  → una copia local legible de un artefacto, o None si no esta.

Con el backend local los tres son casi no-ops (la ruta reservada YA es la
definitiva, `publish` no copia nada), que es exactamente por que esta fase no
cambia ningun comportamiento. El backend HTTP de la fase siguiente implementa
los mismos tres verbos subiendo y bajando bytes, y ni `app.py` ni los handlers
se enteran.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

#: El archivo tal cual lo subio el usuario. Es el unico que hay que conservar
#: mientras el job viva: `PUT /mapping` re-normaliza desde el.
RAW = "raw"
#: Salidas del normalize (las tres las escribe `run_normalize` de una pasada).
FLAT = "flat"
NESTED = "nested"
REPORT = "report"
#: Salidas del geocode. `geocoded_nested` es el nested REGENERADO con las
#: coordenadas: el de normalize se armo antes de tenerlas.
GEOCODED = "geocoded"
GEOCODED_NESTED = "geocoded_nested"
GEOCODED_REPORT = "geocoded_report"

KINDS = (RAW, FLAT, NESTED, REPORT, GEOCODED, GEOCODED_NESTED, GEOCODED_REPORT)

#: Layout dentro de la carpeta del job: (subdirectorio, nombre). El `raw` no
#: esta aca porque su nombre lo elige el usuario (ya saneado por `safe_filename`).
#:
#: Los nombres NO son arbitrarios: `run_normalize` deriva el nested y el report
#: del stem de la salida flat (`normalized.csv` → `normalized.nested.json`), asi
#: que este mapa tiene que coincidir con lo que el pipeline escribe de verdad.
LAYOUT: dict[str, tuple[str, str]] = {
    FLAT: ("normalized", "normalized.csv"),
    NESTED: ("normalized", "normalized.nested.json"),
    REPORT: ("normalized", "normalized.report.json"),
    GEOCODED: ("geocoded", "geocoded.csv"),
    GEOCODED_NESTED: ("geocoded", "geocoded.nested.json"),
    GEOCODED_REPORT: ("geocoded", "geocoded.report.json"),
}


class UnknownKind(KeyError):
    """Se pidio un artefacto que no existe en el layout."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"artefacto '{kind}' desconocido; validos: {sorted(KINDS)}")
        self.kind = kind


def relative_path(kind: str, filename: str | None = None) -> Path:
    """Ruta del artefacto RELATIVA a la carpeta del job.

    Es la misma cuenta para el disco local y para la URL interna de la fase 2,
    asi que vive aca y no en un backend.
    """
    if kind == RAW:
        return Path("raw") / (filename or "upload")
    try:
        subdir, name = LAYOUT[kind]
    except KeyError as exc:
        raise UnknownKind(kind) from exc
    return Path(subdir) / name


class ArtifactStore(ABC):
    """Los archivos de un job, sin asumir que estan en el disco de este proceso."""

    @abstractmethod
    def reserve(self, job_id: str, kind: str, *, filename: str | None = None) -> Path:
        """Ruta local donde escribir el artefacto. El directorio queda creado."""

    @abstractmethod
    def publish(self, job_id: str, kind: str, path: str | Path) -> str:
        """Deja el artefacto disponible. Devuelve la referencia para el `Job`."""

    @abstractmethod
    def resolve(self, job_id: str, kind: str, ref: str | Path | None) -> Path | None:
        """Copia local legible del artefacto, o `None` si no esta.

        `ref` es lo que quedo guardado en el `Job`; manda sobre el layout, porque
        hay salidas que se mudan de lugar (el nested se regenera dentro de
        `geocoded/` despues del geocode).
        """

    @abstractmethod
    def delete(self, job_id: str) -> None:
        """Borra todo lo del job. Idempotente."""

    def exists(self, job_id: str, kind: str, ref: str | Path | None) -> bool:
        return self.resolve(job_id, kind, ref) is not None
