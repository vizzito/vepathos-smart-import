#!/bin/sh
# Regenera el sqlite desde catalog.json (+ UNECE/GS1/libpostal/overrides).
# GeoNames: --always (build) o --if-missing (arranque). Offline salvo GeoNames.
set -e
OUT="${SMART_IMPORT_VOCAB_PATH:-/data/vocab/smart_import_vocab.sqlite}"
GEO="${SMART_IMPORT_LOCALITY_GEONAMES_PATH:-/data/vocab/locality_expand_geonames.json}"
WANT_GEO="${SMART_IMPORT_VOCAB_GEONAMES:-1}"
MODE="${1:---if-missing}"

mkdir -p "$(dirname "$OUT")" "$(dirname "$GEO")"
echo "vocab: sqlite ← catalog → $OUT"
python -m smart_import.vocab setup --out "$OUT"

run_geonames() {
    echo "vocab: GeoNames cities15000 → $GEO"
    python -m smart_import.vocab setup --out "$OUT" --geonames --geonames-out "$GEO"
}

if [ "$WANT_GEO" != "1" ]; then
    echo "vocab: GeoNames omitido (SMART_IMPORT_VOCAB_GEONAMES=$WANT_GEO)"
    exit 0
fi
if [ "$MODE" = "--always" ]; then
    run_geonames
elif [ "$MODE" = "--if-missing" ] && [ ! -s "$GEO" ]; then
    run_geonames || echo "vocab: GeoNames no disponible (sin red); solo ciudades curadas"
fi
