# Corpus con coordenadas verificadas

Compara **la coordenada real** de cada parada con el pin que genera Smart Import.
Sin esto, “geocodifica bien” es una opinión.

Documentación de **cómo correr** la medición (CLI + pytest) y cómo agregar otra zona.

## 1. Apuntar a los PBF (una vez por terminal)

Los extracts **no** están sueltos en `data/*.osm.pbf`. Viven en
`data/_extracts/<zona>/n…_s…-pyrosm.osm.pbf`. Los PBF de país/región viven en
`data/<continente>_tile_…/` (a veces anidados: `india/`, `us_tile_…/`).
El registry recorre **toda** la raíz `data/` con rglob.

```bash
# NO: /workspace/...  (eso no existe en la Mac)
# NO: .../data/_extracts  solo  (si está vacío no ve argentina-pyrosm.osm.pbf)

unset SMART_IMPORT_PBF_DIR
export ROUTE_OPTIMIZER_DATA=/Users/martinvizzolini/workspace/route-optimizer-app/data

# verificar que hay mapas (recursivo)
find "$ROUTE_OPTIMIZER_DATA" -name '*.osm.pbf' | head
```

`SMART_IMPORT_PBF_DIR` **pisa** a `ROUTE_OPTIMIZER_DATA`. Si apuntás a una
carpeta inexistente, el mensaje es `ningun PBF cubre ese bbox`.

Si no hay extract de CABA, usa el PBF de país (`argentina-pyrosm.osm.pbf`) si
está bajo esa raíz. `_extracts` populado **no** es obligatorio.

La primera corrida construye el índice sqlite (una vez). Después lo reutiliza.

## 2. El reporte que importa (2907 paradas CABA)

Esto es la comparación real vs generada. Tarda ~1 min.

```bash
cd ~/workspace/vepathos-smart-import

# 13 dirs MercadoLibre — precisión con input limpio
.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/caba_ml_13.csv

# 13 orders estilo Tiendanube (calle + nro separados; 10 con lat/lng)
.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/caba_orders_13.json \
  --depot-city CABA --depot-country Argentina

# 6 dirs del paste de Miami (demo web). Geolocalizador = Miami.
# La 1ª vez: extract de Miami + índice (el PBF de Florida entero es 625 MB).
# osmium extract -b -80.35,25.65,-80.10,25.95 -o \
#   "$ROUTE_OPTIMIZER_DATA/_extracts/florida/n25.95_s25.65_e-80.10_w-80.35-pyrosm.osm.pbf" \
#   "$ROUTE_OPTIMIZER_DATA"/north-america_tile_*/us_tile_*/florida-pyrosm.osm.pbf
.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/miami_whatsapp_6.json \
  --origin-lat 25.774 --origin-lon -80.194 \
  --depot-city Miami --depot-country "United States"

# 2907 paradas reales — recall (el que pedís)
.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/caba_stops_2907.json \
  --out /tmp/caba_2907_accuracy.json

# 326 paradas Tandil — todas con altura (calle + nro + CP). El extract de CABA no cubre.
.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/tandil_stops_326.json \
  --origin-lat -37.3211784 --origin-lon -59.1343635 \
  --depot-city Tandil --depot-country Argentina \
  --no-enhance
```

La salida es una **tabla fila a fila** (coord real, pin, metros, address
original, address enviado al geo) y al final totales: acierto (≤100 m),
% con pin, mediana y confianza media.

```bash
# 2907 filas + totales. Mismos params que POST /geocode (depot + geolocalizador).
.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/caba_stops_2907.json \
  --origin-lat -34.6037 --origin-lon -58.3816 \
  --depot-city CABA --depot-country Argentina \
  --dump-only house | less -S

# solo las 558 con altura
.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/caba_stops_2907.json \
  --dump-only house | less -S

# pytest (misma tabla; -s para verla)
.venv/bin/python -m pytest -s -m real_geo tests/test_geocode_accuracy.py
```

`--dump-only`: `all` | `house` | `pin` | `miss`. `--no-dump` deja solo totales.

El comando detecta solo columnas de dirección + lat/lng (CSV, JSON o XLSX).

