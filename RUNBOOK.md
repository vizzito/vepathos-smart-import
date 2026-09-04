# Runbook — Smart Import

Cómo correrlo local y en producción, y cómo probarlo **sin tocar la web**.

---

## Índice

1. [¿Tengo que subir un archivo por la web? No](#1-tengo-que-subir-un-archivo-por-la-web-no)
2. [Setup local](#2-setup-local)
3. [Nivel 1 — tests](#nivel-1--tests-30-segundos)
4. [Nivel 2 — demo del stack completo](#nivel-2--demo-del-stack-completo)
5. [Nivel 3 — CLI sobre tus propios archivos](#nivel-3--cli-sobre-tus-propios-archivos)
6. [Nivel 4 — servicio HTTP con curl](#nivel-4--servicio-http-con-curl)
7. [Nivel 5 — desde tu web](#nivel-5--desde-tu-web)
8. [Producción](#8-producción)
9. [Diagnóstico](#9-diagnóstico)

---

## 1. ¿Tengo que subir un archivo por la web? No

Hay **cinco niveles**, de menos a más. Cada uno prueba más superficie que el anterior y
la web es el último, no el primero.

| Nivel | Qué prueba | Necesita | Tarda |
|---|---|---|---|
| 1. `pytest` | toda la lógica: 113 tests | nada | 4 s |
| 2. `demo.sh` | el stack entero con logs | nada | 15 s |
| 3. CLI | tus archivos reales | tus archivos | segundos |
| 4. `curl` | el contrato HTTP | el servicio arriba | 1 min |
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

Para el geocoder, apuntá a los PBF que ya genera el cutter:

```bash
export SMART_IMPORT_PBF_DIR=~/workspace/route-optimizer-app/data/_extracts
```

---

## Nivel 1 — tests (30 segundos)

```bash
.venv/bin/python -m pytest tests/ -q
```

No necesita archivos, ni servicio, ni modelo, ni PBFs. Cubre lectura de los 6 formatos,
mapeo, normalización, agrupado, geocoding (con un índice sintético), la API y el
comportamiento cuando el modelo no está.

Para ver qué prueba cada uno:

```bash
.venv/bin/python -m pytest tests/ -v
.venv/bin/python -m pytest tests/test_normalize.py -v -k coordenadas
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

## Nivel 3 — CLI sobre tus propios archivos

Todos los comandos aceptan `-v` para el log paso a paso.

```bash
# ¿Qué hay dentro del archivo?
.venv/bin/python -m smart_import inspect mi_archivo.xlsx

# ¿Cómo mapea las columnas y con cuánta confianza?
.venv/bin/python -m smart_import detect -i mi_archivo.xlsx

# Convertir al formato Vepathos
.venv/bin/python -m smart_import -v normalize \
    -i mi_archivo.xlsx -o out/normalizado.csv --emit flat,nested --phone-region AR

# Geolocalizar (acción aparte)
.venv/bin/python -m smart_import -v geocode \
    -i out/normalizado.csv -o out/geocodificado.csv \
    --origin-lat -34.6037 --origin-lon -58.3816
```

**Corregir un mapping que salió mal:**

```bash
echo '{"Codigo interno": "reference", "Sucursal origen": null}' > mapping.json
.venv/bin/python -m smart_import normalize -i mi_archivo.xlsx \
    -o out/normalizado.csv --mapping mapping.json
```

**Diagnosticar fila por fila:**

```bash
.venv/bin/python -m smart_import normalize -i mi_archivo.xlsx \
    -o out/diag.csv --diagnostics       # agrega row_status y row_issues
```

---

## Nivel 4 — servicio HTTP con curl

```bash
export SMART_IMPORT_PBF_DIR=~/workspace/route-optimizer-app/data/_extracts
.venv/bin/python -m smart_import serve --port 8100
```

Escucha en **:8100**. Docs interactivas en <http://localhost:8100/docs>.
Los logs salen por stderr con el mismo formato del CLI.

```bash
# 1. ¿Qué puede hacer el servicio ahora?
curl -s localhost:8100/health | python -m json.tool

# 2. Subir y normalizar
curl -s -X POST "localhost:8100/imports?phone_region=AR" \
     -F "file=@mi_archivo.xlsx" | python -m json.tool

# 3. Estado (guardá el job_id del paso anterior)
JOB=imp_xxxxxxxx
curl -s localhost:8100/imports/$JOB | python -m json.tool

# 4. Muestra de filas para la UI
curl -s "localhost:8100/imports/$JOB/preview?limit=5" | python -m json.tool

# 5. Corregir el mapping
curl -s -X PUT localhost:8100/imports/$JOB/mapping \
     -H 'Content-Type: application/json' \
     -d '{"Codigo interno": "reference"}' | python -m json.tool

# 6. Descargar
curl -s "localhost:8100/imports/$JOB/download?format=flat"   -o normalizado.csv
curl -s "localhost:8100/imports/$JOB/download?format=nested" -o optimizador.json

# 7. ¿Hay cobertura antes de ofrecer el botón?
curl -s "localhost:8100/geocoding/coverage?lat=-34.60&lon=-58.38" | python -m json.tool

# 8. Geolocalizar (asíncrono)
curl -s -X POST "localhost:8100/imports/$JOB/geocode?origin_lat=-34.60&origin_lon=-58.38"
watch -n2 "curl -s localhost:8100/imports/$JOB | python -c \
    'import json,sys; d=json.load(sys.stdin); print(d[\"status\"], d.get(\"geocode\",{}).get(\"progress\"))'"

# 9. Separar columna compuesta con el modelo (requiere [ai])
curl -s -X POST "localhost:8100/imports/$JOB/extract?max_rows=200"
```

---

## Nivel 5 — desde tu web

El contrato para el cliente es corto:

```
POST /imports                  multipart  file=<archivo>
  -> { job_id, status, report{mapping, deliveries, needs_geocode}, next_actions[] }

GET  /imports/{id}             polling de estado
GET  /imports/{id}/preview     filas para la tabla de revisión
PUT  /imports/{id}/mapping     el usuario corrige un dropdown
GET  /imports/{id}/download?format=flat|nested|geocoded
POST /imports/{id}/geocode     el usuario aprieta "Geolocalizar"
```

**`next_actions` es el contrato clave.** Cada respuesta te dice qué puede hacer el
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
muestres — significa que no hay filas sin coordenadas.

Para desarrollo, CORS está en `*`. **Cerralo al dominio real antes de producción**
(`smart_import/api/app.py`, `allow_origins`).

---

## 8. Producción

Corre como **container independiente en la misma VM del cutter**, leyendo sus PBF en
read-only. No comparte proceso, código ni entorno con el cutter.

### 8.1 Preparar

```bash
ssh <vm-del-cutter>
cd ~ && git clone <repo> vepathos-smart-import && cd vepathos-smart-import
cp .env.example .env.prod
```

Editá `.env.prod`:

```bash
SMART_IMPORT_MAX_FILE_MB=10
SMART_IMPORT_MAX_ROWS=50000
SMART_IMPORT_AI_ENABLED=false          # prendelo sólo si el benchmark lo justifica
SMART_IMPORT_PBF_DIR=/data/pbf         # montado read-only, no tocar
SMART_IMPORT_INDEX_DIR=/data/indexes
GEOCODER_FALLBACK=none                 # nunca llama a un servicio externo solo
```

### 8.2 Levantar

```bash
export ROUTE_OPTIMIZER_DATA=/home/martin/route-optimizer-app/data   # donde está el cutter
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f smart-import
```

Queda como un tercer container en la VM:

```
VM 16 GB
├── route-optimizer-cutter    escribe  data/_extracts/*.osm.pbf
├── vepathos-worker
└── vepathos-smart-import     lee      /data/pbf  (:ro)     ← nuevo
                              escribe  /data/indexes, /data/cache
```

El `:ro` garantiza que no puede modificar ni borrar los extracts del cutter.

### 8.3 Verificar

```bash
curl -s localhost:8100/health | python3 -m json.tool
```

`geocoding.pbf_available` tiene que ser > 0. Si da 0, el mount está mal:

```bash
docker compose -f docker-compose.prod.yml exec smart-import ls /data/pbf | head
```

### 8.4 Pre-construir índices (opcional pero recomendado)

La primera geolocalización de una zona construye su índice (~10 s por cada 25 MB de
PBF) y el usuario espera. Para las zonas donde ya sabés que operan tus clientes,
adelantalo:

```bash
docker compose -f docker-compose.prod.yml exec smart-import \
  python -m smart_import build-geocoder-index --origin-lat -34.60 --origin-lon -58.38
```

### 8.5 Conectar routehub-fastapi

Smart Import corre en la red privada; **no lo expongas a internet**. routehub-fastapi
le hace proxy. Feature flag del lado de routehub:

```bash
SMART_IMPORT_ENABLED=false      # default: con esto apagado, todo sigue como hoy
SMART_IMPORT_URL=http://10.0.0.x:8100
```

Si Smart Import está caído, el flujo legacy de Vepathos funciona idéntico: no hay
ninguna dependencia en sentido inverso.

### 8.6 Recursos

Arranca conservador — comparte la VM con el cutter:

```yaml
mem_limit: 4g
cpus: 2
```

`normalize` usa 82 MB para 50k filas. Lo que consume memoria de verdad es construir un
índice de un PBF grande. Subilo cuando tengas medición, no antes.

---

## 9. Diagnóstico

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `pbf_available: 0` | el mount de `_extracts` está mal | `exec smart-import ls /data/pbf` |
| `sin cobertura PBF para el area` | no hay extract para esa zona | `list-pbf --lat X --lon Y`; el cutter todavía no cortó ahí |
| geocode tarda mucho la 1ª vez | está construyendo el índice | normal, ~10 s por 25 MB; queda cacheado |
| `needs_review` siempre | headers muy raros | mirá `detect`; corregí con `PUT /mapping` |
| todo `not_found` | falta el depot | pasá `--origin-lat/--origin-lon`: sin eso no hay desempate |
| `dependencies_installed: false` | falta torch | `pip install -e ".[ai]"` (solo si querés `extract`) |
| `extract` tarda muchísimo | 1,4 s por fila en CPU | bajá `SMART_IMPORT_EXTRACT_MAX_ROWS` |
| jobs que desaparecen | el store es en memoria | no uses `--workers > 1` todavía |

**Ver qué decidió y por qué:**

```bash
cat out/normalizado.report.json | python -m json.tool | head -40
```

Trae el mapping con confianza y método, los contadores por estado, los avisos y hasta
200 filas con su problema puntual.

**Logs con más detalle:**

```bash
.venv/bin/python -m smart_import -v normalize ...     # CLI
SMART_IMPORT_VERBOSE=1 .venv/bin/python -m smart_import serve   # servicio
```
