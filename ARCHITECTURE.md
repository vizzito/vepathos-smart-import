# Arquitectura e integración

## 1. Dónde encaja en tu stack

Sí: **el web client manda el archivo** y Smart Import devuelve el resultado normalizado.
La pregunta importante es *por dónde pasa*, y la respuesta es: por routehub-fastapi, no
directo.

```
   vepathos-router-client  (browser)
            │
            │  POST /imports/smart   multipart: el archivo del cliente
            ▼
   routehub-fastapi  :8000          ← ya existe. Sólo orquesta.
            │                          · autentica (API key / tenant)
            │                          · valida extensión y tamaño
            │                          · hace proxy y devuelve job_id
            │
            │  POST /imports         red privada, NO expuesta a internet
            ▼
   vepathos-smart-import  :8100     ← NUEVO. Todo el trabajo pesado.
            │
            ├── normalize   reglas + fuzzy + heurísticas       (síncrono, ~1.5 s / 50k filas)
            ├── extract     reglas + phonenumbers + libpostal  (síncrono)
            └── geocode     índice SQLite desde OSM            (async)
                    │
                    └──▶ /data/pbf  ← los .osm.pbf del cutter, READ-ONLY
```

### Por qué el proxy y no acceso directo desde el browser

Smart Import **no tiene autenticación ni multi-tenancy**. No sabe qué es una empresa ni
un usuario. Si lo exponés directo, cualquiera sube archivos y lee jobs ajenos.

routehub-fastapi ya resuelve auth y tenant. Que él haga de puerta significa: una sola
capa de seguridad, un solo dominio para el browser, y Smart Import queda en la red
privada donde no lo alcanza nadie de afuera.

### Aislamiento

Smart Import **no importa código** de ningún repo de Vepathos, y nadie importa el suyo.
La dependencia es en un solo sentido, y es HTTP.

```
routehub-fastapi ──HTTP──▶ smart-import        ← si se cae, routehub sigue
smart-import     ──lee───▶ _extracts (:ro)     ← nunca escribe ahí
smart-import     ─────X──▶ cutter, optimizer   ← ninguna relación
```

Con `SMART_IMPORT_ENABLED=false` en routehub, el flujo legacy funciona idéntico a hoy.

---

## 2. Despliegue físico

Corre como **tercer container en la VM que ya tiene el cutter**, porque ahí están los
PBFs y ahí sobra CPU cuando el cutter no está cortando.

```
┌─ VM 16 GB ───────────────────────────────────────────────┐
│                                                          │
│  route-optimizer-cutter    escribe  data/_extracts/*.pbf │
│  vepathos-worker                                         │
│  vepathos-smart-import     lee      /data/pbf  (:ro)     │  ← nuevo
│                            escribe  /data/indexes         │
│                                     /data/cache           │
│                                     /data/jobs            │
└──────────────────────────────────────────────────────────┘
        ▲
        │ red privada
        │
┌─ VM web 4 GB ────────────┐
│  routehub-fastapi  :8000 │
│  vepathos-router-client  │
└──────────────────────────┘
```

El `:ro` no es cosmético: garantiza que Smart Import **no puede** borrar ni corromper los
extracts del cutter, aunque tenga un bug.

Los índices de geocoding viven en **su propio volumen**, no dentro de `_extracts`. Si
mañana corrés un script de limpieza de PBFs, no te lleva puestos los índices.

---

## 3. Configuración: qué corre y qué no

Todo sale de `.env`. El núcleo —`normalize` **y la extracción de texto libre**— siempre
está y no necesita modelo. Lo demás se prende y apaga sin tocar código.

| Quiero… | `.env` | Imagen |
|---|---|---|
| **Normalizar + geocodificar** (default) | `GEOCODING_ENABLED=true` `TARGET=runtime` | ~370 MB |
| Sólo normalizar | `GEOCODING_ENABLED=false` `TARGET=runtime` | ~370 MB |
| + libpostal | `LIBPOSTAL_ENABLED=true` `TARGET=runtime-libpostal` | ~2 GB extra de datos |

```bash
cp .env.example .env
docker compose up -d               # lo que diga el .env
curl -s localhost:8100/health      # verifica qué quedó activo
curl -s localhost:8100/config      # la config efectiva del proceso
```

`/health` devuelve las capacidades reales:

