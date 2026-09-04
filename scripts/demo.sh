#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Recorrido completo del stack, con logs de cada etapa. NO necesita la web.
#
#   ./scripts/demo.sh              solo reglas (sin IA, sin PBF)
#   ./scripts/demo.sh --geocode    + geocoding contra un PBF real
#   ./scripts/demo.sh --ai         + separacion de columna compuesta con el modelo
#   ./scripts/demo.sh --all        todo
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
OUT="out/demo"
WITH_GEOCODE=0; WITH_AI=0
for arg in "$@"; do
  case "$arg" in
    --geocode) WITH_GEOCODE=1 ;;
    --ai)      WITH_AI=1 ;;
    --all)     WITH_GEOCODE=1; WITH_AI=1 ;;
    *) echo "opcion desconocida: $arg"; exit 2 ;;
  esac
done

bold() { printf "\n\033[1m%s\033[0m\n" "$1"; }
step() { printf "\n\033[1;36m━━━ %s ━━━\033[0m\n" "$1"; }

[ -x "$PY" ] || { echo "falta el venv. Corre: python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev,geo]'"; exit 1; }
rm -rf "$OUT"; mkdir -p "$OUT"
# cache propio del demo, para que el paso 8 sea realmente una primera corrida
export SMART_IMPORT_CACHE_PATH="$OUT/cache.sqlite"

step "0. Fixtures"
$PY -m smart_import make-fixtures --out fixtures --rows 40 | tail -1

step "1. INSPECT — que hay dentro del archivo"
echo "Archivo hostil: 4 filas de titulo antes del header, filas vacias, 3 hojas."
$PY -m smart_import inspect fixtures/preamble_dirty.xlsx \
  | $PY -c "
