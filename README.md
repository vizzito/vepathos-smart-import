# Vepathos Smart Import

Convierte archivos de entregas **en cualquier formato y con cualquier estructura** al
formato Vepathos. La geolocalización es una **acción aparte** que decide el usuario.

Servicio **independiente**: no importa código de ningún repo de Vepathos, no modifica el
cutter ni el optimizador, y si está apagado el flujo legacy funciona exactamente igual.

```
archivo del cliente          →  normalize  →  archivo Vepathos
(CSV/TSV/TXT/XLSX/XLS/JSON)                   (plano + anidado)
                                                     │
                                         el usuario decide
                                                     │
                                                 geocode  →  + coordenadas
```

---

> **¿Cómo lo instalo / deployo?** Ver [SETUP.md](SETUP.md) — Docker local y prod,
> concurrencia y escala, prune de imágenes, web, variables y cheatsheet.
>
> **¿Cómo lo pruebo?** Ver [RUNBOOK.md](RUNBOOK.md) — cinco niveles, de `pytest`
> (30 s, sin dependencias) hasta la integración con la web. Casos variables en
> [`examples/`](examples/README.md). Smoke HTTP: `./scripts/http-smoke.sh`.
> **No hace falta subir un archivo por la web para probar que todo anda.**

## Instalación

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # normalización + tests
pip install -e ".[geo]"        # + geocoder (pyosmium)
pip install -e ".[api]"        # + servicio HTTP (FastAPI)
pip install -e ".[libpostal]"  # + parser libpostal (opcional, ver docs/libpostal.md)
python -m smart_import.vocab setup            # sqlite; en Docker lo hace el build
python -m smart_import.vocab setup --geonames # ciudades mundiales (opcional)
```

Deploy (local y prod): [SETUP.md](SETUP.md). El `docker compose build` ya corre el setup.

## Los comandos

```bash
python -m smart_import inspect fixtures/preamble_dirty.xlsx
python -m smart_import detect  --input fixtures/es_headers_raros.xlsx
python -m smart_import normalize --input fixtures/es_headers_raros.xlsx \
    --output out/normalized.csv --emit flat,nested
python -m smart_import build-geocoder-index --pbf /data/pbf/<region>.osm.pbf \
    --output data/indexes/<region>.sqlite
python -m smart_import geocode --input out/normalized.csv \
    --index data/indexes/<region>.sqlite --origin-lat -34.6037 --origin-lon -58.3816 \
    --output out/geocoded.csv
python -m smart_import normalize --input examples/free-text/whatsapp_12.txt \
    --output out/wa.csv --phone-region AR       # texto libre: 12 entregas, sin modelo
