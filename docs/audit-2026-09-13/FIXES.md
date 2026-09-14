# Correcciones implementadas — Smart Import

> Validación posterior completa y API local disponible: [LOCAL_TEST.md](LOCAL_TEST.md). Incluye los 50 casos real_geo/libpostal que inicialmente se habían excluido.

Cambios locales posteriores a la auditoría de `19bee4f`. No se desplegó, no se modificaron secretos y no se accedió a servicios de producción. `REVIEW.md` conserva los hallazgos y mediciones de la base anterior.

## Comportamiento corregido

- **H01–H02, caché y confianza:** la identidad incluye origen/bbox exactos, generación del índice y parámetros de matching. La versión 6 invalida entradas anteriores. Se conserva `soft_reject` en SQLite y `geocode_requires_review` en el CSV; `/issues` respeta el techo de revisión.
- **H03, remapping:** un normalize exitoso incrementa la revisión e invalida las coordenadas, progreso y reporte geográfico anteriores. Preview, download y acciones comprueban vigencia. Un nested que no pudo regenerarse deja de ofrecerse.
- **H04/H11, entradas geográficas:** rangos WGS84, finitud, par completo, bbox ordenado y radio positivo. El nombre del índice se resuelve dentro del directorio configurado, incluyendo protección contra symlinks que escapan. Se rechazan rutas absolutas; no se soporta bbox que cruza el antimeridiano.
- **H05, admisión:** mapping reclama atómicamente la operación y respeta la cola. El executor geográfico embedded tiene capacidad limitada.
- **H06–H07, parsers:** profundidad JSON ≤100, presupuesto lineal de búsqueda de recuperación, límite sobre filas expandidas y búsqueda de columnas con set. La reparación protege cadenas antes de tocar tokens numéricos. XLSX se limita a 256 MiB expandidos, 10.000 miembros y 1.024 columnas; XLS deja de materializar toda la matriz y libera recursos.
- **H08, concurrencia:** token único por intento, locks no reentrantes, renew/release y save condicionales mediante Lua. Los artefactos de workers viven en directorios por intento, el transporte verifica el token, y el estado solo publica la referencia mientras conserva el lease. También se protegen escrituras del watchdog. Mensajes de operaciones reemplazadas se descartan.
- **H09, contadores:** las filas descartadas por evidencia insuficiente cuentan como pendientes; el progreso avanza también en caminos que usan `continue`.
- **H10, transferencia:** 408/429/5xx son transitorios, con Retry-After acotado y backoff con jitter. El worker propaga fallos transitorios de artefactos para que la cola reintente. Las descargas son streaming, con límite de bytes, deadline y limpieza de parciales.
- **H12, uploads:** la reserva, Content-Length, bytes recibidos y timeout de inactividad se comprueban en ASGI antes del parser multipart. Las escrituras finales utilizan temporales únicos y renombre atómico.
- **H13, direcciones:** NFC evita cortar calles con tildes descompuestas; cláusulas de piso/departamento dejan de competir como candidatos de calle/altura.
- **H14, acceso:** autenticación Bearer configurable con claves por tenant y comprobación de propiedad en listado, lectura y acciones. `/internal` conserva su token independiente. El overlay deja de publicar una IP pública por defecto.
- **H15, privacidad:** coordenadas eliminadas de INFO en los puntos encontrados; errores públicos y reintentos usan códigos/tipos, sin texto crudo de excepción. Errores de validación HTTP omiten valores de entrada. `.env*` queda excluido del contexto Docker.
- **H16, recursos:** caché con TTL positivo de 30 días, negativo de 1 día y purga a 500.000 entradas (con margen de hasta 999 inserciones entre purgas); muestras ≤50; cierre ante errores de inicialización; cliente HTTP inicializado bajo lock; lifespan espera tareas/executor antes de liberar recursos. `osmium extract` tiene timeout de 900 segundos.

La implementación sigue siendo un geocoder OSM local. No se agregaron proveedores pagos, URLs elegidas por el cliente ni precisión inventada cuando faltan datos.

## MCP utilizable por stdio

Esquema exacto anunciado: [mcp-smart-input.schema.json](mcp-smart-input.schema.json).

Nuevo extra opcional y entry point:

```sh
pip install -e '.[mcp]'
smart-import-mcp
```

El proceso usa **MCP SDK 2.2**, anuncia `smart_input` con input/output schema, valida tipos estrictamente y devuelve `isError` más un error semántico. No importa ni arranca la API FastAPI. No escucha por HTTP.

Ejemplo de argumentos:

