# Ejemplos para probar a mano

- **Entradas que la web ya acepta** (completas o mínimas): [`vepathos-golden/`](vepathos-golden/README.md)
- **Entradas sintéticas hostiles** (headers raros, latin1, sin coords, etc.): los archivos de esta carpeta

Casos variables listos para CLI / curl. Se regeneran (no editar a mano):

```bash
.venv/bin/python -m smart_import make-fixtures --out examples --rows 40
```

Los `ref_*` de `fixtures/` son archivos “ya bien formados” (round-trip). El resto vive acá.

| Archivo | Qué ejercita | Salida esperada |
|---|---|---|
| `es_headers_raros.xlsx` | Headers AR abreviados (`Dest.`, `Kg`, `Cant bultos`) | mapping alto, flat Vepathos |
| `es_sin_coords.csv` | Sin lat/lng | `needs_geocode = 100%` → botón geocode |
| `en_weird.csv` | Headers EN raros | mapping por alias/fuzzy |
| `semicolon_latin1.csv` | `;` + cp1252 + coma decimal | lectura + números OK |
| `pipe_delimited.txt` | Delimitador `\|` | lectura OK |
| `tabs.tsv` | TSV | lectura OK |
| `preamble_dirty.xlsx` | Título + 4 filas basura + 3 hojas | elige hoja `Datos`, salta preámbulo |
| `swapped_coords.csv` | lat/lng invertidas | **aviso**, no corrige solo |
| `one_row_per_delivery.xlsx` | `cantidad de bultos` sin `package_id` | expande a N bultos |
| `merged_field.csv` | Nombre+dir+tel en una columna | aviso; `extract` si IA on |
| `no_headers.csv` | `Campo 1..6` | solo heurísticas de contenido |
| `mixed_locale.xlsx` | AR + US mezclados | ambas locales |
| `legacy.xls` | Excel BIFF viejo | lectura OK |

## Cómo probar uno

```bash
cd ~/workspace/vepathos-smart-import
FILE=examples/es_sin_coords.csv

.venv/bin/python -m smart_import inspect "$FILE"
.venv/bin/python -m smart_import -v normalize -i "$FILE" -o out/ejemplo.csv --emit flat,nested

# o por HTTP (servicio arriba en :8100):
curl -s -X POST "localhost:8100/imports?phone_region=AR" -F "file=@$FILE" | .venv/bin/python -m json.tool
```

Smoke HTTP completo (captura `job_id`, preview, download, geocode):

```bash
./scripts/http-smoke.sh examples/es_sin_coords.csv
```