python -m smart_import benchmark-extraction --repeats 5
python -m smart_import serve --port 8100
```

---

## Qué produce

### Formato plano (CSV/XLSX, hoja `deliveries`) — una fila por `delivery × package`

```
delivery_id, lat, lng, address, zone, customer_name, phone, reference,
package_id, quantity, weight_kg, length_cm, width_cm, height_cm, volume_cm3,
packaging, value_cents, tw_start, tw_end, tw_timezone,
priority, service_time_min, status
```

Se emiten **solo las columnas que existían en el archivo de entrada**, en este orden.
Por eso un archivo que ya viene en formato Vepathos sale **byte a byte idéntico**.

### Formato anidado (JSON) — lo que consume el optimizador

```json
{"addresses": [
  {"delivery_id": "DLV-1000", "lat": 47.620949, "lng": -122.294487,
   "address": "900 Broadway, Seattle", "zone": "C-1",
   "packages": [
     {"package_id": "PKG-001", "weight_kg": 0.0,
      "dimensions": {"length": 22.0, "width": 18.0, "height": 6.0},
      "time_window": {"start": "...", "end": "...", "time_zone": "America/Los_Angeles"}}
   ]}
]}
```

### Report (`<salida>.report.json`)

Mapping aplicado con confianza y método, filas por estado, avisos, tiempos por etapa
y hasta 200 filas con problemas detallados.

---

## Cómo decide el mapping

El mapping de columnas es **determinístico**: alias del schema, normalización de
nombres, fuzzy matching y perfilado del contenido. No interviene ningún modelo. El
mapping resultante se aplica a las N filas con código.

| # | Paso | Confianza |
|---|---|---|
| 1 | alias exacto (`lat`, `direccion`, `Dest.`) | 0.97 – 1.00 |
| 2 | nombre normalizado (sin acentos/puntuación) | 0.97 |
| 3 | fuzzy (`Dir. entrega` → `address`) | ≤ 0.95 |
| 4 | **contenido de la columna** (rangos, patrones, cardinalidad) | ≤ 0.93 |
| 5 | modelo pequeño — **solo** sobre las columnas que quedaron dudosas | 0.85 |
| 6 | sigue dudoso → `needs_review`, lo corrige el usuario | — |

Si el nombre y el contenido coinciden, la confianza sube. Si el nombre dice `lat` pero
los valores no están en `[-90, 90]`, la confianza **baja**.

Umbrales: `AUTO_ACCEPT_THRESHOLD=0.90`, `REVIEW_THRESHOLD=0.70`.

Corregir un mapping a mano:

```bash
echo '{"Codigo interno": "reference", "Sucursal origen": null}' > mapping.json
python -m smart_import normalize -i archivo.xlsx -o out/x.csv --mapping mapping.json
```

---

## Reglas que el sistema no rompe

- **`normalize` nunca geocodifica.** Geocodificar es una segunda decisión del usuario.
- **Nunca inventa datos.** Campo opcional ausente = vacío. Match débil = sin coordenadas.
- **Una fila sin coordenadas pero con dirección no se descarta**: queda `needs_geocode`.
- **Coordenadas fuera de rango se rechazan y se reportan**, no se "arreglan".
- **lat/lng invertidas se avisan, no se corrigen solas** — invertir en silencio manda
  entregas a otro país.
- **Una fila corrupta no reclasifica la columna entera** (rangos con estadística robusta).
- **`weight_kg = 0` es cero, no null.** Una entrega sin bultos sigue siendo válida.
- **Sin modelo externo el sistema funciona completo.** Extracción = reglas +
  librerías (`phonenumbers`, libpostal opcional).
- **`GEOCODER_FALLBACK=none` por defecto**: jamás llama a un servicio externo solo.

---

## Geocoding: reutiliza los PBF que ya genera el cutter

El cutter deja sus extracts en `data/_extracts/<zona>/` con el **bbox en el nombre**:

```
n60.02_s59.55_e10.95_w10.68-pyrosm.osm.pbf
```

Smart Import lee **solo el nombre del archivo** para saber qué cubre cada PBF: cero
acoplamiento con el código del cutter y cero necesidad de abrir PBFs de 300 MB.

```bash
export SMART_IMPORT_PBF_DIR=/data/pbf          # los _extracts, montados :ro
python -m smart_import list-pbf --lat 59.91 --lon 10.75
```

El PBF se procesa **una sola vez** por región; después geocodificar 50.000 direcciones
consulta SQLite (FTS5 + RTree), no vuelve a tocar el PBF.

```
región.osm.pbf  →  build-geocoder-index  →  región.sqlite  →  geocode ×50.000
```

Si no pasás `--index`, se elige el **extract más chico que cubra** el depot y se
construye el índice al vuelo:

```bash
python -m smart_import geocode -i out/normalized.csv -o out/geocoded.csv \
    --origin-lat 59.91 --origin-lon 10.75
```

### Estados de salida

| `geocode_status` | Significado |
|---|---|
| `already_geocoded` | ya tenía coordenadas: se conservan intactas |
| `matched` | score ≥ 0.90 **y** altura resuelta |
| `matched` | ≥ ~81% con altura resuelta → pin confiable (UI verde Valid) |
| `low_confidence` | 70–80%, o calle sin altura exacta → coordenadas a revisar (UI amarillo Review) |
| `not_found` | < 70% → **sin coordenadas**, nunca inventadas |
| `error` | fallo del índice |

Un match a nivel calle **no puede** declararse `matched` por más que puntúe alto: sería
afirmar una precisión que no se tiene.

El depot (`--origin-lat/--origin-lon`) desempata entre calles homónimas de distintas
ciudades — la señal que ningún geocoder genérico tiene y Vepathos siempre conoce.

### Medir la calidad antes de confiar

CABA 13 + 2907 (PBF, pytest, cómo leer el %):
[examples/geocode-truth/README.md](examples/geocode-truth/README.md).

```bash
unset SMART_IMPORT_PBF_DIR
export ROUTE_OPTIMIZER_DATA=/Users/martinvizzolini/workspace/route-optimizer-app/data

