"""ASGI guards run before FastAPI parses or spools the request body."""
import asyncio
import secrets
from contextlib import asynccontextmanager
from contextvars import ContextVar

from fastapi import HTTPException
from starlette.responses import JSONResponse

from ..identity import tenant

admitted: ContextVar[bool] = ContextVar("upload_admitted", default=False)


@asynccontextmanager
async def existing_admission(acquire):
    if admitted.get():
        yield
    else:
        async with acquire():
            yield


class RequestGuards:
    def __init__(self, app, config, admission):
        self.app, self.config, self.admission = app, config, admission

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        cfg = self.config()
        path = scope.get("path", "")
        headers = dict(scope.get("headers", []))
        principal = None
        if (cfg.api_keys and scope["method"] != "OPTIONS"
                and not path.startswith("/internal/") and path != "/health"):
            supplied = headers.get(b"authorization", b"")
            for token, owner in cfg.api_keys.items():
                if secrets.compare_digest(supplied, ("Bearer " + token).encode("utf-8")):
                    principal = owner
            if principal is None:
                return await JSONResponse({"detail": "Unauthorized"}, 401)(scope, receive, send)
        upload = scope["method"] == "POST" and path.rstrip("/") == "/imports"
        # Internal artifact streaming has its own separately configured limit.
        limit = int(cfg.max_file_mb * 1024 * 1024) + 1024 * 1024 if upload else 1024 * 1024
        if path.startswith("/internal/"):
            limit = int(cfg.max_artifact_mb * 1024 * 1024)
        size = 0
        started = False

        async def bounded_receive():
            nonlocal size
            try:
                async with asyncio.timeout(cfg.upload_idle_timeout_s):
                    message = await receive()
            except TimeoutError:
                raise HTTPException(408, "Tiempo de carga agotado") from None
            size += len(message.get("body", b""))
            if limit and size > limit:
                raise HTTPException(413, "Request demasiado grande")
            return message

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        @asynccontextmanager
        async def slot():
            if upload:
                async with self.admission():
                    reset = admitted.set(True)
                    try:
                        yield
                    finally:
                        admitted.reset(reset)
            else:
                yield

        reset = tenant.set(principal)
        try:
            content_length = headers.get(b"content-length")
            if content_length:
                try:
                    length = int(content_length)
                except ValueError:
                    raise HTTPException(400, "Content-Length inválido") from None
                if length < 0 or (limit and length > limit):
                    raise HTTPException(413, "Request demasiado grande")
            async with slot():
                await self.app(scope, bounded_receive, safe_send)
        except HTTPException as exc:
            if started:
                raise
            await JSONResponse({"detail": exc.detail}, exc.status_code,
                               headers=exc.headers)(scope, receive, send)
        finally:
            tenant.reset(reset)
