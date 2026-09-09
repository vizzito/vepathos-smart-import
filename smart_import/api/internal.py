"""El puerto por el que los workers buscan y devuelven archivos.

No es API publica: no aparece en `/docs`, no lo llama el front y no tiene
cabida en ninguna integracion. Es la contracara de `artifacts/http.py`, y es lo
que permite que un worker corra en una maquina que no comparte disco con la api
—ni siquiera una IP entrante— porque todas las conexiones las abre el worker.

Dos verbos sobre el mismo recurso, `(job_id, kind)`:

    GET  → los bytes del artefacto, si existen.
    PUT  → los bytes de un resultado; quedan donde la api sirve descargas.

Y nada mas. En particular, NO tocan el estado del job: quien hizo el trabajo lo
escribe en el store compartido con su propio `save()`. Que un mismo cambio se
escriba desde dos lados es como se corrompe el estado sin que nadie lo note.

<SECURITY_REVIEW>
Riesgo: el rol api expone lectura y ESCRITURA de archivos a la red interna. Un
tercero que alcance el puerto podria bajar datos de clientes o pisar el
resultado de un import ajeno.
Mitigaciones:
  - Token compartido obligatorio (`SMART_IMPORT_WORKER_TOKEN`), comparado con
    `compare_digest` (no `==`) para no filtrar el prefijo correcto por tiempo.
  - Sin token configurado, el router entero responde 404: un despliegue que se
    olvido de la variable queda cerrado, no abierto.
  - Falla siempre 404, nunca 401/403: quien no tiene el token no averigua por
    las respuestas ni que este router existe, ni que jobs hay.
  - El `kind` se valida contra `KINDS` y la ruta la calcula el ArtifactStore
    desde `job_id` + `kind`: nada de lo que manda el cliente se concatena a una
    ruta, asi que no hay `../` que escape del work_dir.
  - El `job_id` de la URL tiene que existir en el store; no se crean jobs por aca.
  - El token viaja en texto plano: el enlace api↔worker va por red privada o
    TLS. Documentado en SETUP.md.
Referencias: OWASP API1 (Broken Object Level Authorization), API7 (SSRF/path).
</SECURITY_REVIEW>
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse

from ..artifacts import (
    FLAT, GEOCODED, GEOCODED_NESTED, KINDS, NESTED, PARCIAL, RAW,
)
from ..jobs import Job
from ..logging_setup import stage

#: Cabecera del token. Propia y no `Authorization` para que ningun proxy,
#: logger o middleware de auth de negocio la confunda con una credencial de
#: usuario final: esto no es un usuario, es el otro proceso.
TOKEN_HEADER = "X-Smart-Import-Token"

#: De donde sale la referencia guardada del artefacto. Los que no estan (los
#: report) viven siempre en su lugar canonico y los resuelve el layout.
REF_FIELD = {
    RAW: "raw_path",
    FLAT: "normalized_path",
    NESTED: "nested_path",
    GEOCODED: "geocoded_path",
    GEOCODED_NESTED: "nested_path",
}


def internal_router(ctx_factory: Callable) -> APIRouter:
    """Router de intercambio de artefactos.

    `ctx_factory` devuelve el contexto vivo (config, store, artifacts) en cada
    request en vez de capturarlo al montar: es el mismo motivo por el que
    `_worker_ctx` se arma por llamada, y sin eso los tests que reemplazan el
    store se quedarian hablando con el de arranque.
    """
    router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)

    def autorizado(ctx=Depends(ctx_factory),
                   token: str | None = Header(None, alias=TOKEN_HEADER)):
        esperado = ctx.cfg.worker_token
        if not esperado or not token or not secrets.compare_digest(token, esperado):
            # 404 y no 401: para el que no tiene el token, este router no existe.
            raise HTTPException(404, "Not Found")
        return ctx

    @router.get("/jobs/{job_id}/artifacts/{kind}")
    def bajar(job_id: str, kind: str, ctx=Depends(autorizado)) -> FileResponse:
        job = _job_o_404(ctx, job_id)
        path = ctx.artifacts.resolve(job.id, _kind_o_404(kind), _ref(job, kind))
        if path is None:
            raise HTTPException(404, f"el job '{job_id}' no tiene '{kind}'")
        # El nombre viaja porque el worker lo necesita: el pipeline elige el
        # lector por la extension del raw, y `upload` sin extension no se lee.
        return FileResponse(path, media_type="application/octet-stream",
                            filename=Path(path).name)

    @router.put("/jobs/{job_id}/artifacts/{kind}", status_code=204)
    async def subir(job_id: str, kind: str, request: Request,
                    ctx=Depends(autorizado)) -> None:
        job = _job_o_404(ctx, job_id)
        kind = _kind_o_404(kind)
        if kind == RAW:
            # El original es del usuario y es la unica entrada que no se puede
            # regenerar: `PUT /mapping` re-normaliza desde el. Un worker que lo
            # pise cambia en silencio el resultado de todo lo que venga despues.
            raise HTTPException(409, "el archivo original no se sobrescribe")
        destino = ctx.artifacts.reserve(job.id, kind)

        # Se escribe al lado y se renombra al final: `resolve` de otro request
        # no puede encontrar un archivo a medio subir, que es peor que no
        # encontrar nada porque parece un resultado valido.
        parcial = destino.with_suffix(destino.suffix + PARCIAL)
        # Se corta al vuelo y no al final: el punto del techo es no escribir
        # los bytes, no descubrir despues que se escribieron. El disco de la
        # api es el mismo donde corre routehub.
        techo = int(float(ctx.cfg.max_artifact_mb) * 1024 * 1024)
        tamano = 0
        try:
            with open(parcial, "wb") as fh:
                async for chunk in request.stream():
                    tamano += len(chunk)
                    if techo and tamano > techo:
                        raise HTTPException(
                            413, f"el {kind} supera {ctx.cfg.max_artifact_mb:g} MB "
                                 f"(SMART_IMPORT_MAX_ARTIFACT_MB)")
                    fh.write(chunk)
            os.replace(parcial, destino)
        except Exception:
            parcial.unlink(missing_ok=True)
            raise
        stage(ctx.logger, "INTERNAL", "artefacto recibido", job=job.id, tipo=kind,
              tamano=f"{tamano / 1024:.1f}KB")

    @router.get("/health")
    def salud(ctx=Depends(autorizado)) -> dict[str, str]:
        """Le confirma al worker que el token sirve y que la api esta viva.

        Existe para que un worker mal configurado lo diga al arrancar y no
        cuando falle la primera tarea, media hora despues.
        """
        return {"status": "ok", "role": ctx.cfg.role}

    return router


def _kind_o_404(kind: str) -> str:
    if kind not in KINDS:
        raise HTTPException(404, f"artefacto '{kind}' desconocido")
    return kind


def _job_o_404(ctx, job_id: str) -> Job:
    job = ctx.store.get(job_id)
    if job is None:
        raise HTTPException(404, f"job '{job_id}' inexistente")
    return job


def _ref(job: Job, kind: str) -> str | None:
    campo = REF_FIELD.get(kind)
    return getattr(job, campo, None) if campo else None


__all__ = ["internal_router", "TOKEN_HEADER"]