.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/caba_stops_2907.json
```

### Cache

Clave = dirección normalizada + región. Los clientes de logística repiten destinos.

```
primera corrida: 6 filas, 0.199 s  (0 hits)
segunda corrida: 6 filas, 0.001 s  (100 % hits)
```

---

## Servicio HTTP

```bash
export SMART_IMPORT_PBF_DIR=/ruta/al/route-optimizer-app/data/_extracts
python -m smart_import serve --port 8100
# docs interactivas: http://localhost:8100/docs
```

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/health` | capacidades: IA instalada, PBFs visibles, índices construidos |
| `GET` | `/schemas` | schemas destino y sus campos |
| `POST` | `/imports` | sube y **normaliza** (síncrono). Devuelve mapping + report + `next_actions` |
| `GET` | `/imports/{id}` | estado del job (`progress`, `busy`, `next_actions`, `urls`) |
| `GET` | `/imports/{id}/progress` | solo barra de avance (polling liviano) |
| `GET` | `/imports/{id}/events` | **SSE**: `progress` + `done` en vivo |
| `GET` | `/imports/{id}/preview` | muestra de filas para pintar en la UI |
| `PUT` | `/imports/{id}/mapping` | corrige el mapping y re-normaliza |
| `GET` | `/imports/{id}/download?format=flat\|nested\|geocoded` | descarga el resultado |
| `POST` | `/imports/{id}/geocode` | geolocaliza (async). **Nunca automático** |
| `GET` | `/geocoding/coverage?lat&lon` | ¿hay PBF para esta zona? Consultalo antes de ofrecer el botón |
| `DELETE` | `/imports/{id}` | borra job y archivos |

`normalize` es síncrono porque 50k filas tardan ~1,5 s. `geocode` es asíncrono
(un worker) y se consulta con `GET /imports/{id}`. La columna mezclada se separa
en el mismo normalize (reglas); no hay endpoint `extract`.

Cada respuesta trae **`next_actions`**: qué puede hacer el usuario ahora y con qué link.
La UI no necesita conocer la máquina de estados.

### `geocode_band`: el color lo decide el back

La UI **no calcula la banda**. Smart Import la resuelve una sola vez y la publica; el
cliente la usa tal cual. Recalcularla con umbrales propios es lo que hacía que un
match a nivel calle se pintara verde: el `status` decía "revisar" y el número decía
otra cosa.

| Banda | Significado | UI |
|---|---|---|
| `valid` | la coordenada se usa tal cual | verde, con `%` |
| `review` | hay pin, pero es a nivel calle | ámbar, con `%` |
| `needs_geocoding` | no hay pin | "Set location", sin `%` |

Los cortes viven en el back (`GEOCODE_REVIEW_BAND`, `GEOCODE_VALID_BAND`) y viajan en
`/issues → summary.umbrales_banda`. **No usar `NEXT_PUBLIC_GEOCODE_*` en el cliente**:
dos fuentes de umbral se desincronizan solas.

Dónde llega la banda:

- **CSV** (`download?format=geocoded`, `preview?source=geocoded`) →
  `geocode_band`, `geocode_confidence`, `geocode_status`, `geocode_precision`, `geocode_source`
- **JSON nested** (`download?format=nested`) → `addresses[].geocode.{band, confidence, status, precision, source}`
- **`/issues`** → una fila por entrega que hay que tocar, con `geocode_band`,
  `geocode_percent` y `geocode_precision`; el resumen trae `bandas` y `umbrales_banda`

Dos invariantes que el cliente puede asumir:

1. `geocode_confidence` es la confianza en el **punto**, no en el match textual. Una
   fila `review` nunca reporta un número de la banda verde.
2. Sin pin no hay `geocode_confidence`. Un `%` al lado de "Set location" describiría un
   candidato que se descartó.

```jsonc
// GET /imports/{id}/issues
{"summary": {"total": 12, "listas": 1, "a_revisar": 9, "a_geocodificar": 2,
             "bandas": {"valid": 1, "review": 9, "needs_geocoding": 2},
             "umbrales_banda": {"valid": 0.85, "review": 0.75}},
 "filas": [{"delivery_id": "003", "status": "a_revisar", "geocode_band": "review",
            "geocode_confidence": 0.80, "geocode_percent": 80,
            "geocode_precision": "street"}]}
```

```jsonc
{"job_id": "imp_ab12", "status": "normalized",
 "report": {"deliveries": 40, "packages": 67, "valid_rows": 0, "needs_geocode": 40,
            "mapping": {"Dest.": {"target": "address", "confidence": 0.99}}},
 "next_actions": [
   {"action": "download", "href": "/imports/imp_ab12/download?format=flat"},
   {"action": "geocode",  "href": "/imports/imp_ab12/geocode",
    "description": "Geolocalizar 40 fila(s). NO se ejecuta solo: lo decide el usuario."}]}
```

> El almacén de jobs vive **en memoria del proceso**: con `--workers > 1` cada worker
> vería jobs distintos, así que el arranque falla a propósito. Para escalar se agregan
> instancias con balanceo **sticky**, no procesos.

---

## Texto libre: cómo se resuelve sin modelo

