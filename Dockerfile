# ---------------------------------------------------------------------------
# Vepathos Smart Import
#
# Targets:
#   runtime           -> reglas + fuzzy + heuristicas + geocoder OSM. Imagen chica.
#   runtime-libpostal -> runtime + libpostal (~+2 GB datos de parseo).
#
# libpostal NO consulta internet al parsear: trae modelos entrenados (OSM /
# OpenAddresses) bajados en el BUILD a /opt/libpostal-data. La flag
# SMART_IMPORT_LIBPOSTAL_ENABLED sigue mandando en runtime.
#
# CACHE DE CAPAS (importante):
#   - libpostal vive en `libpostal-build` (casi nunca se invalida).
#   - Cambiar codigo o pyproject NO recompila libpostal.
#   - BuildKit cache mount de pip acelera reinstales si alguna vez hace falta.
#
# Local dia a dia: `docker compose up -d` SIN --build. Solo --build cuando
# cambian Dockerfile / deps. Para codigo Python, monta ./smart_import (abajo
# en compose) o rebuild: el COPY del codigo es la ultima capa y tarda segundos.
#
# Vocabulario: `python -m smart_import.vocab setup --geonames` corre en el
# BUILD (sqlite + ciudades) y el entrypoint refresca el sqlite al arrancar.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libexpat/zlib: pyosmium. osmium-tool: cortar extracts de ciudad. snappy: libpostal
RUN apt-get update && apt-get install -y --no-install-recommends \
        libexpat1 zlib1g libbz2-1.0 libsnappy1v5 ca-certificates osmium-tool \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app


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


# --- libpostal C + datos (~2 GB). Capa pesada; casi nunca se invalida --------
# Al hacer `make install`, libpostal BAJA los model files a --datadir.
# Eso NO es un geocoder online: son tablas CRF entrenadas (OSM/OpenAddresses).
FROM base AS libpostal-build
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential autoconf automake libtool pkg-config \
        curl ca-certificates git libsnappy-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch v1.1.4 https://github.com/openvenues/libpostal /src \
 && cd /src && ./bootstrap.sh \
 && ./configure --datadir=/opt/libpostal-data --prefix=/opt/libpostal \
 && make -j"$(nproc)" && make install \
 && rm -rf /src


# Binding Python `postal` sobre la lib ya compilada.
FROM deps AS deps-libpostal
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*
COPY --from=libpostal-build /opt/libpostal /opt/libpostal
COPY --from=libpostal-build /opt/libpostal-data /opt/libpostal-data
ENV LD_LIBRARY_PATH=/opt/libpostal/lib \
    PKG_CONFIG_PATH=/opt/libpostal/lib/pkgconfig
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install "postal>=1.1" \
 && PYTHONPATH=/install/lib/python3.12/site-packages \
    python -c "from postal.parser import parse_address; print(parse_address('Av. Corrientes 100'))"


# --- base comun de runtime: usuario y directorios ---------------------------
FROM base AS app-base
# TODOS los puntos de montaje se crean aca con el owner correcto. Un volumen
# nombrado sobre una ruta que NO existe en la imagen lo crea Docker como root,
# y el proceso (uid 10001) no puede escribir.
RUN mkdir -p /data/input /data/output /data/pbf /data/indexes /data/extracts /data/cache \
             /data/tmp /data/jobs /data/vocab /model-cache \
    && useradd -r -u 10001 -d /app smartimport \
    && chown -R smartimport /app /data /model-cache

ENV SMART_IMPORT_PBF_DIR=/data/pbf \
    SMART_IMPORT_INDEX_DIR=/data/indexes \
    SMART_IMPORT_EXTRACT_DIR=/data/extracts \
    SMART_IMPORT_CACHE_PATH=/data/cache/geocode_cache.sqlite \
    SMART_IMPORT_WORK_DIR=/data/jobs \
    SMART_IMPORT_VOCAB_PATH=/data/vocab/smart_import_vocab.sqlite \
    SMART_IMPORT_LOCALITY_GEONAMES_PATH=/data/vocab/locality_expand_geonames.json \
    SMART_IMPORT_DEVICE=cpu \
    HF_HOME=/model-cache

EXPOSE 8100
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8100"]


# --- runtime: el target por defecto -----------------------------------------
# Vocabulario: sqlite + GeoNames en /data/vocab (cada rebuild).
# SMART_IMPORT_VOCAB_GEONAMES=0 saltea la descarga (solo CABA/CDMX curados).
FROM app-base AS runtime
COPY --from=deps /install /usr/local
COPY --chown=smartimport smart_import ./smart_import
COPY --chown=smartimport schemas ./schemas
COPY --chown=smartimport scripts ./scripts
COPY --chown=smartimport pyproject.toml README.md ./
ARG SMART_IMPORT_VOCAB_GEONAMES=1
ENV SMART_IMPORT_VOCAB_GEONAMES=${SMART_IMPORT_VOCAB_GEONAMES}
RUN --mount=type=cache,target=/app/data/geonames \
    chmod +x /app/scripts/docker-entrypoint.sh /app/scripts/docker-vocab-setup.sh \
 && /app/scripts/docker-vocab-setup.sh --always \
 && chown -R smartimport /data/vocab /app/scripts
USER smartimport


# --- runtime + libpostal ----------------------------------------------------
FROM app-base AS runtime-libpostal
COPY --from=libpostal-build /opt/libpostal /opt/libpostal
COPY --from=libpostal-build /opt/libpostal-data /opt/libpostal-data
COPY --from=deps-libpostal /install /usr/local
COPY --chown=smartimport smart_import ./smart_import
COPY --chown=smartimport schemas ./schemas
COPY --chown=smartimport scripts ./scripts
COPY --chown=smartimport pyproject.toml README.md ./
ARG SMART_IMPORT_VOCAB_GEONAMES=1
ENV SMART_IMPORT_VOCAB_GEONAMES=${SMART_IMPORT_VOCAB_GEONAMES}
RUN --mount=type=cache,target=/app/data/geonames \
    chmod +x /app/scripts/docker-entrypoint.sh /app/scripts/docker-vocab-setup.sh \
 && /app/scripts/docker-vocab-setup.sh --always \
 && chown -R smartimport /data/vocab /app/scripts
USER smartimport
ENV LD_LIBRARY_PATH=/opt/libpostal/lib \
    SMART_IMPORT_LIBPOSTAL_ENABLED=true

