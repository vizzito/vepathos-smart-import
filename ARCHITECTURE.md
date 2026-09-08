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
{"capabilities": {"normalize": true, "geocoding": true,
                  "rules": true, "phonenumbers": true, "libpostal": false},
 "extraction": {"engine": "rules", "address_parser": "heuristic",
                "libpostal_installed": false}}
```

Una capacidad apagada **no aparece en `next_actions`** — el botón no se dibuja, en vez
de fallar al apretarlo. Y si igual llamás al endpoint, devuelve 503 diciendo qué env var
prender.

### Perfiles del compose

```bash
docker compose up -d                                   # el servicio
docker compose --profile tools run --rm tools list-pbf --lat -34.6 --lon -58.4
docker compose --profile tools run --rm tools build-geocoder-index \
    --origin-lat -34.6 --origin-lon -58.4              # pre-construir un índice
```

`tools` comparte los mismos volúmenes que el servicio, así que un índice que construyas
ahí lo usa el servicio sin copiar nada.

---

## 4. El flujo, paso a paso

> Esta sección es el flujo a nivel integración: qué request va cuándo y qué ve
> el usuario. Para el nivel de código —qué hace cada etapa por dentro, con qué
> lógica decide y por qué está escrita así, sobre todo el camino de la
> dirección— está [docs/pipelines.md](docs/pipelines.md).

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

### El header no es el mecanismo principal

Los `aliases` del schema cubren es/en (y algo de pt). Eso **no** es el techo: el
mapper va `nombre → fuzzy → contenido`, y cuando el nombre no dice nada decide lo
que la columna tiene adentro. Medido sobre 5 campos y 9 idiomas
(`tests/test_mapping_multilingue.py`): **45/45**, incluido un archivo con los
headers en `col_1…col_5`.

Tres reglas sostienen eso, y las tres son "usar lo que ya existe":

* La dirección la decide el **mismo `AddressCandidateScorer`** que usan el
  geocoder y el extractor de texto libre. Antes el mapper tenía su propio regex
  de vías en castellano: aprendía `Hauptstrasse` en un módulo y seguía sin
  entenderla en el otro.
* El tipo de vía puede ir **suelto o pegado**: `Av. Corrientes` en las lenguas
  romances, `Hauptstrasse` / `Kalverstraat` en las germánicas. Los sufijos viven
  en `labels.json` como un pack más.
* La unidad escrita en la celda identifica el peso sin mirar el header: `2,75 kg`
  dice lo mismo en el archivo alemán y en el brasileño, y sale del léxico de
  paquetería.

Y una regla que **baja** confianza en vez de subirla: un match por *parecido*
(`fuzzy`) que el contenido de la columna contradice se manda abajo del piso de
asignación. `Ontvanger` —destinatario en neerlandés— se parece 0.82 a
`container` y se llevaba la columna de nombres a `packaging`.

#### Una escala, tres barras

La confianza **mide siempre lo mismo**: qué tan seguro estoy de que esta columna
es este campo. Por eso los candidatos se pueden comparar entre sí. Lo que no es
igual es **cuánta hace falta**, porque equivocarse no cuesta lo mismo:

| nivel | piso | si me equivoco |
|---|---|---|
| `delivery` | 0.70 | el camión va a otra dirección |
| `timewindow` | 0.65 | la entrega llega fuera de hora |
| `package` | 0.60 | un número mal en un reporte, visible y corregible |

Los tres se derivan de `REVIEW_THRESHOLD`, así que el knob global sigue moviendo
la escalera entera; `MAPPING_MIN_DELIVERY` / `_TIMEWINDOW` / `_PACKAGE` fijan uno
a mano. `/config` los muestra.

No dividir la escala y sí dividir la barra es lo que evita el error de tuneo más
fácil de cometer acá: **subir el score de una regla para que pase el corte**. El
score es lo que el operador lee en el reporte para entender por qué el sistema
creyó lo que creyó; inflarlo para ganarle a un umbral lo convierte en ruido. La
regla de `weight_kg` puntuaba 0.62 contra un piso único de 0.70 — era código
muerto, y la salida honesta era bajar el piso de `package`, no inflar el 0.62.

### Una tabla adentro de un texto libre

La gente escribe un mensaje y le pega abajo el export del sistema. El documento
entero **no** es una tabla —tiene saludo, lista a mano, despedida— así que
`classify_text_mode` lo manda a FREE_TEXT, y hace bien. Pero adentro hay 40
líneas que **sí** son una tabla, y leerlas una por una tira el nombre, los
bultos y la referencia.

```
documento free_text
  → find_table_blocks        forma: corridas de líneas con el mismo delimiter
  → el SCHEMA confirma       ¿el header NOMBRA campos? ¿hay uno de destino?
  → blank_spans              el bloque sale de la vista del segmentador
  → el resto sigue igual     viñetas / bloques / líneas, como siempre