Un paste de WhatsApp **no es un CSV porque tenga comas**. Antes lo era, y el
documento quedaba destruido antes de llegar a ningún extractor:

```
delimiter=','  header_row=2  columns=2
→ ['- Juan Lopez (1140011001) entrega en Palermo', ' la calle es Av. Santa Fe al 137.']
```

La primera entrega (Ana Perez) desaparecía tomada como fila de header.

Hoy un `.txt` se declara **tabular** solo con evidencia estructural real: mismo número
de campos en la mayoría de las líneas, suficientes líneas coherentes, ausencia de
viñetas y saludos, y un header plausible. Sin eso es `free_text` y **el documento se
preserva entero**.

```
FREE TEXT
 → segmentar            viñetas / numeración / bloques / líneas
 → clasificar           ¿este segmento es una entrega? (evidencia, no adivinanza)
 → extraer              coordenadas → teléfono → email → bultos → etiquetas →
                        horario → dirección → nombre → notas
 → validar              confidence por campo
 → normalizar           E.164, ventanas horarias, contexto regional
 → geocodificar         opcional, y solo lo que tiene evidencia suficiente
```

Cada extractor resuelto **achica la ambigüedad del siguiente**: cuando el teléfono ya
se identificó, el que busca el nombre no pelea con esos dígitos. El texto original
nunca se modifica — se enmascara lo consumido y los spans siguen siendo válidos.

### Medido sobre el mismo documento de 12 entregas

| | reglas (actual) |
|---|---|
| tiempo | **~5 ms** |
| CPU | bajo, sin modelo |
| entregas detectadas | 12/12 |
| teléfonos E.164 | 12/12 |
| direcciones con calle+altura | 12/12 |

El modelo NuExtract se midió en su momento (~18 s / fila en CPU) y se eliminó:
no aportaba sobre el heurístico y costaba ~2,5 GB de imagen.

```bash
python -m smart_import benchmark-extraction --repeats 5
```

Las reglas son el camino de producción: rápidas, sin modelo y con mejor acierto
en el corpus medido.

### Qué garantiza el pipeline

- **No inventa.** Sin evidencia suficiente el campo queda en `null` y la fila en
  `needs_review`. Preferimos `address = null` antes que `address = "Salutos"`.
- **`ignored` ≠ `invalid`.** El saludo y la despedida de un mensaje no son entregas
  fallidas: no aparecen como filas rotas ni llegan al geocoder.
- **Extraer ≠ normalizar.** El extractor devuelve solo lo que está escrito; agregar
  "Buenos Aires" o "Argentina" es trabajo del normalizador, y solo con contexto
  regional explícito.
- **Cada campo trae `confidence`, `method` y `evidence`**, para poder explicar por qué
  se decidió lo que se decidió.

El mapeo de columnas es 100 % determinístico (aliases + heurísticas + catálogo).

### Direcciones: `libpostal` es un paso de calidad opcional

El heuristico siempre corre. Con `SMART_IMPORT_LIBPOSTAL_ENABLED=true` (y la
libreria instalada) libpostal se consulta **solo** cuando falta `road` o la road
es sospechosa — no es un cambio de estrategia. Apagado por defecto; ver
[docs/libpostal.md](docs/libpostal.md).

---

## Archivos de prueba

No se versionan binarios: se regeneran.

```bash
python -m smart_import make-fixtures --out fixtures --rows 40 --sizes 1000,5000,10000
```

| Fixture | Qué prueba |
|---|---|
| `ref_ar_orders.csv/.xlsx/.json` | archivos reales — round-trip idéntico |
| `ref_us_seattle.xlsx` | entregas sin bultos, `tw_timezone`, `weight_kg = 0` |
| `es_headers_raros.xlsx` | `Dest.` `Receptor` `Contacto 1` `Kg` `Cant bultos` |
| `es_sin_coords.csv` | 100 % `needs_geocode` |
| `en_weird.csv` | `Ship To Address` `Recipient` `Mobile` `Pkg Count` |
| `semicolon_latin1.csv` | delimitador `;`, cp1252, **coma decimal** (`1,34`) |
| `pipe_delimited.txt` / `tabs.tsv` / `legacy.xls` | otros formatos |
| `preamble_dirty.xlsx` | 4 filas de título antes del header, filas vacías, hojas extra |
| `swapped_coords.csv` | lat/lng invertidas → aviso, sin corrección automática |
| `one_row_per_delivery.xlsx` | `bultos = 3` → expandir a 3 bultos |
| `merged_field.csv` | todo en una columna → el caso que necesita el modelo |
| `no_headers.csv` | `Campo 1..6` → solo heurísticas de contenido |
| `mixed_locale.xlsx` | filas AR + US en el mismo archivo |
| `scale_{1k,5k,10k,50k}.xlsx` | volumen |