```json
{
  "address": "Av. Corrientes 1234",
  "city": "Buenos Aires",
  "country": "AR",
  "geocode": true,
  "origin": {"lat": -34.60, "lon": -58.38}
}
```

Sin `geocode=true`, solo normaliza. Geocodificar exige origen, `SMART_IMPORT_INDEX_DIR` y `SMART_IMPORT_MCP_INDEX` (nombre relativo de un SQLite ya construido). No construye índices durante la llamada. Se usan los umbrales de `Config`; toda coordenada aproximada se devuelve como `review`. El score es similitud heurística, no probabilidad ni exactitud métrica.

Cada consulta corre en un proceso aislado: 10 segundos de deadline y máximo 2 consultas activas. Timeout/cancelación mata y recoge el proceso; saturación devuelve `BUSY`. Es una operación de lectura, sin jobs ni persistencia, por lo que repetirla no duplica escrituras. Resultados pueden cambiar al actualizar el índice/configuración. No usa caché persistente, evitando efectos de estado en este contrato.

`mcp_adapter_proposal.py` se conserva como propuesta histórica para un contrato asincrónico basado en jobs. El servidor implementado usa un contrato **stateless de una dirección**; no anuncia polling ni idempotencia durable inexistentes.

## Pasos de despliegue necesarios

1. Drenar la cola y detener workers antiguos antes de actualizar API y workers juntos: el fencing completo necesita que ambos comprendan el token de intento. La ruta interna todavía acepta clientes legacy sin token de ejecución para compatibilidad; no garantiza fencing para esos clientes.
2. Verificar que workers alcanzan la API por la red privada. El overlay ya no incluye el bind público anterior. Si hace falta acceso remoto, usar un gateway autenticado y TLS; no aplicar el overlay sin verificar conectividad.
3. Para aislamiento de clientes, definir `SMART_IMPORT_API_KEYS` como objeto JSON **secreto → tenant**, con claves ASCII de al menos 32 caracteres. Cada cliente envía `Authorization: Bearer ...`. Un mismo tenant puede tener varias claves para rotación. Sin esta variable se conserva el modo interno de confianza única; no considerarlo autenticación habilitada.
4. Los jobs antiguos tienen tenant vacío y no se asignan automáticamente a un cliente. Drenarlos/descargarlos antes de activar aislamiento, o realizar una migración explícita de propiedad. No hay login de navegador incluido: el gateway debe autenticar e inyectar la identidad o el cliente debe enviar Bearer.
5. Prever caché fría por el cambio de versión y validar un lote representativo en staging. Respetar el TTL de datos y ajustar la política de retención a la organización; borrar un job no elimina inmediatamente la caché compartida.
6. Para MCP stdio, el host local autoriza el proceso. Un futuro transporte MCP remoto necesita su propia autenticación/autorización de transporte; este cambio no lo expone.

## Validación y límites

Las pruebas usan archivos, índices y direcciones sintéticas. Se ejecutan sin proveedores geográficos externos. El SDK MCP se instaló solamente en `/tmp` para verificar tanto cliente en proceso como transporte stdio real; el entorno del proyecto no se actualizó.

Resultados: suite completa **1.749 passed, 3 skipped, 50 deselected** (29,72 s); las omisiones corresponden a 2 tests MCP sin el extra y 1 integración Redis opt-in, además de los 50 casos de datos geográficos/libpostal excluidos. **13 tests MCP pasaron con SDK 2.2.0**, incluyendo stdio real y cancelación. **1 test Redis real pasó**. Tras esa corrida se añadió y aprobó además el caso HTTP de aislamiento de artefactos y rechazo de lease vencido. `git diff --check` sin errores.

Comandos reproducibles:

```sh
.venv/bin/python -m pytest -q -m 'not real_geo and not libpostal' tests docs/audit-2026-09-13/test_adapter_contract.py
RUN_LOCAL_REDIS_TESTS=1 .venv/bin/python -m pytest -q tests/test_redis_fencing_integration.py
# Con el extra MCP instalado:
.venv/bin/python -m pytest -q tests/test_mcp_service.py
```

El test Redis arranca una instancia efímera por socket Unix, sin abrir un puerto TCP ni leer configuración de producción. Requiere `redis-server` local. No se verificaron RabbitMQ real, precisión sobre datos operativos, p95/p99, libpostal, firewall, TLS ni carga de una flota real. El worker batch sigue usando threads: al perder el lease no puede publicar, pero código nativo bloqueado puede requerir reinicio supervisado; solo la consulta MCP implementa cancelación dura del proceso.