```

La forma sola no alcanza y ese es el punto delicado: `Ana Pérez | Av. Corrientes
100 | 11 4000-1000` también tiene tres celdas cortas y distintas, y comérsela
como header pierde la primera entrega. Por eso un bloque se acepta sólo si el
**mapper reconoce su header por nombre** (`alias` / `normalized` / `fuzzy`, nunca
por el contenido) en al menos 2 columnas y la mitad del bloque, y entre ellas hay
un campo de destino. Sin schema la capa no corre.

Los registros de las dos mitades se mezclan **por posición en el documento**, así
que la numeración se sigue leyendo de arriba hacia abajo. Y el peso de una fila
tabular, que es unitario, se sube a total antes de emitirlo: es la misma
conversión que hace el parser de paquetería con "de 4 kilos cada uno", por la
misma razón.

### La paquetería tiene su propia capa

El paso "bultos" no es una regex: es `smart_import/packages/`, con la misma forma
que `smart_import/addresses/` —vocabulario en datos, parser aparte, resultado con
evidencia— y la usan **los dos caminos**, texto libre y tabular.

```
"3 cajas de 10 lb c/u"
  → catálogo `packaging`   cajas → box (BX)      ← el mismo que mapea headers
  → package_lexicon.json   lb → ×0.45359237, "c/u" → por bulto
  → PackageParse           3 bultos · 4.536 kg c/u · 13.608 kg total · box
