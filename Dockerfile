# ---------------------------------------------------------------------------
# Vepathos Smart Import
#
# Targets:
#   runtime  -> reglas + fuzzy + heuristicas + geocoder OSM. Imagen chica.
#   ai       -> agrega transformers/torch. Solo si el benchmark lo justifica;
#               el sistema funciona completo sin esto.
#
# CACHE DE CAPAS (importante):
#   - torch vive en su PROPIA etapa (`torch`), independiente de pyproject.toml.
#   - Cambiar codigo o pyproject NO reinstala torch (~400s).
#   - Solo se reinstala si cambia el RUN de versiones de torch/transformers.
#   - BuildKit cache mount de pip acelera reinstales si alguna vez hace falta.
#
# Local dia a dia: `docker compose up -d` SIN --build. Solo --build cuando
# cambian Dockerfile / deps. Para codigo Python, monta ./smart_import (abajo
# en compose) o rebuild: el COPY del codigo es la ultima capa y tarda segundos.
#
# Los modelos NO van en la imagen: volumen HF_HOME / model-cache.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libexpat/zlib los necesita pyosmium para leer PBF
RUN apt-get update && apt-get install -y --no-install-recommends \
        libexpat1 zlib1g libbz2-1.0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app


# --- torch/transformers: capa AISLADA (casi nunca se invalida) --------------
# NO depende de pyproject.toml ni del codigo. Esta etapa solo se rebuild-ea
# si cambia este RUN (o la imagen base). Cambiar heuristics/pyproject NO
# vuelve a bajar los ~2GB de torch.
FROM base AS torch
# SIN fallback a PyPI. El `|| pip install ...` que habia aca enmascaraba un fallo
# del indice y traia la build de CUDA (torch 2.14.0+cu130) incluso en una imagen
# sin GPU: ~6 GB de mas y la inferencia en CPU MUCHO mas lenta. El indice /whl/cpu
# tiene wheels para x86_64 y aarch64, asi que si esto falla es un problema real
# que hay que ver, no algo que tapar con un fallback.
# torch se instala SOLO desde el indice CPU. Nada de --extra-index-url: con PyPI
# habilitado pip elige 2.14.0+cu130 por encima de 2.14.0+cpu (el sufijo local
# ordena mas alto) y termina metiendo la build de CUDA en una imagen sin GPU:
# ~6 GB de mas y la inferencia en CPU ~25x mas lenta.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.2"
# transformers y sus dependencias si vienen de PyPI, pero torch ya esta resuelto
RUN --mount=type=cache,target=/root/.cache/pip \
    PYTHONPATH=/install/lib/python3.12/site-packages \
    pip install --prefix=/install "transformers>=4.40"
# La build equivocada es silenciosa salvo por el tiempo de inferencia: se falla
# el build aca antes que descubrirlo en produccion.
RUN PYTHONPATH=/install/lib/python3.12/site-packages python -c "\
import torch, sys; v = torch.__version__; print('torch instalado:', v); \
sys.exit(0 if v.endswith('+cpu') else 1)"


# --- deps del proyecto: solo se invalida si cambia pyproject/README ---------
FROM base AS deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake libexpat1-dev zlib1g-dev libbz2-dev \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
# Stub del paquete: pip necesita que exista; el codigo real se copia despues.
RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir -p smart_import && touch smart_import/__init__.py \
    && pip install --prefix=/install ".[geo,api]"


# --- merge: deps del proyecto + torch (torch se COPIA cacheado) -------------
FROM deps AS deps-ai
COPY --from=torch /install /install


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
