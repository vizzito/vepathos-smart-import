# Corpus de test, generación por país y medición

Guía práctica: **qué script usar**, **cómo generar archivos** y **cómo medir**
sin confundir verdad (ground truth) con coords de input del producto.

## Mapa rápido de herramientas

| Herramienta | Qué hace | ¿Usa cola/API? | Coords del archivo |
|-------------|----------|----------------|--------------------|
| `scripts/generate_address_corpus.py` | Genera JSON/CSV con direcciones + lat/lng verificados | No | **Verdad** (OA o fixture) |
| `python -m smart_import geocode-accuracy` | Geocodifica por `address`, mide metros vs lat/lng | No (CLI embebido) | **Solo validación** |
| `python -m smart_import geocode-eval` | Igual idea, más simple; requiere `--index` explícito | No | **Solo validación** |
| `POST /imports` + `/geocode` (Docker :8100) | Flujo producto (normalize → geocode) | Opcional (rol `api`+worker) | **Input del cliente** si hay lat/lng |
| `pytest -m geo_trap` | Regresión congelada (homónimos CABA, etc.) | No | Índice sintético o real |
| `pytest -m real_geo` | Truth files históricos del repo | No | Verdad |

**Regla de oro:** si `lat`/`lng` están en columnas del schema Vepathos, la API asume
que **ya vienen geocodificadas** y no vuelve a geocodificar. Para benchmark usá
`geocode-accuracy`, no upload directo del CSV con coords.

---

## Prerrequisitos

### OpenAddresses (direcciones reales por país)

```bash
# https://batch.openaddresses.io/login → Profile → Create API token
export OPENADDRESSES_TOKEN=oa.xxxx
# o en .env del repo (el script lo lee solo)
```

Cache local: `data/openaddresses/*.geojson.gz` (reutilizable).

### Geocoder (medir error en metros)

```bash
unset SMART_IMPORT_PBF_DIR
export ROUTE_OPTIMIZER_DATA=/Users/TU_USUARIO/workspace/route-optimizer-app/data
find "$ROUTE_OPTIMIZER_DATA" -name '*.osm.pbf' | head
```

La primera corrida de `geocode-accuracy` puede construir el índice SQLite.

---

## Presets por país

Listar todos:

```bash
python scripts/generate_address_corpus.py --list-presets
```

### Pack `languages` — ~30 países (100 dirs c/u)

Idiomas del parser (`es`, `en`, `pt`, `fr`, `it`, `de`, `nl`, `pl`) + extras OA
(Japón, EAU, Nordics). **OpenAddresses no tiene** GB, IE, IN, PE.

```bash
# Un archivo por país
N=100 && python scripts/generate_address_corpus.py --pack languages -n $N

# Un solo archivo mezclado (barajado; cada fila trae country + preset)
N=100 && python scripts/generate_address_corpus.py --pack languages --mix -n $N \
  --out "examples/geocode-truth/generated/languages_mix_n${N}.json"
# → languages_mix_n100_s42.json (el seed va siempre en el nombre)

# Otra muestra: 100 dirs distintas por país (imprime seed=…; repetí con --seed N)
N=100 && python scripts/generate_address_corpus.py --pack languages --mix --new-sample -n $N

# Mix + ruido humano (misma lat/lng; archivo `*_noise5_s42.json`, no pisa el limpio)
N=100 && python scripts/generate_address_corpus.py --pack languages --mix -n $N \
  --noise-level 5 --noise-baseline --noise-permutations 4 \
  --out "examples/geocode-truth/generated/languages_mix_n${N}.json"
```

Sin `--new-sample` el seed es **42**: misma corrida, mismas 100. Con `--new-sample`
sale `languages_mix_n100_s184729.json` (el seed va **siempre** en el nombre). El GeoJSON queda en
cache: no vuelve a bajar el país, solo remuestrea. `--noise-level` se aplica **por país**
antes de barajar; el nombre queda `languages_mix_n100_noise5_s42.json`.

`--mix` solo implica `--pack languages`. Si una fuente falla, **sigue**
y mezcla las que salieron. Un depot único no aplica a este archivo.

Un solo país, mismo formato:

```bash
N=100 && python scripts/generate_address_corpus.py --preset france -n $N --country FR \
  --out "examples/geocode-truth/generated/france_n${N}.json"
```

