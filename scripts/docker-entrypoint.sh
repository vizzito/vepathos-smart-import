#!/bin/sh
# Cada arranque: refresca sqlite del catálogo montado/empaquetado.
# GeoNames solo si falta el JSON (el build ya lo dejó en /data/vocab).
# Alias OSM: si el volume de cache está vacío, sembrar el sqlite versionado.
set -e
/app/scripts/docker-vocab-setup.sh --if-missing
PACKAGED="${SMART_IMPORT_STREET_ALIASES_PACKAGED:-/app/smart_import/resources/street_aliases.sqlite}"
DEST="${SMART_IMPORT_STREET_ALIASES:-/data/cache/street_aliases.sqlite}"
if [ -s "$PACKAGED" ]; then
    mkdir -p "$(dirname "$DEST")"
    if [ ! -s "$DEST" ] || [ "$PACKAGED" -nt "$DEST" ]; then
        cp -f "$PACKAGED" "$DEST"
        echo "street-aliases: seeded $DEST from image"
    fi
fi
exec python -m smart_import "$@"