```bash
pytest -q                                        # 100 tests
python -m smart_import benchmark --sizes 1000,5000,10000,50000
```

### Rendimiento medido (MacBook Apple Silicon, sin IA)

| filas | read | detect | normalize | total | filas/s | ΔRSS |
|---|---|---|---|---|---|---|
| 1.000 | 0,03 s | 0,004 s | 0,003 s | 0,04 s | 25.793 | 0,4 MB |
| 5.000 | 0,10 s | 0,004 s | 0,014 s | 0,14 s | 35.973 | 3,8 MB |
| 10.000 | 0,21 s | 0,004 s | 0,029 s | 0,29 s | 35.080 | 14,3 MB |
| 50.000 | 1,11 s | 0,004 s | 0,183 s | 1,52 s | 32.867 | 66,0 MB |

`detect` es **constante**: no depende de la cantidad de filas. Ese es todo el punto del
diseño — el archivo puede crecer sin que crezca el costo de entenderlo.

---

## Docker: como container independiente en la VM del cutter

```bash
cp .env.example .env.prod
export ROUTE_OPTIMIZER_DATA=/home/martin/route-optimizer-app/data
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml run --rm smart-import list-pbf
```

Monta `<repo-del-cutter>/data/_extracts` en `/data/pbf` **read-only**: ve los mismos
archivos físicos, sin duplicar un byte y sin poder tocarlos.

```
VM 16 GB
├── route-optimizer-cutter   escribe  data/_extracts/*.osm.pbf
├── vepathos-worker
└── vepathos-smart-import    lee      /data/pbf  (:ro)
                             escribe  /data/indexes, /data/cache
```

Target `runtime` (por defecto) o `runtime-libpostal` si querés el parser C.

## Configuración

Todo por environment variable, todo con default razonable — ver `.env.example`.

| Variable | Default | |
|---|---|---|
| `SMART_IMPORT_MAX_FILE_MB` | `10` | límite de tamaño |
| `SMART_IMPORT_MAX_ROWS` | `50000` | límite de filas |
| `AUTO_ACCEPT_THRESHOLD` | `0.90` | por debajo → revisión |
| `SMART_IMPORT_DEFAULT_PHONE_REGION` | — | región ISO para teléfonos (`AR`, `IN`…) |
| `SMART_IMPORT_DELIVERY_ACCEPT_THRESHOLD` | `0.55` | evidencia mínima para ser una entrega |
| `SMART_IMPORT_ADDRESS_ACCEPT_THRESHOLD` | `0.50` | por debajo no se acepta ni se geocodifica |
| `SMART_IMPORT_TEXT_MAX_PROSE_RATIO` | `0.20` | cuánta prosa tolera un `.txt` tabular |
| `SMART_IMPORT_LIBPOSTAL_ENABLED` | `false` | enhancer opcional; ver `docs/libpostal.md` |
| `SMART_IMPORT_ADDRESS_PARSER` | `heuristic` | solo benchmarks: `libpostal` / `enhanced` / `hybrid` |
| `SMART_IMPORT_PBF_DIR` | — | los `_extracts` del cutter |
| `GEOCODER_FALLBACK` | `none` | nunca llama afuera solo |

---

## Fuera de alcance

PDF, imágenes, OCR, Nominatim, Pelias, Elasticsearch, PostGIS, GPU, fine-tuning,
proveedores de geocoding pagos. Y ninguna modificación al cutter, al optimizador, a
routehub-fastapi o al router-client.

### Tampoco hay cola ni object storage, y es a propósito

Un normalize de 50k filas tarda ~1,5 s. Eso no justifica la infraestructura ni la
operación de un trabajo asincrónico, así que la API hace todo en proceso: jobs en
memoria, archivos en un volumen local, y tres puertas de concurrencia que evitan
que una avalancha tumbe la VM (`SMART_IMPORT_HTTP_LIMIT_CONCURRENCY` →
`MAX_NORMALIZE_QUEUE` → `MAX_CONCURRENT_NORMALIZE`).

Para dar más capacidad se agregan **instancias** con balanceo sticky, no procesos
ni colas. El detalle medido está en
[SETUP.md §3](SETUP.md#3-concurrencia-qué-pasa-con-muchos-usuarios-a-la-vez).

Estados: `uploaded → analyzing → needs_mapping_review → normalizing →
normalized` … `→ geocode_queued → geocoding → completed | failed`.
**`normalized` es un estado final válido** aunque nunca se geocodifique.
