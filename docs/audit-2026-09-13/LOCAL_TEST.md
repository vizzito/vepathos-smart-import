# Validación local — 2026-09-13

La API está ejecutándose en `http://127.0.0.1:18101`, modo embedded, con almacenamiento temporal independiente. No usa Redis/RabbitMQ ni credenciales de producción. El puerto 18100 pertenece a un túnel SSH existente y no fue modificado.

- Swagger interactivo: http://127.0.0.1:18101/docs
- Salud: http://127.0.0.1:18101/health
- Resultado de la prueba: http://127.0.0.1:18101/imports/imp_6e2ecfecd941/preview
- Archivo de ejemplo: `/tmp/smart-import-local-de45nily/sample.csv`
- Datos y reporte del smoke test: `/tmp/smart-import-local-de45nily/`

## Prueba HTTP ejecutada

Carga de CSV con dos direcciones → normalización → geocoding → estado completed → descarga CSV y JSON nested. Las dos coordenadas coinciden con los puntos esperados del índice sintético. Bbox malformado devuelve HTTP 422. Health y descargas devuelven 200; upload devuelve 201.

Para repetir en Swagger: `POST /imports`, subir el CSV de ejemplo y copiar `job_id`. Luego `POST /imports/{job_id}/geocode`, con `index=test.sqlite`, `origin_lat=-34.60`, `origin_lon=-58.38`. Consultar el job y descargar el resultado.

**El índice de esta instancia es sintético y pequeño**, diseñado para pruebas controladas. No representa cobertura geográfica general. La normalización sí puede probarse con otros archivos. Es un proceso temporal, no un servicio persistente que arranque tras reiniciar el equipo.

## Tests sobre el estado actual

- Suite habitual + contrato histórico del adaptador: **1.750 passed**, 3 skipped, 50 deselected; 28,79 s.
- Se ejecutaron después los 50 casos excluidos (`real_geo or libpostal`): **50 passed**, 51,75 s. Incluyen índices/corpus reales disponibles localmente y libpostal.
- SDK MCP 2.2.0: **13 passed**, incluido transporte stdio real y cancelación; 1,50 s.
- Redis efímero local por socket Unix: **1 passed**, 0,20 s.

Los 3 skips de la primera corrida corresponden a las dos pruebas de protocolo MCP y la integración Redis, que se ejecutaron satisfactoriamente en sus entornos específicos. El conjunto MCP incluye también casos que ya estaban en la suite habitual; no sumar sus 13 resultados como tests únicos adicionales.

No hubo fallos. Permanecen dos avisos de deprecación de Starlette/TestClient. Aprobar los umbrales de los corpus existentes no equivale a geolocalizar cualquier dirección con exactitud: aún corresponde validar el volumen, cobertura y requisitos operativos del despliegue.

No se desplegó ni modificó producción. Los pasos de actualización coordinada, claves por cliente y conectividad privada están en `FIXES.md`.
