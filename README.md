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
pip install -e ".[ai]"         # + NuExtract (opcional, ~2,5 GB)
```

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
python -m smart_import extract --input out/normalized.csv --output out/separado.csv
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

El modelo de IA **nunca ve el archivo entero**: ve headers + 10-30 filas de muestra.
Una inferencia por archivo, no por fila. El mapping resultante se aplica a las N filas
con código determinístico.

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
- **Sin IA el sistema funciona completo.** Si el modelo no carga, se degrada a reglas
  con un aviso; nunca rompe un import.
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
| `low_confidence` | 0.70 – 0.90, o calle sin altura exacta → coordenadas cargadas **pero marcadas** |
| `not_found` | < 0.70 → **sin coordenadas**, nunca inventadas |
| `error` | fallo del índice |

Un match a nivel calle **no puede** declararse `matched` por más que puntúe alto: sería
afirmar una precisión que no se tiene.

El depot (`--origin-lat/--origin-lon`) desempata entre calles homónimas de distintas
ciudades — la señal que ningún geocoder genérico tiene y Vepathos siempre conoce.

### Medir la calidad antes de confiar

```bash
python -m smart_import geocode-eval --truth direcciones_conocidas.csv \
    --index data/indexes/oslo.sqlite --origin-lat 59.91 --origin-lon 10.75
```

Esconde las coordenadas verdaderas, geocodifica por dirección y compara: cobertura,
% exactas, error en metros (mediana / p90 / máximo).

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
| `POST` | `/imports/{id}/extract` | separa una columna compuesta **con el modelo** (async) |
| `POST` | `/imports/{id}/geocode` | geolocaliza (async). **Nunca automático** |
| `GET` | `/geocoding/coverage?lat&lon` | ¿hay PBF para esta zona? Consultalo antes de ofrecer el botón |
| `DELETE` | `/imports/{id}` | borra job y archivos |

`normalize` es síncrono porque 50k filas tardan ~1,5 s. `extract` y `geocode` son
asíncronos (un worker cada uno) y se consultan con `GET /imports/{id}`.

Cada respuesta trae **`next_actions`**: qué puede hacer el usuario ahora y con qué link.
La UI no necesita conocer la máquina de estados.

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
> vería jobs distintos. Para escalar hace falta el store compartido, que es la etapa
> siguiente junto con RabbitMQ.

---

## El modelo: qué hace y qué no

**El mapeo de columnas es 100 % determinístico — sin IA.** Se probó delegarlo a
NuExtract-1.5-tiny y el modelo **devuelve el schema del prompt** en lugar de razonar
sobre él: mapear headers es una tarea de instrucción, y un 0.5B entrenado para
extracción no la hace. Las reglas resuelven 15 de 16 fixtures.

El modelo se usa en **una sola operación**: separar una columna que mezcla campos, que
es exactamente su tarea de entrenamiento (template JSON + texto → template completo).

```bash
pip install -e ".[ai]"                     # torch + transformers, ~2,5 GB
export SMART_IMPORT_AI_ENABLED=true
python -m smart_import extract -i out/normalized.csv -o out/separado.csv \
    --column address --fields customer_name,address,phone --max-rows 200
```

El modelo se baja solo la primera vez desde HuggingFace (~1 GB) a `~/.cache/huggingface`.

### Medido sobre 12 filas reales (NuExtract-1.5-tiny, 0.5B, CPU)

| Campo | Acierto | |
|---|---|---|
| `address` | **12/12** | limpia, con tildes intactas |
| `phone` | **12/12** | |
| `customer_name` | **9/12** | el 0.5B corrompe nombres con tilde |
| alucinaciones | **0/12** | ninguna, por construcción |

**1,4 s por fila en CPU.** Por eso es opt-in, tiene tope (`SMART_IMPORT_EXTRACT_MAX_ROWS`,
default 2000) y corre en segundo plano.

Cero alucinaciones no es suerte: **todo valor extraído tiene que aparecer en el texto
original**. Extraer no es generar. Cuando el modelo devuelve `"Ana Rodrñez"`, se busca
ese texto en la fuente y se devuelve el span real — `"Ana Rodríguez"`, con su tilde.
Lo que no aparece en la fuente se descarta.

Si el modelo no está instalado o falla, el job **no se rompe**: termina con un aviso y
el archivo normalizado sigue siendo válido.


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

Target `runtime` (por defecto) no incluye torch. El target `ai` sí; usalo solo si el
benchmark lo justifica.

## Configuración

Todo por environment variable, todo con default razonable — ver `.env.example`.

| Variable | Default | |
|---|---|---|
| `SMART_IMPORT_MAX_FILE_MB` | `10` | límite de tamaño |
| `SMART_IMPORT_MAX_ROWS` | `50000` | límite de filas |
| `AUTO_ACCEPT_THRESHOLD` | `0.90` | por debajo → revisión |
| `SMART_IMPORT_AI_ENABLED` | `false` | la IA es opt-in |
| `SMART_IMPORT_DEVICE` | `cpu` | `cpu` / `mps` / `cuda` / `auto` |
| `SMART_IMPORT_PBF_DIR` | — | los `_extracts` del cutter |
| `GEOCODER_FALLBACK` | `none` | nunca llama afuera solo |
| `SMART_IMPORT_EXTRACT_MAX_ROWS` | `2000` | tope de filas para `extract` |

---

## Fuera de alcance

PDF, imágenes, OCR, Nominatim, Pelias, Elasticsearch, PostGIS, GPU, fine-tuning,
proveedores de geocoding pagos. Y ninguna modificación al cutter, al optimizador, a
routehub-fastapi o al router-client.

### Siguiente etapa (diseñada, no implementada)

Consumer de RabbitMQ con colas `smart-import-tasks` + `-dlq` + `-delay` (misma
topología DLX que el optimizador). El archivo **nunca** viaja por la cola: va a object
storage y el mensaje lleva referencias.

```json
{"version": 1, "type": "smart_import.normalize", "job_id": "imp_123",
 "input_object_key":  "smart-import/{user}/{job}/raw/original.xlsx",
 "output_object_key": "smart-import/{user}/{job}/normalized/result.csv",
 "schema": "vepathos_flat_v1"}
```

Estados: `uploaded → queued → analyzing → needs_mapping_review → normalizing →
normalized` … `→ geocode_queued → geocoding → completed | failed`.
**`normalized` es un estado final válido** aunque nunca se geocodifique.