| Preset | Fuente OA | País | Depot al medir | Notas |
|--------|-----------|------|----------------|-------|
| `argentina` | `ar/c/city_of_buenos_aires` | AR | CABA / `-34.598, -58.416` | ~560k dirs. Fixture local 15 dirs sin token |
| `france` | `fr/75/statewide` | FR | Paris / `48.8566, 2.3522` | París / dept. 75 |
| `spain` | `es/25829` | ES | Madrid / `40.4168, -3.7038` | Regional (evita countrywide 400+ MB) |
| `mexico` | `mx/jal/statewide` | MX | Guadalajara / `20.6597, -103.3496` | Jalisco |
| `us_california` | `us/ca/san_francisco` | US | San Francisco / `37.7749, -122.4194` | San Francisco city |

El generador imprime el `geocode-accuracy` con **ese** depot (no hardcodea CABA).
Cambiá `N` y el filename sigue `france_n${N}.json`.

---

## Casos de generación

### 1. Argentina — fixture rápido (sin token, 15 dirs)

Ideal para CI y traps. Máximo 15 filas aunque pidás `-n 1000`.

```bash
python scripts/generate_address_corpus.py \
  --preset argentina --fixture \
  -n 15 --seed 42 \
  --out examples/geocode-truth/generated/argentina_fixture_n15.json
```

Fuente: `sources/oa_caba_sample.csv` (curado, coords verificadas).

### 2. Argentina — OpenAddresses

```bash
N=5000 && python scripts/generate_address_corpus.py --preset argentina -n $N --country AR \
  --out "examples/geocode-truth/generated/argentina_n${N}.json"
```

OA CABA suele traer **calle + altura + coords** sin `city` en el dataset.
Usar `--depot-city CABA` al medir.

### 3. Argentina — mismos N + ruido humano nivel 5

Misma verdad lat/lng; cambia solo el texto (`address`). ~7 filas por dirección base.

```bash
N=1000 && python scripts/generate_address_corpus.py --preset argentina -n $N --country AR \
  --noise-level 5 --noise-baseline --noise-permutations 4 \
  --out "examples/geocode-truth/generated/argentina_n${N}_noise5.json"
```

Con `N=1000`: **7000 filas** (1000 baseline `noise_level=0` + 6000 variantes).

Columnas útiles en CSV:

| Columna | Uso |
|---------|-----|
| `address` | Input ruidoso al parser/geocoder |
| `address_clean` | Dirección canónica original |
| `noise_level` | 0=limpia, 1–5=ruido |
| `noise_variant` | Índice de permutación |
| `lat`, `lng` | Ground truth (no input en API) |

### 4. España

```bash
N=5000 && python scripts/generate_address_corpus.py --preset spain -n $N --country ES \
  --out "examples/geocode-truth/generated/spain_n${N}.json"
```

Con ruido:

```bash
N=1000 && python scripts/generate_address_corpus.py --preset spain -n $N --country ES \
  --noise-level 5 --noise-baseline --noise-permutations 4 \
  --out "examples/geocode-truth/generated/spain_n${N}_noise5.json"
```

Medir (el generador imprime estos flags; no uses CABA):

```bash
N=1000 && .venv/bin/python -m smart_import geocode-accuracy \
  --truth "examples/geocode-truth/generated/spain_n${N}_noise5.json" \
  --depot-city Madrid --depot-country España \
  --origin-lat 40.4168 --origin-lon -3.7038 \
  --enhance --limit 100
```

### 5. Francia / México / San Francisco

Mismo patrón: `N` en el comando y en el filename.

```bash
N=5000 && python scripts/generate_address_corpus.py --preset france -n $N --country FR \
  --out "examples/geocode-truth/generated/france_n${N}.json"

N=5000 && python scripts/generate_address_corpus.py --preset mexico -n $N --country MX \
  --out "examples/geocode-truth/generated/mexico_n${N}.json"

N=5000 && python scripts/generate_address_corpus.py --preset us_california -n $N --country US \
  --out "examples/geocode-truth/generated/usa_ca_n${N}.json"
```

Ruido (Francia):

```bash
N=1000 && python scripts/generate_address_corpus.py --preset france -n $N --country FR \
  --noise-level 5 --noise-baseline --noise-permutations 4 \
  --out "examples/geocode-truth/generated/france_n${N}_noise5.json"
```

Medir Francia (depot **Paris**, no CABA):

```bash
N=5000 && .venv/bin/python -m smart_import geocode-accuracy \
  --truth "examples/geocode-truth/generated/france_n${N}.json" \
  --depot-city Paris --depot-country France \
  --origin-lat 48.8566 --origin-lon 2.3522 \
  --enhance --limit 500
```

México / San Francisco:

