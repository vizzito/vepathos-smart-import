# Guía de setup, deploy y operación — Smart Import

Guía práctica para instalar, correr en local, entender cada container, conectar la
web, deployar en prod, limpiar imágenes Docker y no reinstalar PyTorch de más.

Documentos relacionados:

| Doc | Para qué |
|---|---|
| **Este archivo (`SETUP.md`)** | Install, Docker, concurrencia y escala, local, prod, prune, comandos |
| **[DEPLOY-PRODUCTION.md](DEPLOY-PRODUCTION.md)** | **Prod distribuido** (api-prod + VM worker + Mac libpostal): deploy, firewall, túneles, cheatsheet |
| [RUNBOOK.md](RUNBOOK.md) | Cómo **probar** el pipeline (pytest → demo → curl → web) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Diseño interno y gaps de producción |
| [examples/](examples/README.md) | Archivos de ejemplo (incl. columna-mezclada) |

---

## Índice

1. [Qué es este servicio (en una frase)](#1-qué-es-este-servicio-en-una-frase)
2. [Arquitectura de containers](#2-arquitectura-de-containers)
3. [Concurrencia: qué pasa con muchos usuarios a la vez](#3-concurrencia-qué-pasa-con-muchos-usuarios-a-la-vez)
4. [Requisitos](#4-requisitos)
5. [Setup local — opción A: Docker (recomendado)](#5-setup-local--opción-a-docker-recomendado) · [5.3 Día a día](#53-día-a-día-sin-reinstalar-librerías) · [5.3.1 Tres procesos en la misma Mac](#531-tres-procesos-en-la-misma-mac-no-confundirlos)
6. [Setup local — opción B: venv + CLI](#6-setup-local--opción-b-venv--cli)
7. [Conectar la web (`vepathos-router-client`)](#7-conectar-la-web-vepathos-router-client)
8. [Flujo que ejecuta la web](#8-flujo-que-ejecuta-la-web)
9. [Comandos útiles (cheatsheet)](#9-comandos-útiles-cheatsheet)
10. [Tests](#10-tests) · [10.0 Levantar `.venv`](#100-levantar-venv-recordatorio)
11. [Docker: build, cache, prune (capas libpostal)](#11-docker-build-cache-prune-capas-libpostal)
12. [Deploy en producción](#12-deploy-en-producción) · [12.4 Repartir el trabajo](#124-repartir-el-trabajo-entre-máquinas) · **[DEPLOY-PRODUCTION.md](DEPLOY-PRODUCTION.md)** (rollout api-prod + VM + Mac)
13. [Variables de entorno](#13-variables-de-entorno) · [13.1 Modo distribuido](#131-modo-distribuido-varios-nodos) · [13.3 Cuando algo se cae](#133-qué-pasa-cuando-algo-se-cae)
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
│       ├── volume: jobs, indexes, cache, extracts             │
│       └── bind: ROUTE_OPTIMIZER_DATA → /data/pbf (PBF OSM) │
│                                                              │
│  ── profile tools (OPCIONAL) ─────────────────────────────  │
│  tools                         CLI one-shot (list-pbf, …)    │
└─────────────────────────────────────────────────────────────┘

Browser → Next (/api/optimization/smart-import) → :8100
         (opcional) → RouteHub /imports/smart → :8100
```

Un solo container hace todo. No hay cola, ni Redis, ni object storage: ver
[§3](#3-concurrencia-qué-pasa-con-muchos-usuarios-a-la-vez).

| Container | Puerto | ¿Lo necesitás para la web local? |
|---|---|---|
| `vepathos-smart-import` | 8100 | **Sí** |
| `…-tools` | — | No |

---

## 3. Concurrencia: qué pasa con muchos usuarios a la vez

No hay cola, ni Redis, ni object storage. La API hace todo **en el mismo
proceso**: `POST /imports` → normalize → (geocode explícito) → download, con
jobs en memoria y archivos en un volumen Docker.

Es una decisión, no una deuda: un normalize de 50k filas tarda ~1,5 s, que no
justifica la infraestructura ni la operación de un trabajo asincrónico.

### Lo que hay que saber: una instancia rinde ~1 core

El normalize es Python CPU-bound, así que el **GIL lo serializa**. Medido con
archivos de 5k filas:

| Clientes simultáneos | Tiempo total |
|---|---|
| 1 | 1,3 s |
| 4 | 5,2 s |
| 8 | 10,5 s |

Crece lineal: 8 clientes tardan 8× lo que uno, con la CPU del container clavada
en **1 core aunque tenga 4 asignados**. Por eso subir `SMART_IMPORT_CPUS` o
`MAX_CONCURRENT_NORMALIZE` no da capacidad — solo reparte la misma CPU.

### Las tres puertas que evitan que la VM reviente

De afuera hacia adentro:

| Puerta | Variable | Qué protege |
|---|---|---|
| Concurrencia HTTP | `SMART_IMPORT_HTTP_LIMIT_CONCURRENCY` (512) | uvicorn corta con 503 antes de que el request toque la app: una avalancha no se vuelve miles de conexiones abiertas |
| **Admisión** | `SMART_IMPORT_MAX_NORMALIZE_QUEUE` (32) | Imports en el sistema a la vez. Se pide **antes de leer el body**, así que el 429 no toca disco |
| Techo de CPU | `SMART_IMPORT_MAX_CONCURRENT_NORMALIZE` (4) | Normalizes en paralelo. Deja hilos libres para `/health`, el polling y las descargas |

La del medio es la clave. El archivo se recibe antes de pedir turno de CPU (a
propósito: esperar con el upload a medio camino deja el socket abierto sin hacer
nada), y eso solo es seguro si hay un techo más afuera. Sin la admisión, 1000
uploads simultáneos escribirían hasta `1000 × MAX_FILE_MB` = **10 GB** en el
disco que compartimos con el cutter antes de rechazar a uno solo.

### Medición real de la avalancha

Clientes free (400 stops cada uno) subiendo **todos al mismo tiempo**, defaults
de fábrica:

| Clientes | Aceptados | 429 | Latencia del aceptado (p50) | `/health` | RAM |
|---|---|---|---|---|---|
| 50 | 32 | 18 | 2,1 s | 0 fallos, máx 48 ms | 61 MB |
| 200 | 34 | 166 | 2,2 s | 0 fallos, máx 203 ms | 61 MB |
| 500 | 37 | 463 | 2,7 s | 0 fallos, máx 490 ms | 61 MB |

El servicio no se cae, no se llena la RAM y `/health` nunca falla. Todo se
resuelve en menos de 5 s.

**El precio es explícito: con 500 simultáneos, 463 reciben 429.** Es la decisión
de diseño — un "reintentá en 5 s" inmediato es mejor que 500 personas esperando
un minuto. Requiere una cosa del cliente:

> El front **tiene que respetar `Retry-After`** y reintentar con backoff + jitter.
> Sin eso, el usuario ve un error en vez de una demora.

Subir la cola de admisión sirve poco. Con `MAX_NORMALIZE_QUEUE=200` y los mismos
500 clientes: 155 aceptados en vez de 37, pero la latencia del que entra salta
de 2,7 s a **11,4 s (máx 22 s)**. Se atiende 4× más gente y todos esperan 4× más.

### Cómo se agrega capacidad de verdad

Otra **instancia** (otra VM, mismo compose), no más procesos ni una cola. Dos
condiciones:

1. **Balanceo sticky** (por IP o cookie). Cada instancia solo conoce sus propios
   jobs: con round-robin, el `POST /imports` cae en una y el `POST /geocode` en
   otra, que responde 404. Por la misma razón `serve --workers 4` falla a
   propósito.
2. Dado el GIL, **2 instancias de 2 cores rinden más que 1 de 4**.

Lo que no sobrevive: un reinicio se lleva los jobs en vuelo (viven en memoria).
Los archivos ya descargados no se pierden; los imports a medio camino hay que
volver a subirlos.

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
cp deploy/templates/local-dev.env.template .env
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

El bind de `./smart_import` **no** está en `docker-compose.yml`. Vive en
`docker-compose.override.yml` (copiado del `.example`). Compose lo toma solo
si el archivo existe: en esta Mac de desarrollo sí; en api-prod **no** se crea.

Con override: cambios de Python = **restart**, no rebuild.

```bash
cd ~/workspace/vepathos-smart-import
docker compose up -d smart-import          # SIN --build
# o
docker compose restart smart-import
docker logs -f vepathos-smart-import
```

**No uses `--build`** salvo que cambien `Dockerfile`, deps de sistema o
`pyproject.toml` de forma que afecte paquetes instalados.

El mapa de calles bilingües (`data/street_aliases.sqlite` en el host) **no**
está en git. El container lo lee de `/data/cache/street_aliases.sqlite`
(volume persistente). Después de un barrido local, copialo y reiniciá:

```bash
docker cp data/street_aliases.sqlite vepathos-smart-import:/data/cache/street_aliases.sqlite
docker compose restart smart-import
```

Sin ese archivo el geocoder arranca igual (no-op). Los índices **nuevos** ya
indexan `name:fr`/`name:nl`; los índices viejos del volume necesitan el sqlite
para `Avenue Mozart` → `Mozartlaan`.

### 5.3.1 Tres procesos en la misma Mac (no confundirlos)

En `docker ps` hay **dos** containers de smart-import. El que apunta a prod
**no publica puerto** en el host. El lab local sí (`127.0.0.1:8100`).

| Qué | Container | Cómo lo reconocés en `docker ps` | ¿Es prod? |
|---|---|---|---|
| **Lab local** (podés tenerlo **parado**) | `vepathos-smart-import` | `127.0.0.1:8100->8100` + imagen `runtime-libpostal` | No. API aislada. |
| **Mac → cola de api-prod** | `si-worker-prod-mac` | `8100/tcp` **sin** bind al host; env `.env.prod.smart.local` | Sí. Jobs reales. |
| **CLI / accuracy** | ningún container | `.venv` | No. |

El lab **no hace falta** para que esta Mac procese prod. Para bajarlo:

```bash
docker compose stop smart-import
# o: docker stop vepathos-smart-import
```

Para **actualizar el worker que apunta a prod** no uses `docker compose`
a secas (eso es el lab). Usá [DEPLOY-PRODUCTION.md §6.7](DEPLOY-PRODUCTION.md#67-actualizar-código-en-la-mac-apunta-a-prod)
o [§13.4](DEPLOY-PRODUCTION.md#134-paso-3--mac-worker-libpostal-opcional).
Un `restart` de `vepathos-smart-import` **no** toca `si-worker-prod-mac`.

### 5.4 Profiles opcionales

```bash
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

El proxy Next orquesta:

```
1. POST /imports?phone_region=AR          # normalize (sync; separa columna mezclada)
2. Si needs_geocode + depot lat/lng:
     POST /imports/{id}/geocode?origin_lat&origin_lon
     poll hasta idle
3. download geocoded/flat + issues
4. JSON a la UI: { stops, pending_geocode, geocode, warnings, summary }
```

En logs sanos de un paste estructurado (`Nombre <tel> → calle`):

```
DETECT   text_mode=free_text
NORMALIZE …
DONE
GEOCODE  …          # solo si el usuario / la web lo dispara
```

---

## 9. Comandos útiles (cheatsheet)

```bash
cd ~/workspace/vepathos-smart-import

# --- lab local (:8100). Opcional. NO es la flota prod. ---
docker compose stop smart-import          # bajarlo (prod Mac no lo necesita)
docker compose up -d smart-import         # solo si querés probar en :8100
docker compose restart smart-import
docker cp data/street_aliases.sqlite vepathos-smart-import:/data/cache/street_aliases.sqlite
docker logs -f vepathos-smart-import

# --- Mac worker apuntando a prod (si-worker-prod-mac) ---
# ver DEPLOY-PRODUCTION.md §6 (alta) y §13.4 (rollout de código)
docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml -f docker-compose.mac.worker.yml ps
docker logs si-worker-prod-mac --tail 20

# --- health / smoke ---
curl -sf http://localhost:8100/health | python3 -m json.tool
curl -sf http://localhost:8100/config | python3 -m json.tool
./scripts/http-smoke.sh          # si existe

# --- import rápido ---
FILE=examples/columna-mezclada/04_marketplace_whatsapp.csv
curl -sf -X POST "localhost:8100/imports?phone_region=AR" -F "file=@$FILE" | python3 -m json.tool

# --- CLI (venv) ---
.venv/bin/python -m smart_import detect --input "$FILE"
.venv/bin/python -m smart_import normalize --input "$FILE" --output out/n.csv --emit flat,nested
.venv/bin/python -m smart_import list-pbf --lat -34.6 --lon -58.4

```

---

## 10. Tests

### 10.0 Levantar `.venv` (recordatorio)

Los tests y la CLI corren con el venv **de este repo**, en la carpeta **`.venv`**
(con punto), en la raíz de `vepathos-smart-import`.

| | |
|---|---|
| **Sí** | `~/workspace/vepathos-smart-import/.venv` |
| **No** | `route-optimizer-env` (es del optimizer, otro proyecto) |
| **No** | `/usr/bin/python3` del sistema (3.9 en Mac; el repo pide 3.11+) |

**Primera vez** (crear venv + dependencias):

```bash
cd ~/workspace/vepathos-smart-import
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,geo,api,queue]"   # tests + HTTP + cola (modo distribuido)
# opcional: pip install -e ".[libpostal]"
```

**Cada vez que abrís una terminal** para pytest o CLI:

```bash
cd ~/workspace/vepathos-smart-import
source .venv/bin/activate
# el prompt suele mostrar (.venv)
python --version    # → Python 3.12.x
```

Sin `activate`, usá la ruta explícita (evita confundir venvs):

```bash
cd ~/workspace/vepathos-smart-import
.venv/bin/python -m pytest tests -q --tb=line
```

> El `.env` / `.env.prod.smart.local` lo lee **Docker Compose**, no el venv.
> `pytest` no carga esas variables salvo que las exportes a mano.

### 10.1 Correr tests

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

## 11. Docker: build, cache, prune (capas libpostal)

### Por qué a veces “recompila libpostal siempre”

libpostal vive en la etapa `libpostal-build` del `Dockerfile` (casi nunca se
invalida). Cambiar código Python o `pyproject.toml` **no** debe recompilarla:
el `COPY` del código es la última capa.

### Día a dia

```bash
docker compose up -d smart-import     # sin --build
```

### Cuándo sí rebuild

- Cambió el `Dockerfile`
- Cambió la etapa `libpostal-build` / deps C
- Querés imagen limpia tras prune

```bash
DOCKER_BUILDKIT=1 docker compose build smart-import
docker compose up -d smart-import
```

En un rebuild bueno deberías ver capas `CACHED` para libpostal.

### Limpiar imágenes viejas y rebuild limpio

```bash
cd ~/workspace/vepathos-smart-import
docker compose down

# Borrar SOLO imágenes de este proyecto
docker images 'vepathos/smart-import*' -q | xargs docker rmi -f 2>/dev/null

# Cache de build huérfano (no borra imágenes en uso de otros proyectos)
docker builder prune -f

# Build fresco + up
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

Hay **tres rollouts** distintos. Este capítulo cubre el monolito y el GIL.
El layout actual (api-prod + VM + Mac) está en
**[DEPLOY-PRODUCTION.md](DEPLOY-PRODUCTION.md)**:

| Dónde | Qué actualizar | Doc |
|---|---|---|
| Mac, API `:8100` | `docker compose restart smart-import` (+ sqlite de alias) | [§5.3](#53-día-a-día-sin-reinstalar-librerías) |
| api-prod + VM worker | `git pull` + `build` + `up -d --force-recreate` | [DEPLOY §13](DEPLOY-PRODUCTION.md#13-actualizar-prod--rollout-de-código) |
| Mac worker → cola de prod | mismo, con `worker.yml` + `mac.worker.yml` | [DEPLOY §6](DEPLOY-PRODUCTION.md#6-agregar-una-mac-como-worker-libpostal) y [§13.4](DEPLOY-PRODUCTION.md#134-paso-3--mac-worker-libpostal-opcional) |

### 12.0 Lexicons (automático, no es un servicio)

No hay container de vocabulario. En cada **rebuild / redeploy**:

1. Build: `scripts/docker-vocab-setup.sh --always` →
   `/data/vocab/smart_import_vocab.sqlite` + `locality_expand_geonames.json`
2. Arranque: el entrypoint refresca el sqlite (catálogo nuevo = conceptos nuevos
   sin rebuild si montás `./smart_import`). GeoNames solo si falta el JSON.

No corras `vocab setup` a mano en prod. Si el build no tiene red, fallá el
deploy o usá `SMART_IMPORT_VOCAB_GEONAMES=0` (solo ciudades curadas).

### 12.1 ¿Cuántos procesos / workers? — **uno, y no es negociable**

Es la primera pregunta que aparece y la respuesta es contraintuitiva, así que va
antes que los pasos.

**Un solo proceso uvicorn por instancia.** No hay variable de entorno para
subirlo: `serve --workers 4` **falla con exit 2 y no arranca el servidor**, a
propósito. El almacén de jobs vive en memoria del proceso, así que con dos
workers el `POST /imports` cae en uno y el `POST /geocode` en el otro, que
responde **404 porque no conoce ese job**. Y falla de forma intermitente, que es
peor: con round-robin funciona la mitad de las veces.

Subir procesos tampoco daría capacidad. El normalize es Python CPU-bound y el
GIL lo serializa: **una instancia rinde ~1 core haga lo que haga** (medido en
[§3](#3-concurrencia-qué-pasa-con-muchos-usuarios-a-la-vez): 8 clientes tardan
8× lo que uno, con la CPU clavada en 1 core de 4).

Lo que sí se configura por instancia:

| Variable | Valor prod | Qué es |
|---|---|---|
| — (fijo en 1) | 1 | procesos uvicorn. No hay knob |
| `SMART_IMPORT_CPUS` | `2` | cores del container. Más de 2 no rinde por el GIL |
| `SMART_IMPORT_MAX_CONCURRENT_NORMALIZE` | `2` | normalizes en paralelo. **Bajalo a 2 si comparte VM con el cutter** |
| `SMART_IMPORT_MAX_NORMALIZE_QUEUE` | `32` | imports admitidos a la vez (cota de disco: 32 × `MAX_FILE_MB`) |
| `SMART_IMPORT_HTTP_LIMIT_CONCURRENCY` | `512` | techo de uvicorn. Cuenta los SSE de progreso, que son conexiones abiertas |
| `SMART_IMPORT_GEOCODE_WORKERS` | `1` | imports geocodificando a la vez. En la VM del cutter, 1 |

Para **más capacidad se agregan instancias**, no procesos: otra VM con este
mismo compose y el balanceador adelante con **routing sticky** (por IP o
cookie), porque cada instancia solo conoce sus propios jobs. Dado el GIL,
**2 instancias de 2 cores rinden más que 1 de 4.**

### 12.2 Paso a paso

#### Paso 1 — Preparar la VM

```bash
# Docker + compose v2 (el compose file usa `profiles` y anchors YAML)
docker --version && docker compose version

git clone <repo> /srv/vepathos-smart-import
cd /srv/vepathos-smart-import
```

#### Paso 2 — El `.env` de prod

Partí de la plantilla de api-prod (ver también [DEPLOY-PRODUCTION.md](DEPLOY-PRODUCTION.md)):

```bash
cp deploy/templates/api-prod.env.template .env
```

Estas son las que **hay que corregir para tu VM**; el resto ya viene con el
valor medido y documentado:

| Variable | Cambiar a | Por qué |
|---|---|---|
| `ROUTE_OPTIMIZER_DATA` | ruta real de los PBFs en la VM | sin esto el geocoder no tiene datos |
| `SMART_IMPORT_CORS_ORIGINS` | tu dominio real | **nunca `*` en prod** |
| `SMART_IMPORT_BIND` | `127.0.0.1` | ver la nota de abajo |
| `SMART_IMPORT_CPUS` | cores reales asignados | 2 si comparte con el cutter |
| `SMART_IMPORT_MEMORY_LIMIT` | `4g` sin libpostal, `8g` con | libpostal suma ~2,6 GB al primer uso |
| `SMART_IMPORT_MAX_CONCURRENT_NORMALIZE` | `2` si comparte VM | no competirle CPU al cutter |
| `IMAGE_TAG` | la versión que deployás | permite rollback (`docker compose up -d` con el tag viejo) |

> **`SMART_IMPORT_BIND=127.0.0.1` no es opcional.** El servicio **no tiene
> autenticación propia**: quien lo protege es la red. Y Docker escribe sus
> reglas en la cadena `DOCKER-USER`, que **puentea ufw**: publicado en `0.0.0.0`
> queda accesible desde internet aunque el firewall diga lo contrario. Entra
> solo por RouteHub.

Elegir imagen:

| | Prod (recomendado) | Imagen chica |
|---|---|---|
| `SMART_IMPORT_TARGET` | `runtime-libpostal` | `runtime` |
| `SMART_IMPORT_LIBPOSTAL_ENABLED` | `true` | `false` |
| Disco / RAM | +~2 GB / 4–8g | ~370 MB / 1–2g |
| Direcciones raras (parcela, manzana, India) | enhancer on-demand | solo heurístico |

#### Paso 3 — Build y arranque

```bash
DOCKER_BUILDKIT=1 docker compose build smart-import
docker compose up -d --remove-orphans smart-import
```

`--remove-orphans` limpia containers de servicios que ya no existen en el
compose. Si venís de una versión con RabbitMQ/MinIO, sin este flag Docker tira
un `WARN` en cada `up` y esos containers muertos quedan ocupando disco.

Lo que **`--remove-orphans` no borra son los volúmenes**, y ahí es donde queda
el disco de verdad (un `smart_import_models` de una versión vieja pesa ~1 GB).
Revisalos y borralos a mano, porque el comando es destructivo y conviene leer la
lista antes:

```bash
docker volume ls --filter name=smart_import
docker system df -v | grep smart_import      # tamaño de cada uno
# Los que siguen en uso: indexes, extracts, cache, jobs, vocab.
docker volume rm vepathos-smart-import_smart_import_models    # ejemplo
```

En los logs tienen que aparecer, en este orden:

```
vocab: sqlite ← catalog → /data/vocab/smart_import_vocab.sqlite
  Smart Import escuchando en http://0.0.0.0:8100
  parser: heuristic | libpostal: on | PBF dir: /data/pbf
  techo de concurrencia HTTP: 512 tareas | imports en paralelo: 2 (cola de admision 32)
INFO:     Application startup complete.
```

La última línea de config es la verificación de que el `.env` se aplicó. Si dice
otros números, el `.env` no se leyó (ver Paso 4).

#### Paso 4 — Verificar que el `.env` se aplicó

Es el paso que más se saltea y el que más cuesta después.

```bash
# 1) El container está sano (el healthcheck da margen por el vocab del arranque)
docker compose ps          # esperado: (healthy)

# 2) Config EFECTIVA del proceso, no la del archivo
curl -s localhost:8100/config | python3 -m json.tool | head -40

# 3) Capacidades y carga
curl -s localhost:8100/health | python3 -m json.tool
#   capabilities.geocoding: true
#   geocoding.pbf_available: 447       <-- es un CONTEO. En 0, ROUTE_OPTIMIZER_DATA está mal
#   geocoding.city_centroids: true     <-- en false, falta GeoNames (y falla en silencio)
#   load.normalize_slots / admission_slots: tus valores
```

> **Ojo con dos trampas de configuración.**
> El `.env` lo lee **docker-compose, no Python**: correr `pytest` o un script con
> el venv **no aplica nada** de ese archivo. Y una variable **exportada en el
> shell le gana al `.env`**, así que si cambiás un valor y no toma:
> `unset SMART_IMPORT_CPUS SMART_IMPORT_MEMORY_LIMIT`.

#### Paso 5 — Smoke real, sin la web

No hace falta subir un archivo por la UI para saber si anda:

```bash
./scripts/http-smoke.sh                    # end-to-end contra :8100

# o a mano, con un paste de WhatsApp:
printf 'Av Cabildo 900 1A, Belgrano, CABA | 11-5555-0100\n' > /tmp/t.txt
JOB=$(curl -s -F "file=@/tmp/t.txt" "localhost:8100/imports?phone_region=AR" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')
curl -s "localhost:8100/imports/$JOB/download?format=flat"
curl -s -X DELETE "localhost:8100/imports/$JOB"
```

#### Paso 6 — Ponerlo detrás de RouteHub

```bash
# RouteHub
SMART_IMPORT_ENABLED=true
SMART_IMPORT_URL=http://smart-import:8100        # red Docker interna
# UI
ROUTEHUB_SMART_IMPORT_URL=https://api.tudominio.com/imports/smart
```

**El front tiene que respetar `Retry-After` en los 429**, con backoff + jitter.
No es un detalle: es lo que convierte la protección de sobrecarga en una demora
en vez de un error en la cara del usuario ([§3](#3-concurrencia-qué-pasa-con-muchos-usuarios-a-la-vez)).

### 12.3 Operación

```bash
# Logs (ya rotan: json-file, 10 MB × 3)
docker logs -f vepathos-smart-import

# Actualizar
git pull
DOCKER_BUILDKIT=1 docker compose build smart-import
docker compose up -d --remove-orphans smart-import

# Rollback: poner el IMAGE_TAG anterior en .env y
docker compose up -d smart-import
```

**Qué mirar en `/health`:**

| Señal | Significa |
|---|---|
| `load.normalize_in_flight` pegado a `normalize_slots` | los clientes nuevos esperan o reciben 429. **No subas el cupo — agregá una instancia** |
| `load.admitted` pegado a `admission_slots` | se rechaza antes de leer el archivo. Misma conclusión |
| `environment_age_s` mucho mayor a 30 | murió el refresco de fondo y lo que reporta es viejo |
| `geocoding.pbf_available: false` | el mount de PBFs se cayó; el geocode va a fallar |

### 12.4 Repartir el trabajo entre máquinas

> **Topología en prod hoy (api-prod + VM worker + Mac libpostal):** guía paso a paso,
> firewall Hetzner, overlays y troubleshooting en **[DEPLOY-PRODUCTION.md](DEPLOY-PRODUCTION.md)**.
> Los pasos con `samevm` más abajo son la arquitectura **anterior** (API y worker en
> la misma VM de PBFs).

Todo lo anterior describe **una** VM que hace todo. Esta sección es la otra
forma: la VM sigue recibiendo los archivos y sirviendo las descargas, pero el
trabajo pesado lo hacen otras máquinas —incluida una Mac de escritorio— que se
prenden y se apagan sin coordinar nada.

**Cuándo vale la pena.** Cuando el techo de la VM se nota (imports esperando
turno o 429 en horario pico), o cuando hay CPU ociosa en otra máquina que no se
puede exponer a internet. Si con una instancia alcanza, no lo hagas: son tres
piezas más de infraestructura para operar.

**Lo que tiene que existir antes:** un RabbitMQ y un Redis alcanzables desde
los dos lados. Son los mismos que ya usa el optimizer; lo único que separa a
los dos servicios es el prefijo de las colas y la DB de Redis.

#### El orden importa, y hay un bloqueo

**No se puede sumar un worker a un stack que todavía no encola.** Mientras la VM
corra en `embedded`, no existe ninguna cola: una máquina nueva se conectaría a un
broker sin nada que consumir y se quedaría mirando. Así que el orden no es
negociable — primero la VM pasa a `api`, después se le suman nodos.

Lo que hace que eso no dé miedo: **la VM sigue siendo la misma, en la misma IP y
el mismo puerto**, y RouteHub no se toca. Lo único que cambia es quién hace el
trabajo adentro. La vuelta atrás es una variable y diez segundos.

#### Paso 0 — El ensayo, sin tocar producción

Antes de tocar la VM conviene correr el flujo entero en local, contra un broker
y un Redis de juguete que no chocan con nada:

```bash
docker run -d --name si-local-rabbit -p 5674:5672 -p 15674:15672 rabbitmq:3-management
docker run -d --name si-local-redis  -p 6399:6379 redis:7-alpine

cp .env .env.local-api        # partí del .env que ya usás
cat >> .env.local-api <<'EOF'
SMART_IMPORT_ROLE=api
RABBITMQ_HOST=host.docker.internal
RABBITMQ_PORT=5674
REDIS_HOST=host.docker.internal
REDIS_PORT=6399
SMART_IMPORT_REDIS_DB=1
SMART_IMPORT_QUEUE_PREFIX=local
SMART_IMPORT_WORKER_TOKEN=un-token-de-prueba
EOF

docker compose --env-file .env.local-api up -d smart-import
# y el worker, con el overlay de la misma máquina
docker compose --env-file .env.worker \
  -f docker-compose.worker.yml -f docker-compose.samevm.worker.yml up -d
```

Subí un archivo por el 8100 de siempre y seguilo. Si `POST /imports` devuelve
201 con el reporte adentro y el geocode termina, el contrato no cambió y lo que
sigue es repetirlo apuntando a la infraestructura de verdad.

Para volver a como estaba: `docker compose down` del worker y
`docker compose up -d smart-import` **sin** `--env-file` (vuelve a leer tu `.env`
y el rol vuelve a `embedded`).

#### Paso 1 — Convertir la VM en el nodo `api`

En su `.env`, usar la plantilla `deploy/templates/api-prod.env.template` (rol
`api`, broker, Redis y token). Después:

```bash
openssl rand -hex 32                        # el token, el MISMO en los dos lados
docker compose up -d smart-import
curl -s localhost:8100/health | python3 -m json.tool | grep -A3 deployment
```

Si falta el broker o Redis, **el proceso no arranca**. Es a propósito: una API
que acepta archivos sin tener a quién encargarle el trabajo deja jobs colgados
que nadie va a terminar.

Desde acá, esta VM no normaliza ni geocodifica nada: `MAX_CONCURRENT_NORMALIZE`
deja de tener efecto y el techo real pasa a ser cuántos workers hay prendidos.
Y como el estado vive en Redis, el balanceo **ya no necesita ser sticky**:
cualquier nodo api contesta por cualquier job.

#### Paso 2 — Prender el worker de la misma VM

**Antes que ninguna máquina de afuera.** Desde el paso anterior, la VM no
procesa nada por su cuenta: si no hay un solo worker, los archivos se aceptan y
se encolan sin que nadie los haga. Este worker es el piso del servicio; los de
afuera suman capacidad y se pueden ir sin que se note.

```bash
cp deploy/templates/worker-vm.env.template .env.worker
# el token del Paso 1, el broker y Redis por su IP privada, y —como esta
# maquina tiene los PBF montados— SMART_IMPORT_CONSUME_GEOCODE=true

docker compose --env-file .env.worker \
  -f docker-compose.worker.yml -f docker-compose.samevm.worker.yml up -d
```

> **Prod actual:** API en api-prod y worker en VM separada — ver
> [DEPLOY-PRODUCTION.md](DEPLOY-PRODUCTION.md). El overlay `samevm` quedó
> obsoleto en esa topología.

`.env.worker` es un **overlay**, no la config completa del nodo: el compose lee
primero el `.env` del despliegue (el mismo del nodo api) y después este, que
gana. Los umbrales tienen que ser los mismos en los dos lados o el resultado
depende de qué nodo agarre el archivo — medido acá con el mismo CSV: con
`GEOCODE_VALID_BAND=0.81` da 20 pines verdes, con `0.85` da 4. La prueba de que
están alineados es `fleet.config_drift` vacío en `/health`.

El overlay `samevm` resuelve un detalle que muerde: la api publica en
`127.0.0.1:8100` del host, así que desde un container esa dirección no existe.
El worker entra a la red del compose de la api y le pide por su nombre de
servicio, igual que hace RouteHub.

#### Paso 3 — Sumar workers de otras máquinas

En la otra máquina, con el repo clonado. Copiá también el `.env` del nodo api
—de ahí salen los umbrales compartidos— y dejá `.env.worker` solo para lo que
cambia:

```bash
scp deploy@178.105.42.199:~/vepathos-smart-import/.env .env
cp deploy/templates/worker-vm.env.template .env.worker
# editar: RABBITMQ_HOST, REDIS_HOST, SMART_IMPORT_API_URL y el token del Paso 1

# ¿llega a las tres piezas? Esto no consume nada: prueba y sale.
docker compose --env-file .env.worker -f docker-compose.worker.yml \
  run --rm worker worker --check

docker compose --env-file .env.worker -f docker-compose.worker.yml up -d
```

`--check` es el primer comando a correr en una máquina nueva. Prueba la cola,
Redis y la api por separado, así el que falla se ve solo en vez de aparecer
como «el import no avanza» media hora después.

#### Paso 4 — El caso de la Mac (o cualquier máquina detrás de NAT)

La Mac no tiene IP pública ni está en la red privada del servidor. No hace
falta: **todas las conexiones las abre el worker**. Un túnel SSH alcanza.

```bash
# ~/.ssh/config
Host vepathos-tunnel
  HostName <ip-publica-del-server>
  User <usuario>
  LocalForward 16379 10.0.0.2:6379     # Redis
  LocalForward 5673  10.0.0.2:5672     # RabbitMQ
  LocalForward 8110  127.0.0.1:8100    # la api de smart-import
  ServerAliveInterval 30
  ExitOnForwardFailure yes
```

La dirección de la derecha **la resuelve el servidor**, no tu máquina: por eso
Redis y RabbitMQ se piden por su IP en la red privada, y la api —que corre en
la misma VM a la que entrás y publica en loopback— se pide como `127.0.0.1`. Si
la movés a otra VM, ahí va su IP privada.

```bash
ssh -N vepathos-tunnel &

docker compose --env-file .env.worker \
  -f docker-compose.worker.yml -f docker-compose.mac.worker.yml up -d
```

El overlay es el mismo patrón que `docker-compose.mac.cutter.yml` del
optimizer: reemplaza los hosts por `host.docker.internal` y los puertos por los
del túnel. Los puertos altos (16379/5673/8110) evitan chocar con lo que ya
corre en la Mac — un Redis local en 6379 se llevaría los jobs de otro lado.

Si el túnel se cae, no se pierde nada: el worker deja de consumir, reintenta la
conexión solo, y las tareas quedan en la cola para el que pueda tomarlas.

#### La vuelta atrás

En cualquier punto, y sin tocar RouteHub:

```bash
# 1. bajar los workers (las tareas en vuelo vuelven a la cola)
docker compose --env-file .env.worker -f docker-compose.worker.yml down

# 2. el nodo api vuelve a hacer el trabajo él mismo
#    comentar SMART_IMPORT_ROLE=api en el .env  (o ponerlo en embedded)
docker compose up -d smart-import
curl -s localhost:8100/health | python3 -c \
  "import json,sys; print(json.load(sys.stdin)['deployment'])"
```

Lo que se pierde: los jobs que estaban en vuelo en ese momento, porque su estado
vivía en Redis y el modo `embedded` no lo lee. Son minutos de trabajo, no datos
del usuario — el archivo original lo tiene él. Por eso conviene hacer el cambio
en una ventana tranquila, aunque no requiera una.

#### Paso 5 — RouteHub y la web: nada que tocar

El contrato HTTP no cambió. `POST /imports` sigue devolviendo 201 con el job
entero cuando el resultado llega dentro de `SMART_IMPORT_DEFAULT_WAIT_S`, y
`202` con el `job_id` cuando tarda más ([§13.2](#132-qué-cambia-para-quien-consume-la-api)).
Lo único que conviene revisar es que el cliente siga el `job_id` por
`GET /imports/{id}` o por SSE, que es lo que la UI ya hace para el geocode.

#### Paso 6 — La verificación que importa

No es «el container está arriba», es **ver un import resolviéndose en la otra
máquina sin haber tocado el `.env` del servidor**:

```bash
# en el server
JOB=$(curl -s -F "file=@fixtures/es_sin_coords.csv" localhost:8100/imports \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

# en la Mac: el trabajo aparece acá
docker logs -f vepathos-smart-import-worker

# en el server: el resultado lo sirve la api, no el worker
curl -s "localhost:8100/imports/$JOB/download?format=flat" | head -3
```

Y la prueba que de verdad justifica todo esto: **bajar la Mac a mitad de un
job** (`docker compose stop`, o directamente cerrar la laptop) y ver que otro
nodo lo termina. Un `stop` drena lo que tiene en vuelo; un corte seco deja la
tarea sin confirmar y otro worker la retoma en 1-2 minutos.

#### Operar los nodos

```bash
# más capacidad: prender otro worker (misma imagen, otro nombre)
SMART_IMPORT_WORKER_NAME=worker-2 docker compose --env-file .env.worker \
  -f docker-compose.worker.yml up -d

# sacar uno de circulación: SIGTERM, termina lo que está haciendo y sale
docker compose --env-file .env.worker -f docker-compose.worker.yml stop

# qué hay pendiente y qué fracasó
rabbitmqctl list_queues name messages | grep smart-import
```

**Volver atrás** es una variable: `SMART_IMPORT_ROLE=embedded` en el `.env` del
servidor y `docker compose up -d`. La VM vuelve a hacer todo en su proceso, como
antes. Lo que se pierde son los jobs que estaban en Redis (la API vuelve a su
almacén en memoria); las tareas que quedaron en la cola siguen ahí, esperando a
un worker que ya no va a existir — conviene purgarlas.

**Una trampa a tener presente:** en el nodo api, `GEOCODING_ENABLED=true`
significa «este despliegue ofrece geocodificar», no «esta máquina puede». Quien
puede es un worker con `CONSUME_GEOCODE=true` y los PBF montados. Si no hay
ninguno prendido, los geocodes se encolan y esperan. Quién está consumiendo qué
se ve en `GET /health` → `fleet`:

```json
"fleet": {
  "workers": [{"node": "mac-de-martin:4116", "queues": ["smart-import-normalize",
               "smart-import-geocode"], "slots": 2, "geocoding": true}],
  "queues": {"smart-import-normalize": 0, "smart-import-geocode": 0, "smart-import-dlq": 0},
  "config_drift": []
}
```

Una cola de geocode que crece con `workers` vacío de `geocoding: true` es
exactamente ese caso.

**Lo que un reinicio se lleva:** los jobs en vuelo, porque viven en memoria. Los
archivos ya descargados no se pierden; los imports a medio camino hay que
volver a subirlos. Es el gap #1 de ARCHITECTURE.md y la razón por la que varias
instancias necesitan balanceo sticky.

---

## 13. Variables de entorno

**La fuente de verdad son las plantillas en `deploy/templates/`** y el `.env`
real de cada máquina (gitignored). Copiá la plantilla que corresponda:

| Plantilla | Destino | Uso |
|-----------|---------|-----|
| `local-dev.env.template` | `.env` | Desarrollo local (monolito) |
| `api-prod.env.template` | `.env` | Nodo API en api-prod |
| `worker-vm.env.template` | `.env.worker` | Worker en VM PBFs |
| `worker-mac.env.template` | `.env.prod.smart.local` | Worker Mac + libpostal |

Ver [deploy/templates/README.md](deploy/templates/README.md) y
[DEPLOY-PRODUCTION.md](DEPLOY-PRODUCTION.md).

Resumen de las que más se toca. La config **efectiva** de un proceso corriendo
se lee siempre en `GET /config`, que es lo único que no miente:

| Variable | Default | Rol |
|---|---|---|
| **Imagen y datos** | | |
| `SMART_IMPORT_TARGET` | `runtime-libpostal` | build stage: `runtime` (~370 MB) o con libpostal (+~2 GB) |
| `ROUTE_OPTIMIZER_DATA` | — | raíz de PBFs en el host. Sin esto no hay geocode |
| `SMART_IMPORT_GEONAMES_DIR` | `./data/geonames` | cities15000. Sin esto, centroides de ciudad y detección de localidad quedan apagados **en silencio** |
| **Red y seguridad** | | |
| `SMART_IMPORT_BIND` | `127.0.0.1` | interfaz donde se publica. **En prod, loopback**: no hay auth propia y Docker puentea ufw |
| `SMART_IMPORT_PORT` | `8100` | puerto del host |
| `SMART_IMPORT_CORS_ORIGINS` | `*` | en prod: el dominio real |
| **Concurrencia** ([§12.1](#121-cuántos-procesos--workers--uno-y-no-es-negociable)) | | |
| `SMART_IMPORT_HTTP_LIMIT_CONCURRENCY` | `512` | techo de uvicorn (503 por encima). Cuenta los SSE abiertos |
| `SMART_IMPORT_MAX_NORMALIZE_QUEUE` | `32` | admisión: 429 **antes de leer el body**. Cota de disco = esto × `MAX_FILE_MB` |
| `SMART_IMPORT_MAX_CONCURRENT_NORMALIZE` | `4` | normalizes en paralelo. Techo, no acelerador (GIL) |
| `SMART_IMPORT_NORMALIZE_QUEUE_WAIT_S` | `20` | espera por un turno antes del 429 |
| `SMART_IMPORT_GEOCODE_WORKERS` | `1` | imports geocodificando a la vez |
| `SMART_IMPORT_CPUS` / `_MEMORY_LIMIT` | `4` / `8g` | recursos del container |
| `SMART_IMPORT_JOB_TTL_HOURS` | `24` | horas antes de borrar un job terminado. `0` = el disco crece sin techo |
| **Límites de entrada** | | |
| `SMART_IMPORT_MAX_FILE_MB` | `10` | ~50k filas de CSV |
| `SMART_IMPORT_MAX_ROWS` | `50000` | filas por archivo |
| **Extracción** | | |
| `SMART_IMPORT_LIBPOSTAL_ENABLED` | `true` | enhancer on-demand (requiere imagen libpostal) |
| `SMART_IMPORT_DEFAULT_PHONE_REGION` | — | ISO para teléfonos (`AR`, `US`, `IN`). El job la pisa |
| `SMART_IMPORT_DELIVERY_ACCEPT_THRESHOLD` | `0.55` | cuándo una línea del paste es una parada. **Medido**: ruido tope 0,05 vs mediana 0,83 |
| `SMART_IMPORT_ADDRESS_ACCEPT_THRESHOLD` | `0.50` | piso para aceptar una dirección. **Medido: subirlo no sirve** — ver el comentario en el `.env` antes de tocarlo |
| **Geocoding** | | |
| `SMART_IMPORT_GEOCODING_ENABLED` | `true` | prende geocode OSM (nunca es automático) |
| `SMART_IMPORT_STREET_ALIASES` | `/data/cache/street_aliases.sqlite` | mapa name↔name:fr/nl (Docker). En venv: `data/street_aliases.sqlite`. Ausente = no-op |
| `GEOCODER_FALLBACK` | `none` | lo que OSM no encuentra queda `not_found` para ubicación manual |
| `GEOCODE_MATCH_THRESHOLD` / `GEOCODE_VALID_BAND` | `0.85` | status `matched` y color verde. **Tienen que ser iguales** o la UI pinta verde algo que el geocoder marcó dudoso |
| `GEOCODE_LOW_CONFIDENCE_THRESHOLD` / `GEOCODE_REVIEW_BAND` | `0.70` | piso para devolver coordenada y para mostrarla. **Iguales** |
| `GEOCODE_SOFT_REJECT_MIN` | `0.75` | pin de respaldo. **≥ `REVIEW_BAND`** o la banda le saca el pin igual |
| `GEOCODE_STREET_MATCH_MIN` | `0.70` | cuánto tiene que matchear la calle. Guardián del falso positivo más caro |
| `GEOCODE_MAX_DISTANCE_KM` / `_MAX_LOW_CONFIDENCE_KM` | `500` / `15` | geofences: atrapan la calle homónima en otra provincia |
| **Vocabulario** | | |
| `SMART_IMPORT_VOCAB_PATH` | `/data/vocab/…` | SQLite; lo genera el build/entrypoint |
| `SMART_IMPORT_VOCAB_GEONAMES` | `1` | `0` saltea cities15000 en el build (útil sin red) |
| **Modo distribuido** ([§13.1](#131-modo-distribuido-varios-nodos)) | | |
| `SMART_IMPORT_ROLE` | `embedded` | `embedded` (todo en un proceso, lo de siempre), `api` o `worker` |
| `REDIS_HOST` / `REDIS_PORT` | — / `6379` | estado de los jobs. **Obligatorio** con rol `api` o `worker` |
| `RABBITMQ_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_VHOST` | — / `5672` / — / — / `/` | la cola de tareas. **Obligatorio** con rol `api` o `worker` |
| `SMART_IMPORT_QUEUE_PREFIX` | `smart-import` | prefijo de las colas. Dos despliegues contra el mismo broker necesitan prefijos distintos |
| `SMART_IMPORT_WORKER_TOKEN` | — | credencial compartida del canal api↔worker. **Vacío = `/internal` cerrado** |
| `SMART_IMPORT_API_URL` | — | de dónde baja el worker los archivos. Obligatorio en rol `worker` |
| `SMART_IMPORT_SCRATCH_DIR` | temp del sistema | espacio de trabajo del worker. Se borra al terminar cada tarea |
| `SMART_IMPORT_CONSUME_NORMALIZE` / `_CONSUME_GEOCODE` | `true` / `false` | de qué colas come este worker. Geocode **solo** donde hay PBF montado |
| `SMART_IMPORT_WORKER_SLOTS` | `2` | tareas en paralelo por worker. De acá sale el prefetch |
| `SMART_IMPORT_MAX_REQUEUE_ATTEMPTS` | `10` | intentos antes de apartar una tarea a la DLQ |
| `SMART_IMPORT_TASK_TIMEOUT_NORMALIZE_S` / `_GEOCODE_S` | `300` / `1800` | pasado esto la tarea se aparta y el job queda fallido, en vez de girar para siempre |
| `SMART_IMPORT_SHUTDOWN_DRAIN_S` | `60` | cuánto espera un worker que baja a lo que está en vuelo |
| `SMART_IMPORT_RUN_LOCK_TTL_S` | `30` | cuánto se espera antes de dar por muerto a un nodo callado. **No** es cuánto puede durar una tarea (ver abajo). Piso: 10 |
| `SMART_IMPORT_DEFAULT_WAIT_S` | `30` | cuánto espera `POST /imports` el resultado antes de responder 202. `0` = siempre 202 |

Las tres reglas de alineación de las bandas de geocode están explicadas en los
`.env`: romperlas no da error, solo hace que el operador vea un color y el
sistema haya decidido otra cosa.

### 13.1 Modo distribuido (varios nodos)

Con `SMART_IMPORT_ROLE=embedded` —el default— **no cambia nada**: un proceso,
estado en memoria, archivos en su disco. Las variables de arriba no se leen.

Con roles, el servicio se parte en dos y aparecen tres canales entre ellos:

- **estado** — los jobs viven en Redis, así que cualquier nodo contesta
  `GET /imports/{id}` aunque el trabajo lo haya hecho otro;
- **archivos** — el rol `api` guarda el archivo del usuario y sirve las
  descargas; el `worker` no tiene ninguno de los dos. Se los pide por
  `/internal/…`, trabaja en su scratch y devuelve el resultado;
- **trabajo** — la api no normaliza ni geocodifica: publica una tarea en
  RabbitMQ y el worker la toma. Por la cola viajan referencias (`job_id` y los
  parámetros del pedido), nunca archivos.

Todas las conexiones las abre el worker: no necesita IP entrante ni volumen
compartido, y por eso puede correr en cualquier máquina.

`/internal` no es API pública (no está en `/docs`) y **exige
`SMART_IMPORT_WORKER_TOKEN`**: sin token configurado, o con uno que no coincide,
cada ruta responde 404 — quien no lo tenga no se entera ni de que existe. El
token viaja en texto plano, así que el enlace api↔worker va por red privada o
TLS, igual que Redis.

```bash
# nodo api  (sirve HTTP, no procesa)
SMART_IMPORT_ROLE=api  REDIS_HOST=10.0.0.5  RABBITMQ_HOST=10.0.0.5 \
SMART_IMPORT_WORKER_TOKEN=$TOKEN
smart-import serve

# nodo worker (otra máquina, sin volúmenes; no sirve HTTP)
SMART_IMPORT_ROLE=worker  REDIS_HOST=10.0.0.5  RABBITMQ_HOST=10.0.0.5 \
SMART_IMPORT_WORKER_TOKEN=$TOKEN  SMART_IMPORT_API_URL=http://10.0.0.4:8100 \
SMART_IMPORT_SCRATCH_DIR=/var/tmp/si
smart-import worker
```

Que un nodo esté en modo distribuido se ve en `GET /health` →
`deployment: {role, state}`. Un worker al que le falta `API_URL`, el token, el
broker o los PBF que dice consumir **no arranca**: imprime todo lo que falta y
sale con código 2. Es preferible a que falle la primera tarea media hora después.
Para probar las tres conexiones sin consumir nada —lo primero a correr en una
máquina nueva— está `smart-import worker --check`.

Los comandos de arriba son el modo desnudo, para entender qué es cada pieza. El
despliegue con Docker, el overlay para una máquina detrás de NAT y la puesta en
marcha paso a paso están en
[§12.4](#124-repartir-el-trabajo-entre-máquinas).

### 13.2 Qué cambia para quien consume la API

Casi nada, y a propósito. `POST /imports` encola y **espera** hasta
`SMART_IMPORT_DEFAULT_WAIT_S` (o el `?wait=` del request):

- si el resultado llega a tiempo → **201 con el job entero**, byte por byte lo
  mismo que devuelve un despliegue de un solo proceso;
- si no llega → **202** con el `job_id`, `poll` y `events`. El trabajo sigue; el
  cliente lo mira por `GET /imports/{id}` o por SSE, que es lo que la web ya
  hace para el geocode;
- si la cola está caída → **503** al toque, en vez de aceptar un archivo que
  nadie va a procesar.

`?wait=0` responde 202 siempre, para un cliente que prefiera seguirlo por SSE.

### 13.3 Qué pasa cuando algo se cae

| Situación | Qué hace el sistema |
|---|---|
| El worker muere a mitad de una tarea | Nadie ackeó: el broker la redeliverea. Otro nodo la retoma cuando vence la reserva de ejecución (con el default, hasta ~40 s) |
| Dos entregas del mismo job a la vez | El segundo ve la reserva tomada y difiere la tarea con demora. Nunca dos nodos escribiendo la misma salida |
| Falla pasajera (Redis, la api reiniciándose) | Se reintenta con demora creciente, hasta `MAX_REQUEUE_ATTEMPTS`; después va a la DLQ y el job queda `failed` con el motivo |
| Archivo corrupto | No se reintenta: el job queda `failed` con el error y la tarea se confirma. Reintentar un `.xlsx` roto solo ocupa un slot |
| Tarea colgada | Pasado `TASK_TIMEOUT_*_S` se aparta a la DLQ y el job deja de estar ocupado: el usuario ve el error en vez de un spinner eterno |
| `docker compose stop` de un worker | SIGTERM: deja de tomar tareas, termina lo que tiene en vuelo (hasta `SHUTDOWN_DRAIN_S`) y sale. Lo que no llegó a empezar lo toma otro |

Las tareas apartadas quedan en `{prefijo}-dlq` con el motivo escrito adentro y
un TTL de 24 h. Para mirarlas:

```bash
rabbitmqctl list_queues name messages | grep smart-import
```

**La reserva de ejecución y por qué no conviene bajarla a cualquier cosa.**
`SMART_IMPORT_RUN_LOCK_TTL_S` no es cuánto puede durar una tarea: mientras el
worker vive la renueva cada un tercio de ese tiempo, así que un geocode de una
hora la sostiene sin problema. Lo que mide es **cuánto se espera antes de dar
por muerto a un nodo que dejó de dar señales**, y de ahí sale cuánto tarda otro
en retomar su trabajo: el TTL más un tercio más, que es lo que tarda el que
espera en volver a preguntar.

Bajarlo acelera el failover y sube el riesgo de una muerte falsa. Si una pausa
de GC, un hipo de Redis o una laptop que se suspende dejan al worker sin
renovar más que el TTL, otro toma el job y **el mismo archivo se procesa dos
veces**: no se corrompe nada —gana el que termina último y la salida es la
misma— pero es trabajo tirado, y en un geocode largo se nota. Cuando pasa queda
en el log del que perdió, con el nombre de la variable adentro:

```
se perdio el lock de imp_abc123: otro nodo lo dio por muerto (sin renovar por
mas de 30s). Se va a procesar dos veces. Si se repite, subi SMART_IMPORT_RUN_LOCK_TTL_S
```

El piso es 10 s y el arranque avisa si pusiste menos, en vez de aplicarlo en
silencio. Como referencia: `15` con nodos estables en red local, el default
`30` para el caso normal, `45`–`60` si los workers son laptops o están detrás
de un túnel casero.

---

## 14. Diagnóstico rápido

| Síntoma | Qué mirar |
|---|---|
| Web 503 “unavailable :8100” | `docker compose ps`, `curl /health`, `SMART_IMPORT_URL` |
| `pbf_available: 0` | `ROUTE_OPTIMIZER_DATA` mal montado |
| Todo a pending_geocode | falta `depotLat`/`depotLng` en el request |
| Columna mezclada mal separada | fixtures en `examples/columna-mezclada/`; mirá `test_placeholders` |
| Geocode basura (nombre+tel en address) | faltó contexto de depot en geocode / query enrich |
| `python-multipart` en pytest | venv equivocado; usá `.venv` de este repo |
| Rebuild recompila libpostal | usaste `docker builder prune -a` o cambió la etapa `libpostal-build` |

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
- [ ] Pegar `examples/columna-mezclada/06_paste_ready.txt` o subir un CSV de columna mezclada
- [ ] Logs: `EXTRACT` con `por_reglas` (o modelo solo en filas libres) → `GEOCODE`
- [ ] Mapa: stops con coords y/o diálogo de geolocalización manual

---

## Resumen de oro

```bash
# Tests / CLI (venv de ESTE repo — no route-optimizer-env)
cd ~/workspace/vepathos-smart-import
source .venv/bin/activate
pytest -q

# Local Docker
cp deploy/templates/local-dev.env.template .env   # ROUTE_OPTIMIZER_DATA=...
DOCKER_BUILDKIT=1 docker compose build smart-import   # 1 vez: imagen + vocab + GeoNames
docker compose up -d smart-import                     # día a día SIN --build (entrypoint refresca sqlite)
curl -sf localhost:8100/health

# Limpiar y rebuild limpio
docker compose down
docker images 'vepathos/smart-import*' -q | xargs docker rmi -f 2>/dev/null
docker builder prune -f
DOCKER_BUILDKIT=1 docker compose build smart-import && docker compose up -d smart-import
```