| archivo | n | con altura | qué mide |
|---|---|---|---|
| `caba_ml_13.csv` | 13 | 12 (92%) | **precisión** (`Av. Santa Fe 3200`) |
| `caba_orders_13.json` | 13 (10 con lat/lng) | 13 (100%) | **precisión** sobre export Tiendanube (`Av. Rivadavia` + `4800`) |
| `miami_whatsapp_6.json` | 6 | 6 (100%) | **precisión** del paste demo (`100 Biscayne Blvd` …). Texto: `miami_whatsapp_6.txt` |
| `caba_stops_2907.json` | 2907 | 558 (19%) | **recall** sobre paradas escritas como vienen |
| `tandil_stops_326.json` | 326 | 326 (100%) | **precisión** interior BA: `Sarmiento 1219, B7000 Tandil…` |
| `tiendanube_ba_12.json` | 12 | 12 | Tiendanube BA (calle+nro). Source: `sources/tiendanube-orders-ba.csv` |
| `mercadolibre_ba_12.json` | 12 | 12 | ML flattened, solo coords válidas. Source: `sources/mercadolibre-shipment-flattened.csv` |
| `vepathos_orders_caba_12.json` | 12 | 12 | Export Vepathos (1 fila por delivery). Source: `sources/vepathos-orders.csv` |
| `shopify_caba_12.json` | 12 | 12 | Shopify `address1`. Source: `sources/shopify-orders-caba.json` |

No se promedian. El grande es CABA real; el chico es un export de ML.

Salida típica: `con pin`, `<=100m`, mediana, peores 5. El % es **sobre el total**,
no sobre los que tuvieron pin. Siempre mirá **con altura** vs **sin altura** aparte.

```bash
# gate CI: fallar si <=100m (con altura) baja de N%
.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/caba_ml_13.csv --fail-under 0
```

## 3. pytest (misma comparación, aserciones)

Siempre (sin PBF): detecta columnas y que el % no se calcule mal.

```bash
.venv/bin/python -m pytest tests/test_geocode_accuracy.py tests/test_mapping.py tests/test_vocab.py -q
```

Con PBF + índice (los 2 `real_geo`: ML 13 + las 2907):

```bash
unset SMART_IMPORT_PBF_DIR
export ROUTE_OPTIMIZER_DATA=/Users/martinvizzolini/workspace/route-optimizer-app/data

.venv/bin/python -m pytest -q -m real_geo tests/test_geocode_accuracy.py
```

Smoke más corto (primeras 200 del JSON):

```bash
SMART_IMPORT_GEOCODE_TRUTH_LIMIT=200 \
  .venv/bin/python -m pytest -q -m real_geo tests/test_geocode_accuracy.py
```

Si skippea: no hay `ROUTE_OPTIMIZER_DATA`, la ruta es `_extracts` vacío, o falta
el índice. Corré primero el `geocode-accuracy` del §2 (lo construye).

## 4. Cómo leer el reporte

**Con altura vs sin.** El 81% del JSON no tiene número (`Av Victorica`). Eso no
se resuelve a puerta ni en teoría.

**Línea base (2026-09-06), 2907 CABA:**

```
grupo             n   con pin   <=100m   <=250m   <=500m   mediana
con altura      558      12%       9%      10%      10%      41 m
sin altura     2349      12%       2%       3%       5%     814 m
```

ML 13 (extract CABA, 2026-09-06 tarde): ~83% pin, mediana ~540 m, **0% ≤100 m**.
pytest solo exige ≥80% pin y mediana ≤700 m en ese CSV.

**No bajes** `GEOCODE_MATCH_THRESHOLD` / `GEOCODE_SOFT_REJECT_MIN` para “subir
recall”: en CABA eso infló pines a kilómetros.

## 5. Agregar otra zona (más casos de uso)

1. Exportá paradas **con lat/lng verificados** (CSV/JSON/XLSX).
2. Guardá el archivo acá: `examples/geocode-truth/<zona>.csv`.
3. Medí:

```bash
.venv/bin/python -m smart_import geocode-accuracy \
  --truth examples/geocode-truth/<zona>.csv \
  --out examples/geocode-truth/<zona>_YYYY-MM-DD.json
```

4. Reportá con altura vs sin altura. No promedies.
5. Si querés un test: copiá `test_recall_paradas_caba_2907` y cambiá el path +
   pisos (`pin_pct`). No agregues SKUs ni aliases al overrides para “mejorar” esto.

Otras zonas no reemplazan CABA: el techo es cobertura OSM de **esa** ciudad.

## 6. Vocabulario (no es geocode)

```bash
.venv/bin/python -m smart_import.vocab setup
.venv/bin/python -m smart_import.vocab setup --geonames
.venv/bin/python -m smart_import.vocab stats
```

En Docker el build/entrypoint ya corre el setup. Ver [SETUP.md](../../SETUP.md).
