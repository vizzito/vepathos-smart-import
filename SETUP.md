# Guía de setup, deploy y operación — Smart Import

Guía práctica para instalar, correr en local, entender cada container, conectar la
web, deployar en prod, limpiar imágenes Docker y no reinstalar PyTorch de más.

Documentos relacionados:

| Doc | Para qué |
|---|---|
| **Este archivo (`SETUP.md`)** | Install, Docker, concurrencia y escala, local, prod, prune, comandos |
| [RUNBOOK.md](RUNBOOK.md) | Cómo **probar** el pipeline (pytest → demo → curl → web) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Diseño interno y gaps de producción |
| [examples/](examples/README.md) | Archivos de ejemplo (incl. columna-mezclada) |

---

## Índice

1. [Qué es este servicio (en una frase)](#1-qué-es-este-servicio-en-una-frase)
2. [Arquitectura de containers](#2-arquitectura-de-containers)
3. [Concurrencia: qué pasa con muchos usuarios a la vez](#3-concurrencia-qué-pasa-con-muchos-usuarios-a-la-vez)
4. [Requisitos](#4-requisitos)
5. [Setup local — opción A: Docker (recomendado)](#5-setup-local--opción-a-docker-recomendado)
6. [Setup local — opción B: venv + CLI](#6-setup-local--opción-b-venv--cli)
7. [Conectar la web (`vepathos-router-client`)](#7-conectar-la-web-vepathos-router-client)
8. [Flujo que ejecuta la web](#8-flujo-que-ejecuta-la-web)
9. [Comandos útiles (cheatsheet)](#9-comandos-útiles-cheatsheet)
10. [Tests](#10-tests)
11. [Docker: build, cache, prune (capas libpostal)](#11-docker-build-cache-prune-capas-libpostal)
12. [Deploy en producción](#12-deploy-en-producción)
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
FILE=examples/columna-mezclada/04_marketplace_whatsapp.csv
curl -sf -X POST "localhost:8100/imports?phone_region=AR" -F "file=@$FILE" | python3 -m json.tool

# --- CLI (venv) ---
.venv/bin/python -m smart_import detect --input "$FILE"
.venv/bin/python -m smart_import normalize --input "$FILE" --output out/n.csv --emit flat,nested
.venv/bin/python -m smart_import list-pbf --lat -34.6 --lon -58.4

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

Partí de la plantilla de prod, **no** de `.env.example` (esa es la local):

```bash
cp .env.prod.example .env
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

**Lo que un reinicio se lleva:** los jobs en vuelo, porque viven en memoria. Los
archivos ya descargados no se pierden; los imports a medio camino hay que
volver a subirlos. Es el gap #1 de ARCHITECTURE.md y la razón por la que varias
instancias necesitan balanceo sticky.

---

## 13. Variables de entorno

**La fuente de verdad son los dos `.env`**, y no por comodidad: ahí cada valor
lleva escrito de dónde sale (medición y script, o "sin medir" con el default del
código, para que se distinga un valor elegido de uno heredado).

- **`.env.example`** — local. `cp .env.example .env`
- **`.env.prod.example`** — producción. `cp .env.prod.example .env`

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
| El worker muere a mitad de una tarea | Nadie ackeó: el broker la redeliverea. Otro nodo la retoma cuando vence la reserva de ejecución, ~1-2 min |
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
# Local que importa
cp .env.example .env          # ROUTE_OPTIMIZER_DATA=...
DOCKER_BUILDKIT=1 docker compose build smart-import   # 1 vez: imagen + vocab + GeoNames
docker compose up -d smart-import                     # día a día SIN --build (entrypoint refresca sqlite)
curl -sf localhost:8100/health

# Limpiar y rebuild limpio
docker compose down
docker images 'vepathos/smart-import*' -q | xargs docker rmi -f 2>/dev/null
docker builder prune -f
DOCKER_BUILDKIT=1 docker compose build smart-import && docker compose up -d smart-import
```