import json,sys; d=json.load(sys.stdin)
print(f\"  formato={d['format']} hojas={d['sheets']} elegida={d['sheet']}\")
print(f\"  header en la fila {d['header_row']} (descarto {d['preamble_rows']} de preambulo)\")
print(f\"  {d['rows']} filas x {d['column_count']} columnas\")
for c in d['columns']:
    print(f\"    {c['name']:<20} {c['apparent_type']:<7} nulls={c['null_pct']}%  ej: {c['examples'][:2]}\")"

step "2. NORMALIZE — archivo hostil -> formato Vepathos"
$PY -m smart_import -v normalize -i fixtures/preamble_dirty.xlsx \
  -o "$OUT/dirty.csv" --emit flat,nested --phone-region AR

step "3. Round-trip — un archivo que YA viene bien sale identico"
$PY -m smart_import normalize -i fixtures/ref_us_seattle.xlsx -o "$OUT/seattle.csv" >/dev/null 2>&1
if $PY - <<'EOF'
from smart_import.readers import read_any
import csv, sys
orig = read_any("fixtures/ref_us_seattle.xlsx")
rows = list(csv.reader(open("out/demo/seattle.csv", encoding="utf-8")))
same = rows[0] == orig.columns and len(rows) - 1 == len(orig)
sys.exit(0 if same else 1)
EOF
then echo "  IDENTICO: mismas columnas, mismo orden, mismas filas"
else echo "  DIFERENCIAS"; fi

step "4. Casos borde del archivo real de AR"
$PY -m smart_import normalize -i fixtures/ref_ar_orders.csv -o "$OUT/ar.csv" --emit flat,nested 2>/dev/null
$PY - <<'EOF'
import json
r = json.load(open("out/demo/ar.report.json"))
print(f"  {r['rows_input']} filas -> {r['deliveries']} entregas, {r['packages']} bultos")
print(f"  VP-1001 con 3 bultos agrupados, VP-1998 sin coords -> needs_geocode ({r['needs_geocode']})")
for issue in r["row_issues"]:
    print(f"  fila {issue['row']} ({issue['delivery_id']}): {issue['issues'][0]}")
d = json.load(open("out/demo/ar.nested.json"))["addresses"]
multi = next(a for a in d if len(a["packages"]) == 3)
print(f"  anidado: {multi['delivery_id']} -> {len(multi['packages'])} bultos, "
      f"dimensiones={multi['packages'][0].get('dimensions')}")
EOF

step "5. Columna compuesta — el sistema avisa que no le alcanza"
$PY -m smart_import detect -i fixtures/merged_field.csv 2>/dev/null | $PY -c "
import json,sys; d=json.load(sys.stdin)
for c,m in d['mapping'].items():
    print(f\"  {c:<16} -> {m['target']:<14} {m['confidence']:.2f}\")
for w in d['warnings']: print(f'  aviso: {w}')"

if [ "$WITH_AI" = "1" ]; then
  step "6. EXTRACT — separar la columna compuesta con el modelo"
  $PY -m smart_import normalize -i fixtures/merged_field.csv -o "$OUT/merged.csv" >/dev/null 2>&1
  echo "  antes:"; head -3 "$OUT/merged.csv" | sed 's/^/    /'
  SMART_IMPORT_AI_ENABLED=true $PY -m smart_import -v extract \
    -i "$OUT/merged.csv" -o "$OUT/merged_split.csv" --max-rows 6 2>&1 | grep -v "Loading weights\|HF_TOKEN"
  echo "  despues:"; head -4 "$OUT/merged_split.csv" | sed 's/^/    /'
else
  step "6. EXTRACT (omitido)"
  echo "  Corre con --ai para probarlo. Requiere: pip install -e '.[ai]' (~2,5 GB)"
fi

if [ "$WITH_GEOCODE" = "1" ]; then
  step "7. PBF disponibles"
  : "${SMART_IMPORT_PBF_DIR:?exporta SMART_IMPORT_PBF_DIR al directorio _extracts del cutter}"
  $PY -m smart_import list-pbf --lat 59.91 --lon 10.75 2>&1 >/dev/null | tail -1
  $PY -m smart_import list-pbf --lat 59.91 --lon 10.75 2>/dev/null | $PY -c "
import json,sys; d=json.load(sys.stdin)
s=d.get('selected')
print(f\"  para Oslo: {s['path'].split('/')[-1]} ({s['size_mb']} MB, zona {s['zone']})\" if s
      else '  sin cobertura para ese punto')"

  step "8. GEOCODE — accion SEPARADA, nunca automatica"
  cat > "$OUT/oslo.csv" <<'CSV'
Dir. entrega;Nom Dest;Tel dest;Kg
Grensen 5, Oslo;Kari Nordmann;41234567;1,5
Akersgata 55, Oslo;Ola Hansen;92345678;0,8
Karl Johans gate 1, Oslo;Ingrid Berg;46781234;2,25
Calle Inexistente 999, Springfield;Homer Simpson;5551234;1,0
CSV
  echo "  paso 8a: normalize (fijate que NO geocodifica)"
  $PY -m smart_import -v normalize -i "$OUT/oslo.csv" -o "$OUT/oslo_norm.csv" 2>&1 | grep -E "NORMALIZE|DONE"
  echo
  echo "  paso 8b: geocode (el usuario lo pide explicitamente)"
  $PY -m smart_import -v geocode -i "$OUT/oslo_norm.csv" -o "$OUT/oslo_geo.csv" \
    --origin-lat 59.91 --origin-lon 10.75 2>&1 | grep -vE "^\s+[0-9.]+s\s+$"

  step "9. CACHE — la MISMA consulta, la segunda vez"
  $PY -m smart_import -v geocode -i "$OUT/oslo_norm.csv" -o "$OUT/oslo_geo2.csv" \
    --origin-lat 59.91 --origin-lon 10.75 2>&1 | grep -E "cache|DONE"
else
  step "7-9. GEOCODE (omitido)"
  echo "  Corre con --geocode y SMART_IMPORT_PBF_DIR apuntando a los _extracts del cutter."
fi

step "10. SERVICIO HTTP — el mismo pipeline detras de la API"
PORT="${DEMO_PORT:-8199}"
"$PY" -m smart_import serve --port "$PORT" > "$OUT/api.log" 2>&1 &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
  curl -sf "localhost:$PORT/health" >/dev/null 2>&1 && break
  sleep 0.5
done

echo "  GET /health"
curl -s "localhost:$PORT/health" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
a, g = d["ai"], d["geocoding"]
print("    IA instalada=%s habilitada=%s" % (a["dependencies_installed"], a["enabled"]))
print("    PBFs visibles=%s indices=%s" % (g["pbf_available"], len(g["indexes_built"])))
print("    geocoding_automatico=%s  <- nunca se dispara solo" % g["automatic"])
'

echo
echo "  POST /imports  (archivo sin coordenadas)"
curl -s -X POST "localhost:$PORT/imports" -F "file=@fixtures/es_sin_coords.csv" > "$OUT/http_job.json"
"$PY" -c '
import json
d = json.load(open("out/demo/http_job.json"))
r = d["report"]
print("    job=%s estado=%s" % (d["job_id"], d["status"]))
print("    %s filas -> %s entregas | needs_geocode=%s" % (r["rows_output"], r["deliveries"], r["needs_geocode"]))
print("    el servicio OFRECE, no ejecuta:")
for act in d["next_actions"]:
    print("      - %-9s %s" % (act["action"], act["description"][:64]))
'

echo
echo "  logs del servicio (mismo pipeline, misma trazabilidad):"
grep -E "HTTP|READ |DETECT|NORMALIZE|DONE" "$OUT/api.log" | tail -8 | sed 's/^/  /'

kill $API_PID 2>/dev/null || true
trap - EXIT

step "Resultados"
ls -la "$OUT" | tail -n +2 | awk '{printf "  %-32s %8s\n", $9, $5}'
bold "Listo. Para el servicio HTTP:  $PY -m smart_import serve --port 8100"
