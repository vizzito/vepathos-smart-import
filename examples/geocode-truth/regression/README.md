# Regresión del geocoder fila por fila

`geocode-accuracy` mide el geocoder **llamándolo directo**. Producción no lo usa así:
pasa por `geocoding.runner.run`, que agrega el gate de evidencia, las bandas con
`force_review`, los geofences y el reintento limpio. Esa diferencia escondió
regresiones reales (2026-09-15: filas como `alvarado 471` descartadas sin buscar).

`geocode-regression` corre **el camino del producto** y califica cada fila:

```bash
# snapshot (perfil fast: límites por suite; full: todo)
.venv/bin/python -m smart_import geocode-regression --label base --profile full

# después del cambio, comparar y fallar si hay regresiones
.venv/bin/python -m smart_import geocode-regression --label fix --profile full \
  --against out/regression/base

# solo algunas suites / tags
.venv/bin/python -m smart_import geocode-regression --label smoke --only tandil_planillas_reales
.venv/bin/python -m smart_import geocode-regression --label ar --tags ar

# con los umbrales de producción (verde ≥ 0.85, soft-reject ≥ 0.75)
.venv/bin/python -m smart_import geocode-regression --label base-prod --profile full \
  --env-file deploy/templates/api-prod.env.template

# diff entre dos snapshots ya hechos
.venv/bin/python -m smart_import geocode-regression-diff out/regression/base out/regression/fix
```

## Nota de cada fila

| resultado | nota | significa |
|---|---|---|
| `green_good` / `none_expected` | 6 | verde a ≤ 100 m, o basura correctamente sin pin |
| `amber_good` | 5 | ámbar a ≤ 100 m |
| `amber_fair` | 4 | ámbar a ≤ 500 m |
| `none` | 3 | sin pin (se ubica a mano) |
| `green_fair` | 2 | verde entre 100 y 500 m: nadie lo revisa |
| `amber_bad` | 1 | ámbar lejos |
| `green_bad` | 0 | **falso verde**: el peor error |

Una fila baja de nota = **regresión**. Un pin idéntico que pasa de verde a ámbar
se lista aparte (`verde→ambar`): es política de bandas, no un pin peor.

## Tipos de verdad

- `point`: lat/lng (OpenAddresses, paradas reales).
- `street`: el pin tiene que caer sobre esa calle del índice (planillas sin puerta).
- `intersection`: cruce verificado por nodos (< 40 m).
- `none`: no es una dirección (`Gonzalo`, `totales bolsas 0`): ningún pin es correcto.
- `skip`: verdad incierta, se registra pero no suma.

## Corpus

| archivo | fuente | qué mide |
|---|---|---|
| `corpora/tandil_planillas_reales.json` | 2 planillas reales (91 filas) | apellidos, typos, esquinas, pies de planilla |
| `corpora/oa_<iso>_*.json` | OpenAddresses (26 regiones) | verdad independiente de OSM + ruido 1–6 |
| `corpora/osm_<iso>_*.json` | nodos `addr:*` del índice (45 ciudades) | ruido 1–6; la fila limpia es optimista |
| `../*.json`, `../traps/*` | corpus históricos | CABA, Tandil, exports, homónimos |

Cada dirección base sale limpia + 1 variante por nivel 1–5 + hasta 4 del **nivel 6**
(planilla de despacho: apellido solo, truncada, typo fonético, número pegado, nota al
final, nombre adelante, mayúsculas). Regenerar:

```bash
.venv/bin/python scripts/build_regression_corpora.py --source all --manifest
```

Los corpus son **congelados**: regenerarlos cambia las claves y el diff contra un
snapshot viejo deja de ser comparable. Si se regeneran, se vuelve a sacar la base.