```

Los sustantivos salen del catálogo de vocabulario (269 alias en 9 idiomas); los
números en letras, las abreviaturas de despacho, las unidades y las pistas de
"cada uno" / "en total" salen de `resources/package_lexicon.json`. Un typo se
resuelve por **esqueleto fonético** (`paquetes` y `paketed` colapsan a la misma
forma y quedan a distancia 1), no por una lista de errores conocidos.

**`weight_kg` es siempre el peso de UN bulto**, venga de una celda de Excel o de
una frase de WhatsApp. Una sola convención, y la que ya tenía el camino tabular.

No es estilo: el CSV plano **se vuelve a leer** —después de geocodificar, o
porque el usuario lo reimporta— y esa relectura es tabular siempre. Si el
archivo dijera el total de la entrega, cada vuelta multiplicaría el peso por la
cantidad de bultos: "4 paquetes de 4 kilos cada uno" salía 16 kg la primera vez
y **64 la segunda**. Lo que se emite tiene que significar lo mismo que lo que se
lee, y `tests/test_packages_free_text.py::test_round_trip_no_multiplica_el_peso`
lo fija normalizando dos veces seguidas.

Por eso `PackageParse` expone las dos lecturas, `weight_total_kg` y
`weight_per_unit_kg`: la frase declara una y el schema guarda la otra. Sin pista
explícita (`2 cajas 3 kg`) el peso se sigue leyendo como total de la entrega y se
reparte, como siempre: la capa nunca inventa una interpretación que el texto no
declaró.

El contrato de esta capa es un corpus, no una lista de reglas:
`tests/package_cases.py` tiene 166 frases reales en 7 idiomas —cantidades,
plurales, typos, abreviaturas, fracciones, conversiones, `cada uno` vs `total`—
y 24 de ellas son direcciones y teléfonos que **no** tienen que producir ningún
bulto. El reporte de cobertura por patrón sale de ahí:

```bash
pytest tests/test_package_battery.py::test_reporte_de_cobertura -s
```

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
| 1 | **Jobs en memoria** | un reinicio pierde los imports en vuelo; obliga a balanceo sticky entre instancias | 1-2 días (persistir el store en SQLite) |
| 2 | **Sin auth ni tenancy** | cualquiera con acceso a la red lee jobs ajenos | resuelto por el proxy de routehub |
| 3 | **Archivos en disco local** | no sobrevive a recrear el container | conviven con el TTL de jobs; solo molesta con varias instancias |
| 4 | **CORS en `*`** | hay que cerrarlo al dominio real | `SMART_IMPORT_CORS_ORIGINS` |

El #1 es el que manda: mientras el store viva en memoria, la unidad de escala es
la **instancia con routing sticky**, no el proceso. Persistirlo en SQLite es
suficiente para el #1 y el #3 — no hace falta una cola ni object storage, porque
el trabajo dura segundos y ya está acotado por las puertas de concurrencia
(`SMART_IMPORT_MAX_NORMALIZE_QUEUE` / `MAX_CONCURRENT_NORMALIZE`).

### No bloqueantes, pero conviene

| Qué | Detalle |
|---|---|
| Retención de jobs | hoy nada se borra solo. Falta TTL |
| Índices por adelantado | el 1er usuario de una zona espera ~10 s. `--profile tools build-geocoder-index` |
| Calidad de nombres | reglas + lexicones; medir con fixtures de columna-mezclada |
| Cobertura de OSM | medido sólo en Oslo (100 % cobertura, 62 % exactas, 56 m mediana). Falta medir en tus zonas reales con `geocode-eval` |
| Límite de concurrencia | 1 worker de geocode. Subir con medición, no antes |
| Cola de geocode sin techo | los jobs se apilan en `geocode_queued` sin límite. Es latencia, no colapso (la cola solo guarda ids), pero no hay backpressure ni aviso |

### Decisiones que son tuyas

1. **¿Geocoding con fallback pago?** Hoy `GEOCODER_FALLBACK=none`: lo que OSM no
   encuentra queda `not_found`. La interfaz para agregar Google/Mapbox está lista.
2. **¿Dónde corre?** La VM del cutter es lo natural (ahí están los PBFs), pero compite
   por CPU cuando el cutter corta. Si molesta, va a otra VM con los PBFs replicados.
3. **¿libpostal en prod?** Suma ~2,6 GB RSS al primer uso; el heurístico alcanza para
   LatAm típico. Activarlo solo si medís ganancia en tus zonas.

---

## 7. Orden sugerido

```
Fase 1  ✅  CLI + API standalone, dockerizado, con tests
Fase 2  ⬜  probarlo con archivos reales de tus clientes
Fase 3  ⬜  medir cobertura de geocoding en tus zonas (geocode-eval)
Fase 4  ✅  puertas de concurrencia (admisión + techo de CPU + limit_concurrency)
Fase 5  ✅  proxy RouteHub `/imports/smart` + SMART_IMPORT_ENABLED
Fase 6  ✅  UI: geocode fallido → normalize + requires_geocoding (geo manual)
```

Geocode duro que falla deja el job en `geocode_failed` (normalize intacto). La UI
importa las paradas con coords y mete el resto en el preview como
`requires_geocoding` para `LocateMissingStopDialog`.

Local prod-shaped:

```bash
# smart-import
docker compose up -d                          # API :8100
# routehub
SMART_IMPORT_ENABLED=true SMART_IMPORT_URL=http://host.docker.internal:8100
# UI (.env.local) — directo o via RouteHub:
# SMART_IMPORT_URL=http://localhost:8100
# ROUTEHUB_SMART_IMPORT_URL=http://localhost:8000/imports/smart
```

Lo que sigue: persistir el store de jobs para que un reinicio no se lleve los
imports en vuelo. Hoy vive en memoria del proceso, y esa es la razón por la que
varias instancias necesitan routing sticky.
