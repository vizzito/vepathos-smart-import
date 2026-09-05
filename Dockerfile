# ---------------------------------------------------------------------------
# Vepathos Smart Import
#
# Targets:
#   runtime  -> reglas + fuzzy + heuristicas + geocoder OSM. Imagen chica.
#   ai       -> agrega transformers/torch. Solo si el benchmark lo justifica;
#               el sistema funciona completo sin esto.
#
# ORDEN DE CAPAS: las dependencias se instalan ANTES de copiar el codigo. Si se
# copia el codigo primero, cada cambio de una linea invalida la capa de torch y
# la reinstala entera (~10 min). Con este orden, un cambio de codigo reconstruye
# en segundos porque torch queda cacheado.
#
# Los modelos NO se copian dentro de la imagen: van en un volumen de cache.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# libexpat/zlib los necesita pyosmium para leer PBF
RUN apt-get update && apt-get install -y --no-install-recommends \
        libexpat1 zlib1g libbz2-1.0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app


# --- dependencias: depende SOLO de pyproject.toml ---------------------------
FROM base AS deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake libexpat1-dev zlib1g-dev libbz2-dev \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
# Stub del paquete: pip necesita que exista para resolver el proyecto, pero el
# codigo real se copia despues. Asi esta capa solo cambia si cambia pyproject.
RUN mkdir -p smart_import && touch smart_import/__init__.py \
    && pip install --prefix=/install ".[geo,api]"


# --- dependencias + torch: tambien antes del codigo -------------------------
FROM deps AS deps-ai
RUN pip install --prefix=/install "transformers>=4.40" "torch>=2.2" \
        --index-url https://download.pytorch.org/whl/cpu \
    || pip install --prefix=/install "transformers>=4.40" "torch>=2.2"


# --- base comun de runtime: usuario y directorios ---------------------------
FROM base AS app-base
# TODOS los puntos de montaje se crean aca con el owner correcto. Un volumen
# nombrado sobre una ruta que NO existe en la imagen lo crea Docker como root,
# y el proceso (uid 10001) no puede escribir.
RUN mkdir -p /data/input /data/output /data/pbf /data/indexes /data/cache \
             /data/tmp /data/jobs /model-cache \
    && useradd -r -u 10001 -d /app smartimport \
    && chown -R smartimport /app /data /model-cache

ENV SMART_IMPORT_PBF_DIR=/data/pbf \
    SMART_IMPORT_INDEX_DIR=/data/indexes \
    SMART_IMPORT_CACHE_PATH=/data/cache/geocode_cache.sqlite \
    SMART_IMPORT_WORK_DIR=/data/jobs \
    SMART_IMPORT_DEVICE=cpu \
    HF_HOME=/model-cache

EXPOSE 8100
ENTRYPOINT ["python", "-m", "smart_import"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8100"]


# --- runtime (sin torch) ----------------------------------------------------
FROM app-base AS runtime
COPY --from=deps /install /usr/local
# el codigo va ULTIMO: es lo que cambia en cada commit
COPY --chown=smartimport smart_import ./smart_import
COPY --chown=smartimport schemas ./schemas
COPY --chown=smartimport pyproject.toml README.md ./
USER smartimport
ENV SMART_IMPORT_AI_ENABLED=false


# --- ai (con torch) ---------------------------------------------------------
FROM app-base AS ai
COPY --from=deps-ai /install /usr/local
COPY --chown=smartimport smart_import ./smart_import
COPY --chown=smartimport schemas ./schemas
COPY --chown=smartimport pyproject.toml README.md ./
USER smartimport
ENV SMART_IMPORT_AI_ENABLED=true \
    SMART_IMPORT_AI_PRELOAD=true
