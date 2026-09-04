# ---------------------------------------------------------------------------
# Vepathos Smart Import
#
# Dos targets:
#   runtime  -> reglas + fuzzy + heuristicas + geocoder OSM. Imagen chica.
#   ai       -> agrega transformers/torch (~2-3 GB). Solo si el benchmark lo
#               justifica; el sistema funciona completo sin esto.
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


FROM base AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake libexpat1-dev zlib1g-dev libbz2-dev \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY smart_import ./smart_import
RUN pip install --prefix=/install ".[geo]"


FROM base AS runtime
COPY --from=builder /install /usr/local
COPY smart_import ./smart_import
COPY schemas ./schemas
COPY pyproject.toml README.md ./

# /data/pbf se monta READ ONLY desde los _extracts del cutter
RUN mkdir -p /data/input /data/output /data/pbf /data/indexes /data/cache /data/tmp \
    && useradd -r -u 10001 -d /app smartimport \
    && chown -R smartimport /app /data
USER smartimport

ENV SMART_IMPORT_PBF_DIR=/data/pbf \
    SMART_IMPORT_INDEX_DIR=/data/indexes \
    SMART_IMPORT_CACHE_PATH=/data/cache/geocode_cache.sqlite \
    SMART_IMPORT_AI_ENABLED=false \
    SMART_IMPORT_DEVICE=cpu

ENTRYPOINT ["python", "-m", "smart_import"]
CMD ["--help"]


FROM runtime AS ai
USER root
RUN pip install --no-cache-dir "transformers>=4.40" "torch>=2.2" --index-url https://download.pytorch.org/whl/cpu \
    || pip install --no-cache-dir "transformers>=4.40" "torch>=2.2"
USER smartimport
ENV SMART_IMPORT_AI_ENABLED=true \
    HF_HOME=/model-cache
