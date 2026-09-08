#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Nivel 4 del RUNBOOK: contrato HTTP de punta a punta.
# Captura el job_id de verdad (no uses imp_xxxxxxxx).
#
#   ./scripts/http-smoke.sh                         # default: examples/es_sin_coords.csv
#   ./scripts/http-smoke.sh examples/merged_field.csv
#   ./scripts/http-smoke.sh --geocode examples/es_sin_coords.csv
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
HOST="${SMART_IMPORT_HOST:-localhost:8100}"
DO_GEOCODE=0
FILE=""

for arg in "$@"; do
  case "$arg" in
    --geocode) DO_GEOCODE=1 ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) FILE="$arg" ;;
  esac
done
FILE="${FILE:-examples/es_sin_coords.csv}"

[ -x "$PY" ] || { echo "falta .venv. Corre: python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev,geo]'"; exit 1; }
[ -f "$FILE" ] || {
  echo "no existe: $FILE"
  echo "regenerá ejemplos:  $PY -m smart_import make-fixtures --out examples --rows 40"
  exit 1
}

json() { "$PY" -m json.tool; }

echo "━━━ 0. health ━━━"
HEALTH=$(curl -sf "http://$HOST/health") || {
  echo "servicio caído en $HOST"
  echo "levantalo:  export SMART_IMPORT_PBF_DIR=~/workspace/route-optimizer-app/data/_extracts"
  echo "            .venv/bin/python -m smart_import serve --port 8100"
  exit 1
}
echo "$HEALTH" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
c, g = d["capabilities"], d["geocoding"]
print("  status=%s  schemas=%s" % (d["status"], d["schemas"]))
print("  caps: normalize=%s geocoding=%s libpostal=%s rules=%s" % (
    c.get("normalize"), c.get("geocoding"), c.get("libpostal"), c.get("rules")))
print("  GEO: pbf_available=%s indexes=%s automatic=%s fallback=%s" % (
    g["pbf_available"], len(g["indexes_built"]), g["automatic"], g["fallback"]))
'

echo
echo "━━━ 1. POST /imports  file=$FILE ━━━"
RESP=$(curl -sf -X POST "http://$HOST/imports?phone_region=AR" -F "file=@${FILE}")
echo "$RESP" | json | head -40
JOB=$(echo "$RESP" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')
STATUS=$(echo "$RESP" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["status"])')
NEEDS=$(echo "$RESP" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["report"].get("needs_geocode", 0))')
ACTIONS=$(echo "$RESP" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(", ".join(a["action"] for a in d["next_actions"]))')
echo
echo "  JOB=$JOB  status=$STATUS  needs_geocode=$NEEDS"
echo "  next_actions: $ACTIONS"

echo
echo "━━━ 2. GET /imports/\$JOB ━━━"
curl -sf "http://$HOST/imports/$JOB" | json | head -25

echo
echo "━━━ 3. GET preview ━━━"
curl -sf "http://$HOST/imports/$JOB/preview?limit=3" | json | head -40

echo
echo "━━━ 4. download flat + nested ━━━"
mkdir -p out/http-smoke
curl -sf "http://$HOST/imports/$JOB/download?format=flat"   -o "out/http-smoke/${JOB}.csv"
curl -sf "http://$HOST/imports/$JOB/download?format=nested" -o "out/http-smoke/${JOB}.nested.json"
echo "  flat:   out/http-smoke/${JOB}.csv"
echo "  nested: out/http-smoke/${JOB}.nested.json"
echo "  columnas flat: $(head -1 "out/http-smoke/${JOB}.csv")"

if [ "$DO_GEOCODE" = 1 ]; then
  echo
  echo "━━━ 5. coverage BA ━━━"
  curl -sf "http://$HOST/geocoding/coverage?lat=-34.60&lon=-58.38" | json

  if [ "$NEEDS" = "0" ]; then
    echo "  este archivo ya tiene coords (needs_geocode=0); geocode no aplica."
  else
    echo
    echo "━━━ 6. POST geocode (async) ━━━"
    curl -sf -X POST "http://$HOST/imports/$JOB/geocode?origin_lat=-34.60&origin_lon=-58.38" | json | head -20
    echo
    echo "  polling (sin watch; Ctrl-C para salir)…"
    for _ in $(seq 1 60); do
      LINE=$(curl -sf "http://$HOST/imports/$JOB" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
g = d.get("geocode") or {}
p = g.get("progress") or {}
print("%s phase=%s %s/%s" % (
    d["status"], p.get("phase", "-"), p.get("done", "-"), p.get("total", "-")))
')
      echo "    $LINE"
      case "$LINE" in
        geocoded*|completed*|failed*|*"phase=done"*|*"phase=failed"*) break ;;
      esac
      sleep 2
    done
  fi
fi

echo
echo "Listo. JOB=$JOB"
echo "  Docs: http://$HOST/docs"
echo "  Re-correr con geocode:  ./scripts/http-smoke.sh --geocode $FILE"
