# libpostal (enhancer on-demand)

`libpostal` parsea y normaliza direcciones internacionales. **No es un geocoder**:
no produce coordenadas. Smart Import lo usa —si esta— solo para descomponer una
direccion en componentes (`road`, `house_number`, `unit`, `suburb`, `postcode`,
`landmark`, ...).

En produccion se recomienda **encendido**: imagen `runtime-libpostal` +
`SMART_IMPORT_LIBPOSTAL_ENABLED=true`. El heuristico siempre corre; libpostal
es un *agregado* que el gate consulta solo cuando falta `road` o la road es
sospechosa (parcela, manzana, lote…). Con la flag en `false` no se considera.

## Por que es un target Docker aparte

- es una libreria C, no un paquete Python puro
- descarga ~2 GB de datos de entrenamiento la primera vez
- agrega varios minutos al build de la imagen
- en runtime, los modelos se cargan la **primera** vez que el gate lo llama (~1–2 GB RAM), no en cada linea LatAm

`SMART_IMPORT_TARGET` es el **build stage** del Dockerfile (`runtime` |
`runtime-libpostal`). No es un modo magico: la flag sola, sin esa imagen,
termina en `libpostal=off (flag on pero libreria ausente)`.

## Como se integra en el flujo

```
texto
  → HeuristicAddressParser          (siempre)
  → gate.needs_enhancement?         (missing_road | suspicious_road)
       no  → listo
       si  → LibpostalAddressParser  (solo si SMART_IMPORT_LIBPOSTAL_ENABLED=true
                                      y la libreria esta instalada)
```

No se elige "estrategia libpostal". Se enciende o apaga el enhancer.

## Instalacion local (macOS)

```bash
brew install libpostal
pip install -e ".[libpostal]"          # binding Python `postal`
```

Verificar:

```bash
python -c "from postal.parser import parse_address; print(parse_address('Av. Corrientes 100'))"
```

## De donde salen los datos

libpostal **no llama a un API externo** al parsear. En el install/build baja
~2 GB de *model files* (tablas CRF entrenadas sobre OSM + OpenAddresses) a un
datadir (`/usr/local/share/libpostal` en brew, `/opt/libpostal-data` en Docker).

## Instalacion en Docker (prod)

Targets existentes (no hay `ai` / `ai-libpostal`):

| Target | Contenido |
|---|---|
| `runtime` | API + extraccion + geocoder bindings; sin libpostal (~370 MB) |
| `runtime-libpostal` | lo mismo + lib C + model data |

```bash
# 1) .env
SMART_IMPORT_TARGET=runtime-libpostal
SMART_IMPORT_LIBPOSTAL_ENABLED=true

# 2) rebuild (la 1ra vez baja ~2 GB de model data; despues queda en cache)
docker compose build smart-import

# 3) recrear
docker compose up -d --force-recreate smart-import

# 4) verificar
curl -s localhost:8100/health | python -m json.tool
# capabilities.libpostal=true, extraction.address_parser=enhanced
docker logs -f vepathos-smart-import
# ADDRESS  parser=enhanced  libpostal=on-demand …
```

Sin rebuild, aunque la flag este en `true`, vas a ver:
`libpostal=off (flag on pero libreria ausente)`.

`SMART_IMPORT_ADDRESS_PARSER=libpostal` queda reservado para
`benchmark-extraction` (medir libpostal puro). En produccion no hace falta.

## Cuando se consulta (gate)

| situacion | ¿llama libpostal? |
|---|---|
| `Av. Corrientes 100` / `1171 1st Ave` / `Av. Paulista, 1578` | **no** |
| sin `road` (p.ej. Flat + suburb + pincode India) | **si** |
| `road` sospechosa (`Plot No`, `Manzana`, `Casa` sola) | **si** |
| flag `false` o libreria ausente | **nunca** |

## Tests

```bash
pytest -q tests/test_libpostal_gate.py tests/test_world_addresses.py -s -k resumen
# si hay bottle local:
pytest -q tests/test_libpostal_value.py -m libpostal -s
```
