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
    keep_cities_txt
}

# El generador deja cities15000.txt en data/geonames, que en el build es una
# cache mount y NO queda en la imagen. Sin el .txt, `lookup_city_centroid`
# devuelve None en silencio: se pierde "la ciudad manda sobre el pin del depot"
# y la deteccion de localidad del archivo. Copiarlo al lado del JSON lo deja en
# una capa real de la imagen (el JSON derivado no trae lat/lon ni ISO).
keep_cities_txt() {
    src="${SMART_IMPORT_GEONAMES_SRC:-/app/data/geonames/cities15000.txt}"
    dst="$(dirname "$GEO")/cities15000.txt"
    if [ -s "$src" ]; then
        cp -f "$src" "$dst" && echo "vocab: cities15000.txt → $dst"
    else
        echo "vocab: cities15000.txt no encontrado en $src (sin centroides de ciudad)"
    fi
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
