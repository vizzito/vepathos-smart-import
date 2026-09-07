# Guía de setup, deploy y operación — Smart Import

Guía práctica para instalar, correr en local, entender cada container, conectar la
web, deployar en prod, limpiar imágenes Docker y no reinstalar PyTorch de más.

Documentos relacionados:

| Doc | Para qué |
|---|---|
| **Este archivo (`SETUP.md`)** | Install, Docker, Rabbit/MinIO, local, prod, prune, comandos |
| [RUNBOOK.md](RUNBOOK.md) | Cómo **probar** el pipeline (pytest → demo → curl → web) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Diseño interno y gaps de producción |
| [examples/](examples/README.md) | Archivos de ejemplo (incl. force-ai) |

---

## Índice

1. [Qué es este servicio (en una frase)](#1-qué-es-este-servicio-en-una-frase)
2. [Arquitectura de containers](#2-arquitectura-de-containers)
3. [RabbitMQ y MinIO: ¿para qué? ¿son necesarios?](#3-rabbitmq-y-minio-para-qué-son-necesarios)
4. [Requisitos](#4-requisitos)
5. [Setup local — opción A: Docker (recomendado)](#5-setup-local--opción-a-docker-recomendado)
6. [Setup local — opción B: venv + CLI](#6-setup-local--opción-b-venv--cli)
7. [Conectar la web (`vepathos-router-client`)](#7-conectar-la-web-vepathos-router-client)
8. [Flujo que ejecuta la web](#8-flujo-que-ejecuta-la-web)
9. [Comandos útiles (cheatsheet)](#9-comandos-útiles-cheatsheet)
10. [Tests](#10-tests)
11. [Docker: build, cache, prune (no reinstalar torch)](#11-docker-build-cache-prune-no-reinstalar-torch)
12. [Deploy en producción](#12-deploy-en-producción)
13. [Variables de entorno](#13-variables-de-entorno)
14. [Diagnóstico rápido](#14-diagnóstico-rápido)
15. [Checklist “¿anda?”](#15-checklist-anda)

---

## 1. Qué es este servicio (en una frase)

HTTP en **`:8100`** que recibe planillas arbitrarias → **normalize** (reglas) →
opcional **extract** (heurísticas i18n, luego modelo si hace falta) → opcional
**geocode** (índice OSM local) → CSV/JSON Vepathos.

No es el optimizador ni el cutter: es un servicio **aditivo**. Si está apagado, el
upload clásico de la web sigue igual.

---

## 2. Arquitectura de containers

```
┌─────────────────────────────────────────────────────────────┐
│  docker compose                                              │
│                                                              │
│  smart-import (:8100)     ←── LO ÚNICO que usa la web hoy    │
│       │                                                      │
│       ├── volume: jobs, indexes, cache, model-cache          │
│       └── bind: ROUTE_OPTIMIZER_DATA → /data/pbf (PBF OSM) │
│                                                              │
│  ── profile infra (OPCIONAL) ─────────────────────────────  │
│  rabbitmq (:5672, UI :15672)   cola async (futuro/escala)    │
│  minio (:9000, UI :9001)       S3 local (futuro/escala)      │
│  minio-init                    crea el bucket una vez        │
│                                                              │
│  ── profile worker (OPCIONAL, necesita infra) ────────────  │
│  worker                        consume cola Rabbit           │
│                                                              │
│  ── profile tools / warmup (OPCIONAL) ────────────────────  │
│  tools                         CLI one-shot (list-pbf, …)    │
│  warmup                        precarga pesos del modelo     │
└─────────────────────────────────────────────────────────────┘

Browser → Next (/api/optimization/smart-import) → :8100
         (opcional) → RouteHub /imports/smart → :8100
```

| Container | Puerto | ¿Lo necesitás para la web local? |
|---|---|---|
| `vepathos-smart-import` | 8100 | **Sí** |
| `…-rabbit` | 5672 / 15672 | No |
| `…-minio` | 9000 / 9001 | No |
| `…-minio-init` | — | No (one-shot) |
| `…-worker` | — | No |
| `…-warmup` / `…-tools` | — | No |

---

## 3. RabbitMQ y MinIO: ¿para qué? ¿son necesarios?

### Hoy (MVP / web local)

La API procesa **en el mismo proceso** (threads):

`POST /imports` → extract → geocode → download  

Jobs en memoria + archivos en volumen Docker. **Rabbit y MinIO no intervienen.**

Por eso están detrás de `--profile infra`: no se levantan con un `compose up` simple.

### Para qué existen

| Pieza | Problema que resuelve cuando se cablee de punta a punta |
|---|---|
| **RabbitMQ** | No colgar HTTP con geocode/extract largos; worker aparte; reintentos; no perder trabajo si cae el pod a mitad |
| **MinIO / S3** | Artefactos (CSV) fuera del disco del container; varias réplicas de la API pueden leer el mismo job |
| **worker** | Consume la cola y corre geocode/extract offline |

Código de soporte: `smart_import/queue/`, `smart_import/storage/`. La API HTTP
**aún no encola** por defecto: es infra “prod-shaped” lista para cuando duela la
escala.

### ¿Son necesarios en prod?

| Escenario | Rabbit + S3 |
|---|---|
| 1 VM, 1 container, poco tráfico | **No** |
| Varias réplicas de la API | **Sí S3** (+ store compartido) |
| Imports grandes / no bloquear la web | **Sí Rabbit + worker** |
| Redeploys sin perder jobs a mitad | **Sí ambos** |

En prod real MinIO suele reemplazarse por **S3/GCS**; Rabbit por la cola que ya
usen (o se queda Rabbit).

### Cómo levantarlos (solo si querés probar infra)

```bash
docker compose --profile infra up -d          # rabbit + minio
docker compose --profile infra --profile worker up -d worker
```

UIs:

- Rabbit management: http://localhost:15672 (guest/guest)
- MinIO console: http://localhost:9001 (minioadmin/minioadmin)

Apagar:

```bash
docker compose --profile infra --profile worker stop
# o:
docker stop vepathos-smart-import-rabbit \
            vepathos-smart-import-minio \
            vepathos-smart-import-minio-init \
            vepathos-smart-import-worker 2>/dev/null
```

---

## 4. Requisitos

- Docker Desktop (o Engine + Compose v2)
- Python 3.12+ (solo si usás venv/CLI)
- PBFs OSM del route-optimizer (geocode local), típico:

  `~/workspace/route-optimizer-app/data`

- (Web) Node + `vepathos-router-client` con proxy Smart Import

---

## 5. Setup local — opción A: Docker (recomendado)

### 5.1 Primera vez

```bash
cd ~/workspace/vepathos-smart-import
cp .env.example .env
# Editá al menos:
#   ROUTE_OPTIMIZER_DATA=/ruta/absoluta/a/route-optimizer-app/data
```

Valores útiles en `.env` para local / prod (reglas + libpostal on-demand + geo):

```bash
SMART_IMPORT_TARGET=runtime-libpostal
SMART_IMPORT_LIBPOSTAL_ENABLED=true
SMART_IMPORT_GEOCODING_ENABLED=true
ROUTE_OPTIMIZER_DATA=/Users/VOSTRO/workspace/route-optimizer-app/data
SMART_IMPORT_PORT=8100
SMART_IMPORT_CORS_ORIGINS=*
```

Imagen chica sin libpostal (extract solo heurístico):

```bash
SMART_IMPORT_TARGET=runtime
SMART_IMPORT_LIBPOSTAL_ENABLED=false
```

### 5.2 Build + up (solo API)

El **build** corre `python -m smart_import.vocab setup --geonames` adentro de la
imagen (sqlite + ciudades >15k). El **entrypoint** vuelve a generar el sqlite
cada vez que arranca el container (toma el `catalog.json` montado). No hace falta
correr esos comandos a mano.

```bash
# Primera build: libpostal (si el target) + vocab/GeoNames. Sin modelo de IA.
DOCKER_BUILDKIT=1 docker compose build smart-import
docker compose up -d smart-import

# Health
curl -sf http://localhost:8100/health | python3 -m json.tool
```

Esperado en health: `"status":"ok"`, `capabilities.normalize=true`, y si
corresponde `geocoding`. En el log de arranque: `vocab: sqlite ← catalog`.

Docs interactivas: http://localhost:8100/docs

Para saltear GeoNames (solo CABA/CDMX curados): `SMART_IMPORT_VOCAB_GEONAMES=0`
en `.env` y rebuild.

### 5.3 Día a día (sin reinstalar librerías)

El compose monta `./smart_import` y `./schemas` en el container. Cambios de
código Python = **restart**, no rebuild:

```bash
docker compose up -d smart-import          # SIN --build
# o
docker compose restart smart-import
docker logs -f vepathos-smart-import
```

**No uses `--build`** salvo que cambien `Dockerfile`, deps de sistema o
`pyproject.toml` de forma que afecte paquetes instalados.

### 5.4 Profiles opcionales

```bash
docker compose --profile infra up -d                 # rabbit + minio
docker compose --profile warmup up                   # precarga modelo a volumen
docker compose --profile tools run --rm tools list-pbf --lat -34.6 --lon -58.4
docker compose --profile tools run --rm tools build-geocoder-index \
  --origin-lat -34.6 --origin-lon -58.4
```

---

## 6. Setup local — opción B: venv + CLI

```bash
cd ~/workspace/vepathos-smart-import
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,geo,api]"     # tests + geocoder + HTTP
# opcional:
pip install -e ".[libpostal]"       # parser C; ver docs/libpostal.md

# Lexicons: en Docker esto lo hace el build/entrypoint. En venv, una vez:
python -m smart_import.vocab setup              # sqlite (si no, el runtime arma en memoria)
python -m smart_import.vocab setup --geonames   # ciudades mundiales (descarga ~2–3 MB)

export SMART_IMPORT_PBF_DIR=~/workspace/route-optimizer-app/data
# o ROUTE_OPTIMIZER_DATA=...

python -m smart_import serve --port 8100
```

Smoke sin web: ver [RUNBOOK.md](RUNBOOK.md) (`demo.sh`, `http-smoke.sh`, curl).

### 6.1 Cobertura global (más allá del MVP)

Cuatro capas distintas. **No mezclarlas** (inflar el sqlite no mejora el pin):

| Capa | Qué es | Dónde vive | Qué NO es |
|---|---|---|---|
| Headers | `Dest.`, `Cant bultos` | `schemas/vepathos_flat_v1.json` | Códigos UNECE / ciudades |
| Celdas | caja / BX / delivered | `resources/vocab/catalog.json` → sqlite opcional | Calles OSM |
| Ciudades | CABA, Mumbai, Paris | `locality_expand.json` + `setup --geonames` | Índice de calles |
| Calles | Av Cabildo 3800 | PBF de **país** + extracts chicos | Vocab sqlite |

Los PBF del cutter viven en la raíz `route-optimizer-app/data`, **separados
por continente** (`south-america_tile_…/argentina-pyrosm.osm.pbf`,
`asia_tile_…/india/western-zone-pyrosm.osm.pbf`,
`north-america_tile_…/us_tile_…/florida-pyrosm.osm.pbf`). No hay un
`usa.pbf` / `india.pbf` único: son estados y zonas. Los extracts
(`data/_extracts/…/n…_s…_e…_w…-pyrosm.osm.pbf`) son reuso de menos size: si
existen, se elige el más chico que cubra el punto; si no, la subdivisión
cuyo bbox en `pbf_country_bounds.json` + `pbf_region_bounds.json` cubra.

Para que un `india-pyrosm.osm.pbf` (o `usa`, `japan`, …) se elija sin extract,
el slug tiene que estar en `smart_import/resources/pbf_country_bounds.json`
(bbox N/S/E/W + aliases). Editá el JSON, no Python. Un PBF sin bbox en el
nombre y **sin** entrada ahí no se usa (no adivinamos).

```bash
# Raíz con países + _extracts (no apuntes SMART_IMPORT_PBF_DIR solo a _extracts)
unset SMART_IMPORT_PBF_DIR
export ROUTE_OPTIMIZER_DATA=/ruta/al/route-optimizer-app/data

python -m smart_import.vocab setup            # conceptos; no calles
python -m smart_import.vocab setup --geonames # cities15000 → JSON, no sqlite
```

No bajes `allCountries` ni dumps de calles al sqlite. Eso ya está en el PBF.

---

## 7. Conectar la web (`vepathos-router-client`)

### 7.1 Directo a :8100 (local más simple)

En `vepathos-router-client/.env.local`:

```bash
SMART_IMPORT_URL=http://localhost:8100
# Dejar vacíos en local:
# ROUTEHUB_SMART_IMPORT_URL=
# ROUTEHUB_API_KEY=
```

Reiniciar `next dev`.

El browser llama `POST /api/optimization/smart-import`; el **server** Next habla
con `:8100` (no el browser).

### 7.2 Vía RouteHub (como prod)

```bash
# routehub
SMART_IMPORT_ENABLED=true
SMART_IMPORT_URL=http://host.docker.internal:8100   # o IP del host

# router-client .env.local
ROUTEHUB_SMART_IMPORT_URL=http://localhost:8000/imports/smart
ROUTEHUB_API_KEY=<bearer>
```

### 7.3 UI

Optimization → Upload → **Smart Input** (archivo o paste).

**Obligatorio:** depot seleccionado **con lat/lng**. Sin coords de depot no corre
geocode automático; todo va a `pending_geocode` (pin manual en el mapa).

---

## 8. Flujo que ejecuta la web

El proxy Next orquesta (no hace falta llamar extract a mano):

```
1. POST /imports?phone_region=AR          # normalize (sync)
2. Si columna mezclada / next_actions extract:
     POST /imports/{id}/extract           # reglas i18n primero; modelo solo si falla
     poll /progress hasta idle
3. Si needs_geocode + depot lat/lng:
     POST /imports/{id}/geocode?origin_lat&origin_lon
     poll hasta idle
4. download geocoded/flat + issues
5. JSON a la UI: { stops, pending_geocode, extract, geocode, warnings, summary }
```

En logs sanos de un paste estructurado (`Nombre <tel> → calle`):

```
DETECT   ia=no
EXTRACT  reglas primero…
DONE     por_reglas=N por_modelo=0
GEOCODE  …
```

Si ves 56 filas al modelo sin `por_reglas`, la imagen está vieja: restart con el
código montado o rebuild.

---

## 9. Comandos útiles (cheatsheet)

```bash
cd ~/workspace/vepathos-smart-import

# --- ciclo local ---
docker compose up -d smart-import
docker compose restart smart-import
docker compose stop smart-import
docker compose down
docker logs -f vepathos-smart-import
docker compose ps

# --- health / smoke ---
curl -sf http://localhost:8100/health | python3 -m json.tool
curl -sf http://localhost:8100/config | python3 -m json.tool
./scripts/http-smoke.sh          # si existe

# --- import rápido ---
FILE=examples/force-ai/04_marketplace_whatsapp.csv
curl -sf -X POST "localhost:8100/imports?phone_region=AR" -F "file=@$FILE" | python3 -m json.tool

# --- CLI (venv) ---
.venv/bin/python -m smart_import detect --input "$FILE"
.venv/bin/python -m smart_import normalize --input "$FILE" --output out/n.csv --emit flat,nested
.venv/bin/python -m smart_import list-pbf --lat -34.6 --lon -58.4

# --- infra opcional ---
docker compose --profile infra up -d
docker compose --profile infra --profile worker up -d
```

---

## 10. Tests

Usá **siempre** el venv del repo (no el de route-optimizer):

```bash
cd ~/workspace/vepathos-smart-import
source .venv/bin/activate
# si falta multipart:
pip install 'python-multipart'

pytest -q                          # suite completa (real_geo se skippea sin PBF)
pytest -q -m geocoding             # geocode sintético
pytest -q tests/test_vocab.py tests/test_mapping.py
pytest -q -m real_geo tests/test_geocode_accuracy.py   # CABA 13 + 2907; ver examples/geocode-truth/README.md

# PBF: raíz data/ del route-optimizer (extracts + país), no _extracts solo
# unset SMART_IMPORT_PBF_DIR
# export ROUTE_OPTIMIZER_DATA=/Users/…/route-optimizer-app/data
# .venv/bin/python -m smart_import geocode-accuracy --truth examples/geocode-truth/caba_stops_2907.json
```

---

## 11. Docker: build, cache, prune (no reinstalar torch)

### Por qué a veces “reinstala PyTorch siempre”

Antes, la capa de torch dependía de `pyproject.toml`. Cualquier cambio ahí
invalidaba ~400 s de download. **Ya está separado**: etapa `torch` aislada en el
`Dockerfile`. Cambiar código / pyproject **no** debe reinstalar torch.

### Día a dia

```bash
docker compose up -d smart-import     # sin --build
```

### Cuándo sí rebuild

- Cambió el `Dockerfile`
- Cambió el `RUN` de torch/transformers
- Querés imagen limpia tras prune

```bash
DOCKER_BUILDKIT=1 docker compose build smart-import
docker compose up -d smart-import
```

En un rebuild bueno deberías ver:

```
CACHED [torch 1/1] RUN pip install ... torch ...
```

### Limpiar imágenes viejas y rebuild limpio

```bash
cd ~/workspace/vepathos-smart-import
docker compose down

# Borrar SOLO imágenes de este proyecto
docker images 'vepathos/smart-import*' -q | xargs docker rmi -f 2>/dev/null

# Cache de build huérfano (no borra imágenes en uso de otros proyectos)
docker builder prune -f

# Build fresco (torch se baja UNA vez) + up
DOCKER_BUILDKIT=1 docker compose build smart-import
docker compose up -d smart-import
curl -sf http://localhost:8100/health | python3 -m json.tool
```

Nuclear (borra **todo** lo no usado en Docker Desktop — otros proyectos también):

```bash
docker system prune -a --volumes -f
```

Usalo solo si sabés lo que implica.

### Imagen chica (sin libpostal)

```bash
# .env
SMART_IMPORT_TARGET=runtime
SMART_IMPORT_LIBPOSTAL_ENABLED=false

DOCKER_BUILDKIT=1 docker compose build smart-import
docker compose up -d smart-import
```

---

## 12. Deploy en producción

### 12.0 Lexicons (automático, no es un servicio)

No hay container de vocabulario. En cada **rebuild / redeploy**:

1. Build: `scripts/docker-vocab-setup.sh --always` →
   `/data/vocab/smart_import_vocab.sqlite` + `locality_expand_geonames.json`
2. Arranque: el entrypoint refresca el sqlite (catálogo nuevo = conceptos nuevos
   sin rebuild si montás `./smart_import`). GeoNames solo si falta el JSON.

No corras `vocab setup` a mano en prod. Si el build no tiene red, fallá el
deploy o usá `SMART_IMPORT_VOCAB_GEONAMES=0` (solo ciudades curadas).

### 12.1 Mínimo viable (1 VM, mismo host que cutter/PBFs)

1. Clonar repo + `.env` de prod (CORS cerrado, secrets).
2. Montar `ROUTE_OPTIMIZER_DATA` (o sincronizar PBFs).
3. Elegir target:

   | | Prod (libpostal) | Sin libpostal |
   |---|---|---|
   | `SMART_IMPORT_TARGET` | `runtime-libpostal` | `runtime` |
   | `SMART_IMPORT_LIBPOSTAL_ENABLED` | `true` | `false` |
   | RAM tipica | 4–8g | 1–2g |
   | Extract libre | reglas + enhancer on-demand | solo heurístico |

4. Build + arrancar **solo** la API (el build genera sqlite + GeoNames):

```bash
DOCKER_BUILDKIT=1 docker compose build smart-import
docker compose up -d smart-import
# en logs: "vocab: sqlite ← catalog" y (1ª vez) "vocab: GeoNames cities15000"
```

5. Delante: RouteHub con `SMART_IMPORT_ENABLED=true` y
   `SMART_IMPORT_URL=http://smart-import:8100` (red Docker interna).
6. UI: `ROUTEHUB_SMART_IMPORT_URL=https://api…/imports/smart` + API key.

**No hace falta Rabbit/MinIO** en este modo.

Límites recomendados (compartiendo VM 8 CPU / 16 GB con cutter):

```bash
SMART_IMPORT_MEMORY_LIMIT=4g
SMART_IMPORT_CPUS=2
SMART_IMPORT_GEOCODE_WORKERS=1
SMART_IMPORT_EXTRACT_WORKERS=1
SMART_IMPORT_EXTRACT_MAX_ROWS=100   # o 500; extract es lento en CPU
SMART_IMPORT_MAX_ROWS=20000         # soft; hard en config 50k
SMART_IMPORT_CORS_ORIGINS=https://app.tudominio.com
```

### 12.2 Prod con escala (cuando duela)

1. Object storage real (S3) en lugar de MinIO.
2. Rabbit (o SQS) + `worker` para geocode/extract.
3. Store de jobs compartido (hoy sigue en memoria del proceso — gap documentado
   en ARCHITECTURE.md).
4. Auth solo vía RouteHub (no exponer `:8100` a Internet).

### 12.3 Healthchecks / monitoreo

```bash
curl -sf https://smart-import.internal/health
# capabilities, pbf_available, jobs
```

Logs: `docker logs -f vepathos-smart-import`

---

## 13. Variables de entorno

Ver `.env.example` completo. Resumen:

| Variable | Rol |
|---|---|
| `SMART_IMPORT_TARGET` | `runtime` \| `runtime-libpostal` (build stage Docker) |
| `SMART_IMPORT_LIBPOSTAL_ENABLED` | enhancer on-demand (requiere imagen libpostal) |
| `SMART_IMPORT_GEOCODING_ENABLED` | prende geocode OSM |
| `ROUTE_OPTIMIZER_DATA` | raíz de PBFs en el host |
| `SMART_IMPORT_PORT` | host port (default 8100) |
| `SMART_IMPORT_CORS_ORIGINS` | en prod: dominio real, no `*` |
| `SMART_IMPORT_VOCAB_PATH` | SQLite (Docker: `/data/vocab/…`; lo genera el build/entrypoint) |
| `SMART_IMPORT_VOCAB_GEONAMES` | `1` (default) baja cities15000 en el build; `0` lo saltea |
| `GEOCODE_MAX_LOW_CONFIDENCE_KM` | descarta low_confidence lejos del depot (default 15) |
| `RABBITMQ_*` / `S3_*` | solo con profile infra / worker |

---

## 14. Diagnóstico rápido

| Síntoma | Qué mirar |
|---|---|
| Web 503 “unavailable :8100” | `docker compose ps`, `curl /health`, `SMART_IMPORT_URL` |
| `pbf_available: 0` | `ROUTE_OPTIMIZER_DATA` mal montado |
| Todo a pending_geocode | falta `depotLat`/`depotLng` en el request |
| Extract lento + `por_modelo=N` alto | paste libre o imagen vieja sin heurísticas |
| Geocode basura (nombre+tel en address) | no corrió extract; proxy desactualizado |
| `python-multipart` en pytest | venv equivocado; usá `.venv` de este repo |
| Rebuild baja torch otra vez | usaste `docker builder prune -a` o cambió el `RUN` de torch; o cache BuildKit off |

```bash
# Cobertura OSM para un depot
curl -sf "localhost:8100/geocoding/coverage?lat=-34.60&lon=-58.38" | python3 -m json.tool
```

---

## 15. Checklist “¿anda?”

- [ ] `curl -sf localhost:8100/health` → `status=ok`
- [ ] Logs de arranque: `vocab: sqlite ← catalog`
- [ ] `pbf_available > 0` si querés geocode
- [ ] `.env.local` del client: `SMART_IMPORT_URL=http://localhost:8100`
- [ ] Depot con coordenadas en la UI
- [ ] Pegar `examples/force-ai/06_paste_ready.txt` o subir un CSV force-ai
- [ ] Logs: `EXTRACT` con `por_reglas` (o modelo solo en filas libres) → `GEOCODE`
- [ ] Mapa: stops con coords y/o diálogo de geolocalización manual

---

## Resumen de oro

```bash
# Local que importa
cp .env.example .env          # ROUTE_OPTIMIZER_DATA=...
DOCKER_BUILDKIT=1 docker compose build smart-import   # 1 vez: imagen + vocab + GeoNames
docker compose up -d smart-import                     # día a día SIN --build (entrypoint refresca sqlite)
curl -sf localhost:8100/health

# Rabbit/MinIO: NO hace falta para la web
# docker compose --profile infra up -d

# Limpiar y rebuild limpio
docker compose down
docker images 'vepathos/smart-import*' -q | xargs docker rmi -f 2>/dev/null
docker builder prune -f
DOCKER_BUILDKIT=1 docker compose build smart-import && docker compose up -d smart-import
```