```bash
N=5000 && .venv/bin/python -m smart_import geocode-accuracy \
  --truth "examples/geocode-truth/generated/mexico_n${N}.json" \
  --depot-city Guadalajara --depot-country México \
  --origin-lat 20.6597 --origin-lon -103.3496 \
  --enhance --limit 500

N=5000 && .venv/bin/python -m smart_import geocode-accuracy \
  --truth "examples/geocode-truth/generated/usa_ca_n${N}.json" \
  --depot-city "San Francisco" --depot-country "United States" \
  --origin-lat 37.7749 --origin-lon -122.4194 \
  --enhance --limit 500
```

### 6. Estilos de redacción (misma fila OA, varios formatos)

```bash
python scripts/generate_address_corpus.py \
  --preset argentina --fixture -n 10 \
  --styles oa_default,us,eu,minimal,full
```

| Estilo | Ejemplo |
|--------|---------|
| `oa_default` | `Av. Corrientes, 1234, C1043` |
| `us` | `1234 Av. Corrientes` |
| `eu` | `Av. Corrientes 1234, C1043 Ciudad` |
| `minimal` | `Av. Corrientes 1234, CABA` |
| `full` | + depto, región |

### 7. Ruido — niveles y flags

| Flag | Default | Significado |
|------|---------|-------------|
| `--noise-level {1-6}` | — | Intensidad del desorden |
| `--noise-cumulative` | off | Genera niveles 1..N (no solo N) |
| `--noise-baseline` | off | Incluye fila limpia (`noise_level=0`) |
| `--noise-permutations` | 4 | Máx. permutaciones por nivel (3–5) |
| `--noise-rate` | 1.0 | Fracción de filas base a expandir |

| Nivel | Simula |
|-------|--------|
| 1 | Solo calle + altura |
| 2 | + CP o depto |
| 3 | Ciudad/país, permutaciones |
| 4 | Typos y puntuación rota |
| 5 | Bloques casi completos permutados |
| 6 | Planilla de despacho real: apellido solo, truncada, typo fonético, número pegado, nota al final, nombre adelante, mayúsculas (`noise_kind` por fila) |

### 8. Auto-generación en `geocode-accuracy`

Si el truth no existe y el nombre sigue `{preset}[_fixture]_n{N}.json`:

```bash
.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/generated/argentina_fixture_n150.json \
  --depot-city CABA --origin-lat -34.598 --origin-lon -58.416

N=5000 && .venv/bin/python -m smart_import geocode-accuracy \
  --truth "examples/geocode-truth/generated/france_n${N}.json" \
  --depot-city Paris --depot-country France \
  --origin-lat 48.8566 --origin-lon 2.3522
```

Desactivar: `--no-generate-truth`.

---

## Casos de medición (`geocode-accuracy`)

Geocodifica **solo desde `address`**. Compara pin vs `lat`/`lng` del archivo.
**No** publica las coords del CSV como resultado.

### Mix mundial — un índice por país

El bbox del archivo es planetario: **ningún PBF lo cubre**. `geocode-accuracy`
parte por `preset`/`country` y elige índice + depot de cada región (auto;
`--by-country` / `--no-by-country`). **No** pases `--depot-city` ni `--origin-*`.

```bash
N=100 && python scripts/generate_address_corpus.py --pack languages --mix --new-sample -n $N

.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/generated/languages_mix_n100_s42.json \
  --enhance --limit 200 --no-dump

# Misma medición sobre el mix ruidoso
.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/generated/languages_mix_n100_noise5_s42.json \
  --enhance --limit 200 --no-dump
```

Si ya generaste el mix (seed 42) y no querés otra muestra, corre solo el `geocode-accuracy`.
`--limit 200` recorta el archivo mezclado y después agrupa: **humo** (~7 filas
por país). Para medir de verdad, quitá `--limit` (2700 filas) o usá un JSON
de un solo país. `--no-dump` oculta la tabla fila a fila; el resumen por país
se imprime al final (después de construir índices).

### Argentina CABA — corpus ruidoso (recomendado post-generador)

```bash
N=1000 && .venv/bin/python -m smart_import geocode-accuracy \
  --truth "examples/geocode-truth/generated/argentina_n${N}_noise5.json" \
  --depot-city CABA --depot-country Argentina \
  --origin-lat -34.598 --origin-lon -58.416 \
  --enhance \
  --limit 500 \
  --no-dump
```

Comparar con/sin enhance:

```bash
# baseline parser
N=1000 && .venv/bin/python -m smart_import geocode-accuracy \
  --truth "examples/geocode-truth/generated/argentina_n${N}_noise5.json" \
  --depot-city CABA --origin-lat -34.598 --origin-lon -58.416 \
  --no-enhance --limit 500 --no-dump
```

Dump detallado:

