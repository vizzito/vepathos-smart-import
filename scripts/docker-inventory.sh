#!/usr/bin/env bash
# Inventario de Docker: que hay despues del incidente de disco lleno.
# Correr cuando el daemon este arriba.
set -uo pipefail

if ! docker info >/dev/null 2>&1; then
  echo "El daemon de Docker no responde todavia. Abri Docker Desktop y esperá a que"
  echo "el icono deje de girar, despues volve a correr este script."
  exit 1
fi

echo "=== ESPACIO ==="
docker system df
echo
echo "=== IMAGENES (${1:-todas}) ==="
docker images --format "  {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}" | sort
echo
echo "=== VOLUMENES ==="
docker volume ls --format "  {{.Name}}" | sort
echo
echo "=== CONTAINERS ==="
docker ps -a --format "  {{.Names}}\t{{.Image}}\t{{.Status}}"
echo
echo "=== COMPOSE PROJECTS ==="
docker ps -a --filter "label=com.docker.compose.project" \
  --format "{{.Label \"com.docker.compose.project\"}}" | sort -u | sed 's/^/  /'
