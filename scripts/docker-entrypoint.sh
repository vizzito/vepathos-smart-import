#!/bin/sh
# Cada arranque: refresca sqlite del catálogo montado/empaquetado.
# GeoNames solo si falta el JSON (el build ya lo dejó en /data/vocab).
set -e
/app/scripts/docker-vocab-setup.sh --if-missing
exec python -m smart_import "$@"