```json
{"capabilities": {"normalize": true, "geocoding": true, "extract": false,
                  "rules": true, "phonenumbers": true, "libpostal": false, "ai": false},
 "extraction": {"engine": "rules", "address_parser": "heuristic",
                "libpostal_installed": false}}
```

Una capacidad apagada **no aparece en `next_actions`** — el botón no se dibuja, en vez
de fallar al apretarlo. Y si igual llamás al endpoint, devuelve 503 diciendo qué env var
prender.

### Perfiles del compose

```bash
docker compose up -d                                   # el servicio
docker compose --profile warmup up warmup              # baja el modelo antes de servir
docker compose --profile tools run --rm tools list-pbf --lat -34.6 --lon -58.4
docker compose --profile tools run --rm tools build-geocoder-index \
    --origin-lat -34.6 --origin-lon -58.4              # pre-construir un índice
```

`tools` comparte los mismos volúmenes que el servicio, así que un índice que construyas
ahí lo usa el servicio sin copiar nada.

---

## 4. El flujo, paso a paso

```
1. El usuario arrastra "entregas-septiembre.xlsx"
       │
2. POST /imports/smart  (routehub valida y hace proxy)
       │
3. Smart Import: READ → DETECT → NORMALIZE → ASSEMBLE → EMIT     ~1.5 s
       │        (si el archivo es texto libre, DETECT es
       │         segmentar → clasificar → extraer, sin modelo)
       │
4. Respuesta:  mapping con confianza + contadores + next_actions
       │
   ┌───┴─────────────────────────────────────────┐
   │                                             │
5a. mapping dudoso                          5b. mapping OK
   PUT /imports/{id}/mapping                     │
   (el usuario corrige un dropdown)              │
   └───────────────┬─────────────────────────────┘
                   │
6. ¿Faltan coordenadas?
   ┌───────────────┴────────────────┐
   │ NO                             │ SÍ
   │                                │
   │                    ┌───────────┴───────────┐
   │                    │ el usuario decide     │
   │                    │                       │
   │           "Continuar sin        "Geolocalizar"
   │            coordenadas"          POST /imports/{id}/geocode
   │                    │                       │
   └────────────────────┴───────────────────────┘
                        │
7. GET /imports/{id}/download?format=nested
   → alimenta el mismo pipeline que hoy recibe un archivo bien formateado
```

### Cuando el archivo es texto libre

Un `.txt` solo se trata como tabla si hay **evidencia estructural**: mismo número de
campos en la mayoría de las líneas, suficientes líneas coherentes, sin viñetas ni
saludos, y un header plausible. Una coma repetida no alcanza.

```
documento entero (nunca se recorta)
  → FreeTextSegmenter          viñetas / numeración / bloques / líneas
  → DeliveryCandidateClassifier ¿hay evidencia logística? si no → `ignored`
  → FieldExtractionPipeline     coordenadas → teléfono → email → bultos →
                                etiquetas → horario → dirección → nombre → notas
  → filas del schema Vepathos
```

`ignored` no es `invalid`: el saludo y la despedida de un mensaje no aparecen como
entregas fallidas ni llegan al geocoder. Cada campo trae `confidence`, `method` y
`evidence`, y el `report` incluye un bloque `extraction` con los contadores y los
descartes con su motivo.

**El paso 6 es el que define el producto.** `normalize` nunca geocodifica: informa
cuántas filas necesitan coordenadas y ofrece la acción. Un archivo normalizado sin
coordenadas **es un resultado válido**.

---

## 5. Contrato para el cliente web

```
POST /imports                   multipart  file=<archivo>
  → { job_id, status, report{mapping, deliveries, needs_geocode}, next_actions[] }

GET  /imports/{id}              polling de estado y progreso
GET  /imports/{id}/preview      filas para la tabla de revisión
PUT  /imports/{id}/mapping      { "Dest.": "address", "Obs": null }
GET  /imports/{id}/download?format=flat|nested|geocoded
POST  /imports/{id}/geocode?origin_lat&origin_lon
      [&depot_city&depot_region&depot_postcode&depot_country&depot_address]
      [&max_distance_km]
POST /imports/{id}/extract      separar columna compuesta con el modelo
GET  /geocoding/coverage?lat&lon   ¿hay PBF acá? consultalo antes de ofrecer el botón
```