```bash
N=1000 && .venv/bin/python -m smart_import geocode-accuracy \
  --truth "examples/geocode-truth/generated/argentina_n${N}_noise5.json" \
  --depot-city CABA --origin-lat -34.598 --origin-lon -58.416 \
  --enhance \
  --dump-only far \
  --dump-out /tmp/accuracy_far.json \
  --out /tmp/accuracy_report.json
```

### Qué mirar en la salida

| Métrica | Significado |
|---------|-------------|
| `acierto <=100m` | % filas con pin a ≤100 m de la verdad |
| `mediana` | Error típico en metros |
| `needs_geocoding` | Sin pin |
| `libpostal helped` | Direcciones ruidosas donde libpostal aportó road/altura |
| `peores 5` | Casos para convertir en **traps** |

### Truth históricos del repo

Ver [README.md](./README.md): `caba_stops_2907.json`, `caba_ml_13.csv`,
`tandil_stops_326.json`, `miami_whatsapp_6.json`, etc.

---

## Regresión por el camino del producto

`geocode-accuracy` llama al geocoder directo y se saltea el gate, `force_review`,
geofences y reintento limpio. Para validar un cambio del geocoder usar
`geocode-regression` (82 suites, 70+ países, nota por fila y diff de regresiones):
ver [regression/README.md](./regression/README.md).

## Traps (regresión congelada)

Archivos en `traps/` — no se regeneran; fallan si el geocoder empeora.

```bash
pytest -q -m geo_trap tests/test_geocode_traps.py
```

Ejemplo: `traps/caba_homonyms.json` (Santa Fe 2500, Caseros 1800, homónimos provincia vs CABA).

Flujo incremental acordado:

1. Fix + trap congelado
2. Ampliar corpus (OA / ruido)
3. Curar peores casos de `geocode-accuracy` → nuevo trap

---

## API local vs benchmark (confusión frecuente)

| Acción | ¿Aparece en `docker logs vepathos-smart-import`? |
|--------|--------------------------------------------------|
| `geocode-accuracy` (venv) | **No** |
| `curl /health` | Sí (1 línea) |
| `POST /imports` | Sí (`NORMALIZE`, …) |
| `POST /imports/{id}/geocode` | Sí (`GEOCODE`, …) |

### Upload del CSV con lat/lng (truth)

El log mostrará `con_coordenadas=7000 necesitan_geocoding=0` — **no geocodifica**
porque interpreta las coords como del cliente.

Para probar geocode vía API usá CSV **solo con `address`** (sin lat/lng), o columnas
`truth_lat`/`truth_lng` que no mapeen al schema.

### Probar flujo producto con libpostal local

Ver [SETUP.md](../../SETUP.md) § worker distribuido y `.env` con:

```env
SMART_IMPORT_TARGET=runtime-libpostal
SMART_IMPORT_LIBPOSTAL_ENABLED=true
```

Modo cola (como prod): `SMART_IMPORT_ROLE=api` + Rabbit/Redis + worker
(`docker-compose.worker.yml`). Modo simple: `embedded` en `:8100` (default).

---

## Tests automatizados

```bash
# Unit corpus / ruido / OA parser
pytest tests/test_address_corpus.py tests/test_address_noise.py -q

# Traps geográficos
pytest -q -m geo_trap

# Suite sin red ni libpostal
pytest -q -m 'not real_geo and not libpostal'

# Truth pesados (requieren PBF local)
export ROUTE_OPTIMIZER_DATA=...
pytest -q -m real_geo tests/test_geocode_accuracy.py
```

---

## Troubleshooting generación OA

| Error | Causa | Solución |
|-------|-------|----------|
| `OpenAddresses Batch requiere token` | Sin `OPENADDRESSES_TOKEN` | Token en `.env` o `--fixture` |
| HTTP 400 en descarga | Redirect CDN (fix en repo) | Actualizar `smart_import/tools/address_corpus.py` |
| `Extra data` JSON | GeoJSON NDJSON línea a línea | Ya soportado en `iter_geojson_features` |
| `-n 1000` pero 15 filas | `--fixture` solo tiene 15 | Quitar `--fixture` o usar batch OA |
| Nombre `n150` pero 15 filas | Fixture más chico que N pedido | Normal; `n*` en filename = pedido, no garantía |

---

## Referencias

- [README.md](./README.md) — truth históricos y medición CABA/Miami/Tandil
- [generated/README.md](./generated/README.md) — salida del generador
- [SETUP.md](../../SETUP.md) — PBF, Docker local, modo api/worker
- [DEPLOY-PRODUCTION.md](../../DEPLOY-PRODUCTION.md) — worker Mac prod + túnel
