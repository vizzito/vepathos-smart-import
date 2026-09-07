# Runbook — Smart Import

Cómo correrlo local y en producción, y cómo probarlo **sin tocar la web**.

---

## Índice

1. [¿Tengo que subir un archivo por la web? No](#1-tengo-que-subir-un-archivo-por-la-web-no)
2. [Setup local](#2-setup-local)
3. [Nivel 1 — tests](#nivel-1--tests-30-segundos)
4. [Nivel 2 — demo del stack completo](#nivel-2--demo-del-stack-completo)
5. [Nivel 3 — CLI / ejemplos variables](#nivel-3--cli--ejemplos-variables)
6. [Nivel 4 — servicio HTTP con curl](#nivel-4--servicio-http-con-curl)
7. [Salida, IA y geocoding](#salida-ia-y-geocoding)
8. [Nivel 5 — desde tu web](#nivel-5--desde-tu-web)
9. [Producción](#8-producción)
10. [Diagnóstico](#9-diagnóstico)

> **Install / Docker / Rabbit / prune / prod:** guía operativa completa en
> **[SETUP.md](SETUP.md)**. Este runbook se centra en **cómo probar** el pipeline.

---

## 1. ¿Tengo que subir un archivo por la web? No

Hay **cinco niveles**, de menos a más. Cada uno prueba más superficie que el anterior y
la web es el último, no el primero.

| Nivel | Qué prueba | Necesita | Tarda |
|---|---|---|---|
| 1. `pytest` | toda la lógica: 113 tests | nada | 4 s |
| 2. `demo.sh` | el stack entero con logs | nada | 15 s |
| 3. CLI | archivos en `examples/` + tus archivos | archivos | segundos |
| 4. `curl` / `http-smoke.sh` | el contrato HTTP | el servicio arriba | 1 min |
| 5. tu web | la integración | todo lo anterior | — |

**Empezá por el 2.** Muestra cada etapa y cada decisión en pantalla.

---

## 2. Setup local

```bash
cd ~/workspace/vepathos-smart-import
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev,geo]"
```

`[dev]` trae tests y el servicio HTTP. `[geo]` trae pyosmium (el geocoder).
El modelo de IA es aparte y **opcional**:

```bash
.venv/bin/pip install -e ".[ai]"      # ~2,5 GB (torch + transformers)
```

Para el geocoder, apuntá a la **raíz** `data/` del route-optimizer (PBF de país
por región + `_extracts` chicos). No solo a `_extracts`:

```bash
unset SMART_IMPORT_PBF_DIR
export ROUTE_OPTIMIZER_DATA=~/workspace/route-optimizer-app/data
```

País sin extract (India, Japón, USA…): el slug tiene que estar en
`smart_import/resources/pbf_country_bounds.json`. Ver SETUP §6.1.

---

## Nivel 1 — tests (30 segundos)

```bash
.venv/bin/python -m pytest tests/ -q
```

No necesita archivos, ni servicio, ni PBFs. Cubre lectura, mapeo, normalización,
agrupado, geocoding sintético y la API.

```bash
.venv/bin/python -m pytest tests/test_vocab.py tests/test_mapping.py -q
.venv/bin/python -m pytest tests/test_geocode_accuracy.py -q   # sin PBF: solo detecta columnas
```

**Geocode vs coords reales (CABA, 13 + 2907):** hace falta la raíz `data/` del
route-optimizer, no `/workspace/.../_extracts`. Comandos y cómo leer el %:
[examples/geocode-truth/README.md](examples/geocode-truth/README.md).

```bash
unset SMART_IMPORT_PBF_DIR
export ROUTE_OPTIMIZER_DATA=/Users/martinvizzolini/workspace/route-optimizer-app/data

.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/caba_stops_2907.json

.venv/bin/python -m pytest -q -m real_geo tests/test_geocode_accuracy.py
```

---

## Nivel 2 — demo del stack completo

```bash
./scripts/demo.sh              # sin IA ni PBF
./scripts/demo.sh --geocode    # + geocoding contra un PBF real
./scripts/demo.sh --ai         # + separación de columna compuesta con el modelo
./scripts/demo.sh --all        # todo
```

Recorre las 10 etapas mostrando qué hace y por qué:

```
━━━ 2. NORMALIZE — archivo hostil -> formato Vepathos ━━━
  0.003s  READ       abriendo archivo=preamble_dirty.xlsx tamano=0.01MB
  0.063s  READ       formato=xlsx hoja=Datos header_row=4 filas=40 columnas=9
  0.063s                 3 hojas; se leyo 'Datos'
  0.063s                 4 fila(s) de preambulo descartadas antes del header
  0.063s  DETECT     resolviendo columnas contra schema=vepathos_flat_v1
  0.082s                 Dir. entrega    -> address        0.99 normalized
  0.082s                 Codigo interno  -> package_id     0.78 heuristic   <-- REVISAR
  0.082s                 Sucursal origen -> (sin mapear)
  0.082s  DETECT     mapeadas=8 sin_mapear=1 a_revisar=1 ia=no
  0.099s  NORMALIZE  con_coordenadas=40 necesitan_geocoding=0 invalidas=0
  0.099s  ASSEMBLE   entregas=40 bultos=40
  0.100s  DONE       normalize terminado total=0.0958s revisar=si
```

Con `--geocode` vas a ver la diferencia entre la primera corrida y la segunda:

```
  paso 8b: geocode (el usuario lo pide explicitamente)
      Grensen 5, Oslo             matched        1.00 housenumber  59.91375,10.74386  (indice)
      Karl Johans gate 1, Oslo    low_confidence 0.80 street       59.91181,10.74646  (indice)
      Calle Inexistente 999       not_found      0.13              -                  (indice)
      cache hits=0 misses=4 hit_rate=0%     t=0.149s

━━━ 9. CACHE — la MISMA consulta, la segunda vez ━━━
      cache hits=4 misses=0 hit_rate=100%   t=0.001s        ← 150x
```

---

## Nivel 3 — CLI / ejemplos variables

Los casos variables para probar a mano viven en [`examples/`](examples/README.md)
(headers raros, sin coords, latin1, preamble, lat/lng invertidas, columna mezclada, etc.).

```bash
.venv/bin/python -m smart_import make-fixtures --out examples --rows 40
```

Todos los comandos aceptan `-v`. **Corré desde la raíz del repo** (o usá rutas absolutas):
si el archivo no existe, `curl`/`normalize` fallan con un error confuso.

```bash
FILE=examples/es_sin_coords.csv     # o tu archivo real

.venv/bin/python -m smart_import inspect "$FILE"
.venv/bin/python -m smart_import detect -i "$FILE"

# Convertir al formato Vepathos (schema: vepathos_flat_v1)
.venv/bin/python -m smart_import -v normalize \
    -i "$FILE" -o out/normalizado.csv --emit flat,nested --phone-region AR

# Geolocalizar (acción aparte; necesita SMART_IMPORT_PBF_DIR)
.venv/bin/python -m smart_import -v geocode \
    -i out/normalizado.csv -o out/geocodificado.csv \
    --origin-lat -34.6037 --origin-lon -58.3816
```

**Corregir un mapping** (ejemplo real para `examples/preamble_dirty.xlsx`):

```bash
echo '{"Codigo interno": "reference", "Sucursal origen": null}' > mapping.json
.venv/bin/python -m smart_import normalize -i examples/preamble_dirty.xlsx \
    -o out/normalizado.csv --mapping mapping.json
```

**Diagnosticar fila por fila:**

```bash
.venv/bin/python -m smart_import normalize -i "$FILE" \
    -o out/diag.csv --diagnostics       # agrega row_status y row_issues
```

---

## Nivel 4 — servicio HTTP con curl

```bash
cd ~/workspace/vepathos-smart-import
export ROUTE_OPTIMIZER_DATA=~/workspace/route-optimizer-app/data
.venv/bin/python -m smart_import serve --port 8100
```

Escucha en **:8100**. Docs: <http://localhost:8100/docs>.
Los logs salen por stderr con el mismo formato del CLI.

### Atajo (recomendado)

Captura el `job_id` de verdad, hace preview + download, y opcionalmente geocode:

```bash
./scripts/http-smoke.sh
./scripts/http-smoke.sh --geocode examples/es_sin_coords.csv
./scripts/http-smoke.sh examples/merged_field.csv
```

### A mano (paso a paso)

Importante:

1. Estás en la **raíz del repo** (o usá ruta absoluta al archivo).
2. **No** pongas `JOB=imp_xxxxxxxx` — eso era un placeholder. Capturá el id real.
3. macOS no trae `watch`; usá el loop de abajo.
4. Preferí `curl -sf` (falla ruidoso si el servicio/archivo no existen).

```bash
# 1. ¿Qué puede hacer el servicio ahora?
curl -sf localhost:8100/health | .venv/bin/python -m json.tool
# Mirá: ai.enabled / ai.dependencies_installed / geocoding.pbf_available

# 2. Subir y normalizar — GUARDÁ la respuesta
FILE=examples/es_sin_coords.csv
[ -f "$FILE" ] || .venv/bin/python -m smart_import make-fixtures --out examples --rows 40

RESP=$(curl -sf -X POST "localhost:8100/imports?phone_region=AR&timezone=America/Argentina/Buenos_Aires" -F "file=@${FILE}")
echo "$RESP" | .venv/bin/python -m json.tool

# 3. Capturar el job_id REAL
JOB=$(echo "$RESP" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')
echo "JOB=$JOB"

curl -sf "localhost:8100/imports/$JOB" | .venv/bin/python -m json.tool

# 4. Muestra de filas para la UI
curl -sf "localhost:8100/imports/$JOB/preview?limit=5" | .venv/bin/python -m json.tool

# 5. Corregir mapping (solo si next_actions trae "mapping" / needs_review)
#    Ejemplo válido para preamble_dirty.xlsx (no para one_row_per_delivery):
# curl -sf -X PUT "localhost:8100/imports/$JOB/mapping" \
#      -H 'Content-Type: application/json' \
#      -d '{"Codigo interno": "reference"}' | .venv/bin/python -m json.tool

# 6. Descargar — salidas estándar Vepathos
mkdir -p out
curl -sf "localhost:8100/imports/$JOB/download?format=flat"   -o out/normalizado.csv
curl -sf "localhost:8100/imports/$JOB/download?format=nested" -o out/optimizador.json

# 7. ¿Hay cobertura PBF antes de ofrecer el botón?
curl -sf "localhost:8100/geocoding/coverage?lat=-34.60&lon=-58.38" | .venv/bin/python -m json.tool

# 8. Geolocalizar (async). Solo si next_actions incluye "geocode"
curl -sf -X POST "localhost:8100/imports/$JOB/geocode?origin_lat=-34.60&origin_lon=-58.38" \
  | .venv/bin/python -m json.tool

# Polling portable (sin `watch`):
while true; do
  curl -sf "localhost:8100/imports/$JOB" | .venv/bin/python -c \
    'import json,sys; d=json.load(sys.stdin); g=d.get("geocode") or {}; print(d["status"], g.get("progress"))'
  sleep 2
done
# Ctrl-C cuando status=geocoded / phase=done

# 9. Separar columna compuesta (requiere AI + archivo tipo merged_field)
# FILE=examples/merged_field.csv   # re-importá ese archivo primero → nuevo JOB
# curl -sf -X POST "localhost:8100/imports/$JOB/extract?max_rows=200" | .venv/bin/python -m json.tool
```

Si `python -m json.tool` dice `Expecting value: line 1 column 1`, casi siempre es:
(a) servicio caído, (b) el path de `-F file=@…` no existe desde tu cwd, o
(c) usaste `-s` y curl falló en silencio — usá `-sf`.

Si ves `job 'imp_xxxxxxxx' inexistente`, copiaste el placeholder del doc: capturá
`$JOB` desde la respuesta del `POST /imports`.

---

## Salida, IA y geocoding

### Salida canónica = `vepathos_flat_v1`

Definida en [`schemas/vepathos_flat_v1.json`](schemas/vepathos_flat_v1.json).

| Formato | Qué es | Quién lo consume |
|---|---|---|
| **flat** (CSV/XLSX) | Una fila por `delivery × package`, columnas en orden del schema | UI, export, auditoría |
| **nested** (JSON) | `{"addresses":[{..., "packages":[...]}]}` | optimizador |
| **report** (`*.report.json`) | mapping + confianza + contadores + avisos | depuración / UI |

Solo se emiten las columnas que existían (o se pudieron mapear) en la entrada.
Un archivo que ya viene en formato Vepathos sale idéntico (round-trip).

```bash
curl -sf localhost:8100/schemas | .venv/bin/python -m json.tool
```

### ¿Está corriendo el modelo?

**No en `normalize`.** El mapeo de columnas es 100 % reglas. El modelo
(NuExtract-1.5-tiny) **solo** corre en `extract` (separar una columna compuesta).

Cómo saberlo:

```bash
curl -sf localhost:8100/health | .venv/bin/python -m json.tool
```

| Campo en `/health` o en el job | Significado |
|---|---|
| `ai.enabled` | Flag `SMART_IMPORT_AI_ENABLED` |
| `ai.dependencies_installed` | torch/transformers instalados |
| `ai.model` / `ai.device` | Qué cargaría y dónde (`cpu` / `mps` / `cuda`) |
| `report.ai_used == false` | Normal en imports: el mapping **no** usó IA |
| `extract` en `next_actions` | El servicio ofrece separación con modelo |
| Logs `POST /extract` / stage EXTRACT | Ahí sí se cargó/usó el modelo |

Tu `/health` con `enabled=true` + `dependencies_installed=true` significa “listo para
`extract`”, **no** que el modelo esté corriendo en cada import.

### ¿Cómo funciona la geocodificación y dónde corre?

Corre **local, dentro del proceso** `smart_import serve` (un worker en thread pool).
No es un microservicio aparte y, con `GEOCODER_FALLBACK=none`, **no llama** a
Nominatim/Google ni a nada externo.

```
PBF de país (india-pyrosm.osm.pbf, …)  +  extracts chicos (_extracts/…)
        │  ROUTE_OPTIMIZER_DATA  (raíz data/; no solo _extracts)
        ▼
build-geocoder-index  →  data/indexes/<key>.sqlite   (una vez por archivo)
        │
        ▼
POST /imports/{id}/geocode   →  lee SQLite (FTS5 + RTree) + cache local
        │
        ▼
CSV con lat/lng + geocode_status / confidence / precision
```

- **Dónde:** misma máquina/VM que el servicio (`:8100`). Lee los PBF del cutter
  read-only; escribe índices/cache en `data/indexes` y `data/cache`.
- **Cuándo:** solo si llamás `POST …/geocode`. Nunca automático
  (`geocoding.automatic: false` en `/health`).
- **Qué PBF elige:** extract más chico que cubra el punto; si no hay, corta
  uno desde el PBF de país/región (`osmium extract` → `data/extracts/`) y
  indexa ese recorte. Nunca construye `argentina.sqlite` / `florida.sqlite`
  para una ciudad. Si falta `osmium-tool`, error claro (no fallback silencioso).
- **Estados de fila:** `already_geocoded` | `matched` | `low_confidence` |
  `not_found` | `error`.

---

## Nivel 5 — desde tu web

### Contrato de estado (listo para conectar)

Cada respuesta de job trae lo que la UI necesita para barra de progreso y botones:

```jsonc
{
  "job_id": "imp_ab12",
  "status": "geocoding",          // o extracting | normalized | …
  "busy": true,                   // true = no ofrecer acciones; mostrar progreso
  "progress": {
    "phase": "geocoding",         // queued | loading_model | building_index | extracting | …
    "message": "Geolocalizando 120/1000",
    "done": 120,
    "total": 1000,
    "pct": 12.0,
    "eta_s": 45.2,
    "busy": true,
    "detail": {"matched": 90, "not_found": 10}
  },
  "poll_after_ms": 500,           // null cuando busy=false
  "urls": {
    "self": "/imports/imp_ab12",
    "events": "/imports/imp_ab12/events",   // SSE
    "preview": "/imports/imp_ab12/preview",
    "download_flat": "/imports/imp_ab12/download?format=flat",
    "download_nested": "/imports/imp_ab12/download?format=nested"
  },
  "next_actions": [ /* vacio si busy */ ],
  "report": { "deliveries": 1000, "needs_geocode": 800, "ai_used": false }
}
```

**Dos formas de seguir el avance** (elige una):

```js
// A) Polling (simple)
async function watch(jobId) {
  for (;;) {
    const j = await fetch(`${SI}/imports/${jobId}`).then(r => r.json())
    setProgress(j.progress)
    if (!j.busy) { setJob(j); return j }
    await sleep(j.poll_after_ms ?? 500)
  }
}

// B) SSE (menos requests; ideal para 1k filas + modelo/geocode)
const es = new EventSource(`${SI}/imports/${jobId}/events`)
es.addEventListener('progress', e => setProgress(JSON.parse(e.data)))
es.addEventListener('done', e => { setJob(JSON.parse(e.data)); es.close() })
```

Payload chico si solo querés la barra: `GET /imports/{id}/progress`.

### Flujo de la UI

```
POST /imports                  multipart  file=<archivo>   → sync (~ms–s)
  -> { job_id, status, busy:false, progress, report, next_actions, urls }

GET  /imports/{id}             polling de estado
GET  /imports/{id}/progress    solo barra
GET  /imports/{id}/events      SSE (progress + done)
GET  /imports/{id}/preview     filas para la tabla de revisión
PUT  /imports/{id}/mapping     el usuario corrige un dropdown
GET  /imports/{id}/download?format=flat|nested|geocoded

POST /imports/{id}/geocode     → 202 + busy=true  (async; 1000 dirs OK)
POST /imports/{id}/extract     → 202 + busy=true  (async; modelo ~1.4s/fila)
```

`normalize` es síncrono (50k filas ~1.5 s). Lo que tarda de verdad — **geocode** y
**extract** — es async: la web arranca, se suscribe a `events` / pollea, y cuando
`busy=false` pinta `next_actions`.

**`next_actions` es el contrato de botones.** Cada respuesta te dice qué puede hacer el
usuario ahora y con qué link; tu UI no necesita replicar la máquina de estados:

```jsonc
"next_actions": [
  {"action": "download", "href": "/imports/imp_ab12/download?format=flat",
   "description": "Descargar el archivo normalizado (formato Vepathos)"},
  {"action": "geocode",  "href": "/imports/imp_ab12/geocode",
   "description": "Geolocalizar 40 fila(s) sin coordenadas. NO se ejecuta solo."}
]
```

Pintá un botón por cada acción que venga. Si `geocode` no está en la lista, no lo
muestres — significa que no hay filas sin coordenadas. Mientras `busy=true`,
`next_actions` viene vacío a propósito.

Para desarrollo, CORS está en `*` (`SMART_IMPORT_CORS_ORIGINS`).
**Cerralo al dominio real antes de producción.**

Estados útiles:

| status | busy | Qué mostrar |
|---|---|---|
| `normalized` / `needs_mapping_review` | no | preview + botones |
| `extracting` | sí | barra “Extrayendo N/M” + eta |
| `geocode_queued` / `geocoding` | sí | barra geo (+ “building_index” la 1ª vez) |
| `completed` | no | download geocoded |
| `failed` | no | `error` + reintentar |

---

## 8. Producción

Un solo `docker compose`, configurado por `.env`. Ver [ARCHITECTURE.md](ARCHITECTURE.md)
para el diagrama completo y qué falta resolver.

### 8.1 Elegir qué corre

```bash
cp .env.example .env
```

El núcleo (`normalize`) siempre está. Las otras dos se prenden y apagan solas:

| Quiero… | `.env` | Imagen |
|---|---|---|
| **Prod (recomendado)** | `TARGET=runtime-libpostal` `LIBPOSTAL_ENABLED=true` `GEOCODING_ENABLED=true` | ~2.4 GB |
| Sin libpostal | `TARGET=runtime` `LIBPOSTAL_ENABLED=false` | ~370 MB |
| Sólo normalizar | `GEOCODING_ENABLED=false` `TARGET=runtime` | ~370 MB |

`SMART_IMPORT_TARGET` es el **build stage** del Dockerfile (`runtime` o
`runtime-libpostal`), no un modo de proceso. Ver [docs/libpostal.md](docs/libpostal.md).

Checklist prod libpostal:

```bash
SMART_IMPORT_TARGET=runtime-libpostal
SMART_IMPORT_LIBPOSTAL_ENABLED=true
docker compose build smart-import
docker compose up -d --force-recreate smart-import
curl -sf localhost:8100/health | python -m json.tool
# capabilities.libpostal == true, extraction.address_parser == enhanced
```

Lo único que **sí** hay que ajustar sí o sí es dónde están los PBFs del cutter **en el
host**:

```bash
ROUTE_OPTIMIZER_DATA=/home/martin/route-optimizer-app/data
```

El compose lo monta read-only en `/data/pbf`. Las rutas internas del container las fija
el compose, no el `.env`: no las toques.

> **Espacio en disco.** La imagen `ai` pesa **3,34 GB** (torch) contra 370 MB la
> `runtime`. Sumale ~1 GB del modelo en el volumen. Verificá antes de construir:
>
> ```bash
> df -h /System/Volumes/Data     # macOS
> df -h /var/lib/docker          # Linux
> docker system df               # cuánto ocupa Docker hoy
> ```
>
> Para liberar espacio **de este proyecto solamente** (no toca otros):
>
> ```bash
> docker compose down --rmi local -v
> ```
>
> Evitá `docker system prune -a --volumes`: borra imágenes y volúmenes de **todos**
> tus proyectos, no sólo de este.

### 8.2 Levantar

```bash
docker compose up -d
docker compose logs -f smart-import
```

Los logs de arranque te dicen qué quedó activo:

```
0.023s  HTTP   arrancando Smart Import version=0.1.0 puerto=8100 work_dir=/data/jobs
0.023s  HTTP   capacidades normalize=on geocoding=on ia=off
```

Con la imagen `ai`, conviene bajar el modelo **antes** de empezar a servir, para que el
primer usuario no espere ~60 s:

```bash
docker compose --profile warmup up warmup      # baja el modelo al volumen
docker compose up -d                           # después levanta el servicio
```

### 8.3 Verificar

```bash
curl -s localhost:8100/health | python3 -m json.tool
curl -s localhost:8100/config | python3 -m json.tool   # config efectiva del proceso
```

`capabilities` tiene que decir lo que esperás, y si el geocoding está prendido,
`geocoding.pbf_available` tiene que ser > 0. Si da 0, el mount está mal:

```bash
docker compose exec smart-import ls /data/pbf | head
```

### 8.4 Pre-construir índices (recomendado)

La primera geolocalización de una zona construye su índice (~10 s por cada 25 MB de PBF)
y el usuario espera. Para las zonas donde ya sabés que operan tus clientes, adelantalo:

```bash
docker compose --profile tools run --rm tools \
    build-geocoder-index --origin-lat -34.60 --origin-lon -58.38
```

`tools` comparte los volúmenes del servicio, así que el índice queda listo para él.
Otros comandos útiles:

```bash
docker compose --profile tools run --rm tools list-pbf --lat -34.6 --lon -58.4
docker compose --profile tools run --rm tools warmup
```

### 8.4.1 Medir recall por zona (no bajar umbrales)

CABA 2907: ~12% pin con altura. El techo es cobertura OSM. Cómo apuntar PBF,
correr las 2907, pytest y agregar otra zona:

[examples/geocode-truth/README.md](examples/geocode-truth/README.md)

```bash
unset SMART_IMPORT_PBF_DIR
export ROUTE_OPTIMIZER_DATA=/Users/martinvizzolini/workspace/route-optimizer-app/data

.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/caba_stops_2907.json --out /tmp/caba_2907.json

.venv/bin/python -m pytest -q -m real_geo tests/test_geocode_accuracy.py
```

No bajes `GEOCODE_*` thresholds para inflar pines.

### 8.5 Conectar routehub-fastapi

Smart Import **no tiene auth ni multi-tenancy**: no lo expongas a internet. routehub le
hace de puerta, que ya resuelve API key y tenant.

```bash
# en routehub-fastapi
SMART_IMPORT_ENABLED=false                  # default: con esto todo sigue como hoy
SMART_IMPORT_URL=http://10.0.0.x:8100
```

Y cerrá CORS al dominio real (checklist de deploy — no hardcodear en codigo):

```bash
# un origin, o varios separados por coma (sin espacios obligatorios)
SMART_IMPORT_CORS_ORIGINS=https://app.vepathos.com
# SMART_IMPORT_CORS_ORIGINS=https://app.vepathos.com,https://admin.vepathos.com
```

- `*` solo en local / desarrollo.
- No commitear el dominio de prod en el repo; va en el `.env` del host.
- Si el browser no habla con `:8100` (solo RouteHub server-side), CORS es menos critico, pero igual no dejes `*` en un puerto expuesto.

Si Smart Import está caído, el flujo legacy funciona idéntico: no hay dependencia en
sentido inverso.

### 8.6 Recursos

Arranca conservador — comparte la VM con el cutter:

```bash
SMART_IMPORT_MEMORY_LIMIT=4g
SMART_IMPORT_CPUS=2
SMART_IMPORT_GEOCODE_WORKERS=1
SMART_IMPORT_EXTRACT_WORKERS=1
```

`normalize` usa 82 MB para 50k filas. Lo que consume memoria de verdad es construir un
índice de un PBF grande, y cargar el modelo (~1 GB residente). Subí los workers cuando
tengas medición, no antes.

## 9. Diagnóstico

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `Expecting value: line 1 column 1` | servicio caído o `file=@…` inexistente desde tu cwd | `curl -sf …/health`; `ls` el archivo; corré desde la raíz del repo |
| `job 'imp_xxxxxxxx' inexistente` | usaste el placeholder del doc | capturá `$JOB` del `POST /imports` |
| `watch: command not found` | macOS no trae `watch` | usá el `while true; do …; sleep 2; done` o `./scripts/http-smoke.sh --geocode` |
| `pbf_available: 0` | el mount / `SMART_IMPORT_PBF_DIR` está mal | `list-pbf` o `ls $SMART_IMPORT_PBF_DIR` |
| `sin cobertura PBF para el area` | no hay PBF de país/región que cubra el depot | `list-pbf --lat X --lon Y`; el slug tiene que estar en `pbf_country_bounds.json` |
| `falta osmium-tool para cortar el extract` | no hay extract y no está osmium | `brew install osmium-tool` (local) o rebuild de la imagen |
| geocode tarda mucho la 1ª vez | está construyendo el índice | normal, ~10 s por 25 MB; queda cacheado |
| `needs_review` siempre | headers muy raros | mirá `detect`; corregí con `PUT /mapping` |
| todo `not_found` | falta el depot | pasá `--origin-lat/--origin-lon`: sin eso no hay desempate |
| `dependencies_installed: false` | falta torch | `pip install -e ".[ai]"` (solo si querés `extract`) |
| `ai.enabled=true` pero `ai_used=false` | esperado en normalize | el modelo solo corre en `extract` |
| `extract` tarda muchísimo | 1,4 s por fila en CPU | bajá `SMART_IMPORT_EXTRACT_MAX_ROWS` |
| jobs que desaparecen | el store es en memoria | no uses `--workers > 1` todavía |
| mejoré el geocoder y sigue fallando igual | el cache guarda también los `not_found` | `rm data/cache/geocode_cache.sqlite` (o el volumen `smart_import_cache`) |
| `503` al geocodificar | capacidad apagada | el mensaje dice qué env var prender |
| `Permission denied` en `/data/...` | volumen creado por root | el path tiene que existir en la imagen; ver Dockerfile |
| el botón de geocode no aparece | `pbf_dir` vacío o capacidad off | `curl /health` → `capabilities` |
| `.env` que no se aplica | está montado pero no leído | `curl /config` muestra la config efectiva |

**Ver qué decidió y por qué:**

```bash
.venv/bin/python -m json.tool out/normalizado.report.json | head -40
```

Trae el mapping con confianza y método, los contadores por estado, los avisos y hasta
200 filas con su problema puntual.

**Logs con más detalle:**

```bash
.venv/bin/python -m smart_import -v normalize ...     # CLI
SMART_IMPORT_VERBOSE=1 .venv/bin/python -m smart_import serve   # servicio
```