**`next_actions` es el contrato clave.** Cada respuesta dice qué puede hacer el usuario
ahora y con qué link. Tu UI dibuja un botón por acción y no replica la máquina de estados:

```jsonc
"next_actions": [
  {"action": "download", "href": "/imports/imp_ab12/download?format=flat",
   "description": "Descargar el archivo normalizado (formato Vepathos)"},
  {"action": "geocode",  "href": "/imports/imp_ab12/geocode",
   "description": "Geolocalizar 40 fila(s) sin coordenadas. NO se ejecuta solo."}
]
```

---

## 6. Qué falta para producción

Lo que sigue **no está resuelto** y hay que decidirlo antes de abrirlo a clientes.

### Bloqueantes

| # | Qué | Por qué importa | Esfuerzo |
|---|---|---|---|
| 1 | **Jobs en memoria** | un reinicio pierde los jobs en curso; `--workers > 1` no funciona | 1-2 días (Redis o Postgres) |
| 2 | **Sin auth ni tenancy** | cualquiera con acceso a la red lee jobs ajenos | resuelto por el proxy de routehub |
| 3 | **Archivos en disco local** | no sobrevive a recrear el container; no escala a varias réplicas | 1 día (object storage, bucket `vepathosprod`) |
| 4 | **CORS en `*`** | hay que cerrarlo al dominio real | `SMART_IMPORT_CORS_ORIGINS` |

Los cuatro se resuelven en la misma etapa: **jobs compartidos + object storage + RabbitMQ**.
El diseño del mensaje ya está y el pipeline no cambia — sólo el almacén.

### No bloqueantes, pero conviene

| Qué | Detalle |
|---|---|
| Retención de jobs | hoy nada se borra solo. Falta TTL |
| Índices por adelantado | el 1er usuario de una zona espera ~10 s. `--profile tools build-geocoder-index` |
| Calidad de nombres | el 0.5B acierta 9/12 nombres con tilde. Medir NuExtract-2.0-2B antes de decidir |
| Cobertura de OSM | medido sólo en Oslo (100 % cobertura, 62 % exactas, 56 m mediana). Falta medir en tus zonas reales con `geocode-eval` |
| Límite de concurrencia | 1 worker para geocode y 1 para extract. Subir con medición, no antes |

### Decisiones que son tuyas

1. **¿`extract` va a prod?** Cuesta 1,4 s/fila. Sirve para archivos chicos con columna
   compuesta. Si tus clientes no mandan ese formato, no lo prendas y ahorrás 2,5 GB de
   imagen.
2. **¿Geocoding con fallback pago?** Hoy `GEOCODER_FALLBACK=none`: lo que OSM no
   encuentra queda `not_found`. La interfaz para agregar Google/Mapbox está lista.
3. **¿Dónde corre?** La VM del cutter es lo natural (ahí están los PBFs), pero compite
   por CPU cuando el cutter corta. Si molesta, va a otra VM con los PBFs replicados.

---

## 7. Orden sugerido

```
Fase 1  ✅  CLI + API standalone, dockerizado, con tests
Fase 2  ⬜  probarlo con archivos reales de tus clientes
Fase 3  ⬜  medir cobertura de geocoding en tus zonas (geocode-eval)
Fase 4  ✅  object storage (local/MinIO) + RabbitMQ (compose --profile infra)
Fase 5  ✅  proxy RouteHub `/imports/smart` + SMART_IMPORT_ENABLED
Fase 6  ✅  UI: geocode fallido → normalize + requires_geocoding (geo manual)
```

Geocode duro que falla deja el job en `geocode_failed` (normalize intacto). La UI
importa las paradas con coords y mete el resto en el preview como
`requires_geocoding` para `LocateMissingStopDialog`.

Local prod-shaped:

```bash
# infra
docker compose --profile infra up -d          # RabbitMQ :5672 + MinIO :9000
# smart-import
docker compose up -d                          # API :8100
# routehub
SMART_IMPORT_ENABLED=true SMART_IMPORT_URL=http://host.docker.internal:8100
# UI (.env.local) — directo o via RouteHub:
# SMART_IMPORT_URL=http://localhost:8100
# ROUTEHUB_SMART_IMPORT_URL=http://localhost:8000/imports/smart
```

Lo que sigue (persistencia de jobs entre reinicios / Redis): el store sigue en
memoria del proceso; los artefactos ya pueden vivir en MinIO/S3.
