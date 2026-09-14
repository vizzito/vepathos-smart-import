# Auditoría de Smart Import: producción y MCP

> Informe histórico de la base `19bee4f`. Las correcciones locales posteriores, pruebas actuales y pasos de despliegue están en [FIXES.md](FIXES.md).

Fecha: 2026-09-13. Base revisada: `19bee4f`. Revisión local del repositorio; no se consultaron servicios de producción ni contenidos de archivos `.env`. Solo se agregaron artefactos bajo `docs/audit-2026-09-13/`; no se modificó lógica productiva.

## 1. Resumen ejecutivo

**Preparación para producción: 6/10. Preparación MCP actual: 3/10.** Son valoraciones de ingeniería sobre esta revisión, no porcentajes de exactitud ni certificaciones.

El servicio tiene una base sólida: separación explícita entre normalización y geocoding, geocoder local sin dependencia online, consultas SQLite parametrizadas, índices construidos de forma atómica, caché WAL, admisión, jobs, progreso, TTL y pruebas extensas. No propongo una reescritura ni incorporar un proveedor pago sin una medición que lo justifique.

Sí hay fallos reproducibles con impacto en entregas: una caché puede devolver coordenadas de otra área; un resultado dudoso puede volverse verde; un mapping corregido sigue mostrando coordenadas anteriores; y ciertas direcciones con piso/departamento o Unicode se parsean incorrectamente. También hay huecos concretos de protección contra cargas patológicas y exclusión de ejecuciones.

**Validación realizada:**

- Suite existente: **1.688 passed**, 50 deselected, 2 advertencias de deprecación; 30,52 s. Comando: `.venv/bin/python -m pytest -q -m 'not real_geo and not libpostal'`.
- Auditoría y contrato propuesto: **20 passed, 20 xfailed**, 2 advertencias; 1,12 s. Los `xfail(strict=True)` expresan comportamientos deseados que el código actual incumple, no correcciones aplicadas. Son 20 casos, algunos parametrizados, agrupados en 13 hallazgos reproducidos.
- El adaptador aporta 13 de los tests aprobados. Se verificaron modelos, errores, timeout y contratos de backend; **no se instaló el SDK MCP ni se ejecutó un handshake MCP**.
- Datos sintéticos locales para geocoding. No se midieron exactitud real, p95/p99, firewall, Redis/RabbitMQ reales, libpostal instalado ni comportamiento bajo carga distribuida.

**Orden que priorizaría:** H01/H02/H03, H05/H08, H06/H12, H04/H11 y H13. H09 es un arreglo pequeño que conviene incluir temprano. La exposición de red H14 debe verificarse antes de abrir MCP a clientes externos.

## 2. Hallazgos críticos, código afectado y corrección propuesta

Los fragmentos de solución son propuestas de integración. Donde se agregan campos o interfaces se indica explícitamente: no son parches ya instalados.

### H01 — Alta: caché independiente de bbox/origin y de la versión del índice

Ubicación: `smart_import/geocoding/runner.py:167` y `:315`.

```python
context = (f"{Path(index_path).stem}" ...)
cached = cache.get(query, context)
if cached is not None:
    return cached
result = geocoder.geocode(query, origin=origin, bbox=bbox)
```

Se usa el nombre base del índice y umbrales, pero no `origin` ni `bbox`. La misma query en un índice regional amplio reutiliza el resultado seleccionado para otro contexto. La geofence posterior puede rechazar el pin equivocado, pero no vuelve a buscar el candidato correcto. Con bbox y sin depot ni siquiera existe esa defensa.

**Reproducción:** misma dirección `Av. Corrientes 1234`, mismo índice y caché; primero bbox de Buenos Aires, luego bbox de Córdoba. La segunda llamada devuelve latitud **-34.6037**, cuando la búsqueda dentro de Córdoba debería dar **-31.4201**.

**Corrección:** incorporar la identidad inmutable del dataset y el contexto espacial en una serialización canónica; mantener los umbrales actuales sin redondear configuraciones diferentes a la misma clave.

```python
context = json.dumps({
    "index_path": str(Path(index_path).resolve()),
    "dataset_version": dataset_version,  # hash/version publicado al construir el índice
    "origin": origin,
    "bbox": bbox,
    "matching_settings": matching_settings,  # umbrales completos + parser + enhance
}, sort_keys=True, separators=(",", ":"), allow_nan=False)
```

Los dos últimos identificadores son nuevos datos que debe suministrar el constructor/configuración. `mtime_ns + size` puede servir como invalidación local económica; para compartir caché entre nodos conviene una versión de dataset estable. Incrementar `GEOCODER_VERSION` al desplegar. No hace falta cambiar SQLite por Redis para resolver esto.

### H02 — Alta: se pierde la obligación de revisión en caché y en `/issues`

Ubicación: `smart_import/geocoding/cache.py:97`, `smart_import/geocoding/runner.py:381`, `smart_import/geocoding/bands.py:98`, `smart_import/api/app.py:1000`.

```python
# Cache.get reconstruye solo una parte del resultado:
detail={"from_cache": True}
# El runner usa un dato que ya no está:
soft = bool(result.detail.get("soft_reject"))
# band_from_row vuelve a calcular sin force_review:
return band_for(..., precision=row.get("geocode_precision"))
```

Una coincidencia con calle incorrecta, score 0.91 y `soft_reject=True` es `review` en frío y **`valid` al recuperarla del caché**. Incluso sin caché, `/issues` recalcula esa fila como válida y deja de presentarla al operador. No es solo pérdida de información diagnóstica.

**Corrección:** persistir un booleano de dominio `requires_review` tanto en SQLite como en CSV/nested. No inferirlo únicamente del texto libre de `reason` ni del score.

```python
# Nuevas columnas requires_review (SQLite) y geocode_requires_review (CSV):
force_review = bool(result.detail.get("soft_reject"))
row["geocode_requires_review"] = "true" if force_review else "false"
# Al reconstruir la fila para /issues:
force_review = str(row.get("geocode_requires_review", "")).lower() == "true"
return band_for(status, confidence, has_coords=has_coords,
                valid_at=valid_at, review_at=review_at,
                force_review=force_review)
```

Al leer caché, restaurar el mismo dato en `detail`, o convertirlo en campo explícito de `GeocodeResult`. Migrar columna y versión. Para artefactos anteriores sin el flag, conservar de forma prudente los `street_mismatch` como revisión. Añadir igualdad de semántica entre respuesta fría, caliente, CSV, nested e issues.

### H03 — Alta: un remapping exitoso mantiene visible el geocode anterior

Ubicación: `smart_import/worker/handlers.py:105` y `smart_import/api/app.py:1127`.

```python
job.normalized_path = _publish(...)
job.nested_path = _publish(...)
# No se invalidan geocoded_path, geocode_report ni el artefacto viejo.
if artifacts.exists(job.id, GEOCODED, job.geocoded_path):
    return GEOCODED, job.geocoded_path
```

**Reproducción:** job normalizado con un CSV geocodificado previo que contiene `OLD_DESTINATION`; `PUT /mapping` devuelve 200, pero `/preview` continúa devolviendo `source=geocoded` y el destino anterior. Corregir una columna puede no corregir lo que el usuario descarga.

**Corrección recomendada:** versionar normalización y resultado derivado; publicar la nueva revisión atómicamente, e invalidar todos sus consumidores. Simplemente poner `job.geocoded_path=None` es insuficiente: `ArtifactStore.resolve` busca también la ruta canónica vieja.

```python
# Campos nuevos del Job; un geocode captura la revisión de entrada al iniciar.
job.normalized_revision += 1
job.geocoded_revision = None
job.geocoded_path = None
job.geocode_report = {}
job.geocode_progress = {}
job.error = None

def has_current_geocode(job):
    return (job.geocoded_path is not None
            and job.geocoded_revision == job.normalized_revision)
```

Aplicar ese predicado a preview, flat, nested, download explícito, issues, acciones y deduplicación de tareas. La solución mínima alternativa es borrar de forma segura todos los artefactos derivados tras normalizar y antes de anunciar la nueva salida; el backend de artefactos requiere una operación por tipo. En ambos diseños, serializar la mutación con H05/H08.

### H04 — Media: entrada geográfica inválida produce 500 o trabajo aceptado inválido

Ubicación: `smart_import/api/app.py:1181` y `:1232`.

```python
origin_lat: float | None = Query(None)
origin_lon: float | None = Query(None)
box = tuple(float(p) for p in parts)
```

`bbox=x,0,0,0` devuelve **500**. Latitud 91/longitud 181, `nan` e `inf` se aceptan con **202**. Tampoco se exige el par completo de coordenadas ni un radio finito positivo. Esto se convertiría en errores opacos o resultados inválidos para un agente.

**Corrección:** límites en Query/modelos; parseo de bbox con error de cliente; validar orden y antimeridiano explícitamente.

```python
origin_lat: float | None = Query(None, ge=-90, le=90, allow_inf_nan=False)
origin_lon: float | None = Query(None, ge=-180, le=180, allow_inf_nan=False)
max_distance_km: float | None = Query(None, gt=0, allow_inf_nan=False)

def parse_bbox(raw):
    try:
        n, s, e, w = map(float, raw.split(","))
    except (TypeError, ValueError):
        raise HTTPException(422, "bbox debe tener cuatro números") from None
    if not all(math.isfinite(x) for x in (n, s, e, w)):
        raise HTTPException(422, "bbox debe ser finito")
    if not (-90 <= s < n <= 90 and -180 <= w < e <= 180):
        raise HTTPException(422, "bbox fuera de rango o invertido; cruce de antimeridiano no soportado")
    return n, s, e, w
```

Repetir validación en el contrato de cola para evitar que solo la capa HTTP proteja al worker. Validar códigos de país contra el catálogo existente, no solo una regex ISO aparente.

### H05 — Alta: mapping compite con geocode y elude parte de la admisión

Ubicación: `smart_import/api/app.py:1084`.

```python
job = _job_or_404(job_id)
if broker is not None:
    await run_in_threadpool(_encolar, job, normalize_task(...))
else:
    async with _normalize_slot(job.id):
        ...
```

No hay claim por job ni rechazo `busy`, y en el camino distribuido no se aplica `_rechazar_si_la_cola_esta_llena`. Una petición de mapping sobre un job `geocoding` ejecuta la normalización y devuelve 200. En embedded son pools independientes: el CSV de entrada puede reescribirse mientras se lee. En distribuido se puede sobrescribir el estado desde la API aunque el worker tenga un lock de ejecución.

**Corrección:** unificar las reservas de operaciones mutantes por job y revisar la versión de entrada al publicar. Un `if job.busy` aislado sigue teniendo TOCTOU; el claim debe ser atómico.

```python
# Nueva interfaz común para ambos stores; transición mediante lock/Lua/CAS.
if not store.claim_operation(job_id, expected_revision, operation="normalize"):
    raise HTTPException(409, "El job está ocupado o cambió de revisión")
# Después: admisión de CPU/cola y rollback de la reserva si publicar falla.
```

No reemplazar esa interfaz por un mutex local cuando hay múltiples nodos. Se trata de reservar antes de mutar, no de aumentar los workers.

### H06 — Alta: JSON pequeño puede causar trabajo cuadrático; max_rows no limita los hijos

Ubicación: `smart_import/readers/json_reader.py:131`, `:295`, `:355`.

```python
for item in child:
    expanded.append(row)
if max_rows and len(expanded) >= max_rows:
    break

# Se recorre el sufijo completo ANTES de aplicar el límite del chunk:
end = _matching_brace(text, start)
if end < 0 or (end - start) > _MAX_ADDRESS_CHARS:
    ...
```

Un solo padre con 20 paquetes supera `max_rows=3` y produce 20 filas. Un JSON truncado con muchos inicios `{"address":` obliga a escanear casi el mismo sufijo repetidamente. Con solo 2.214 bytes se recorrieron 221.100 caracteres. Microbenchmark local: 5.514 bytes/0,0451 s; 11.014/0,1722 s; 22.014/0,6984 s. Es **DoS algorítmico cuadrático confirmado**, no una afirmación de ReDoS exponencial.

**Corrección de filas:** comprobar antes de agregar cada hijo, y reportar truncamiento o rechazar sin perder entregas silenciosamente.

```python
for item in child:
    if max_rows and len(expanded) >= max_rows:
        raise ValueError("La expansión de paquetes supera el límite de filas")
    row = dict(parent)
    row.update(_flatten(item))
    expanded.append(row)
```

**Corrección de recuperación:** scanner de una sola pasada consciente de strings/escapes, con límite global de trabajo, profundidad y candidatos. Un parche de contención puede pasar `scan_budget` al scanner y descontar cada carácter examinado; al agotarlo, rechazar con error semántico. Limitar solo el tamaño del chunk *después* de recorrerlo no sirve.

XLS/XLSX también requieren límites de expansión, columnas y contenido descomprimido: `read_only=True` no es una defensa general contra ZIP bombs; XLS materializa la matriz antes de recortar. Esos escenarios se identificaron por inspección, no se ejecutaron payloads grandes.

### H07 — Media: reparar JSON cambia texto válido dentro de comillas

Ubicación: `smart_import/readers/json_reader.py:196`.

```python
n_nf = len(_NONFINITE.findall(out))
if n_nf:
    out = _NONFINITE.sub("null", out)
```

La reparación aplica regex sobre todo el documento. Ante una coma final inválida, `"address":"Calle NaN 42."` se transforma en **`Calle null 42.`**. No afecta solo a números: también nombres, direcciones y referencias pueden modificarse. Otras sustituciones globales comparten el riesgo.

**Corrección:** tokenizar y reparar exclusivamente fuera de literales string; mantener bytes de strings intactos. Hasta implementar ese scanner, es preferible rechazar un documento irrecuperable a cambiar su dirección silenciosamente.

```python
# Contención segura: eliminar sustituciones globales sobre strings.
# JSON estricto puede mantenerse como opción de ingreso:
def reject_nonfinite(_token):
    raise ValueError("JSON contiene un número no finito")

doc = json.loads(raw, parse_constant=reject_nonfinite)
```

Este fragmento cambia la tolerancia a exports rotos y no debe desplegarse como reemplazo automático del modo resiliente. La solución compatible es conservar el salvamento pero proteger lexicalmente los strings.

### H08 — Alta: dos ejecuciones del mismo proceso pueden adquirir el mismo job

Ubicación: `smart_import/queue/consumer.py:107`, `:224`; `smart_import/jobs.py:claim_run`; `smart_import/job_store_redis.py:220`.

```python
self.holder = f"{socket.gethostname()}:{os.getpid()}"
ctx.store.claim_run(task.job_id, self.holder, ...)
# Si ya está tomado por el mismo holder, se permite volver a entrar.
```

Con `worker_slots=2` (default), mensajes duplicados o dos tareas distintas del mismo job pueden entrar simultáneamente al mismo worker. Ambas comparten holder, archivos y scratch. Una puede borrar el scratch al terminar mientras la otra lo usa. La reproducción obtiene **True dos veces** en memoria y Redis; no se afirmó haber reproducido una partición de red real.

Además, `renew_run` y `release_run` hacen GET seguido de SET/DELETE sin comparación atómica. Si vence el lock entre ambos comandos, pueden renovar o borrar el del nuevo dueño. `_Renovador` advierte que perdió el lock pero deja que el worker siga publicando.

**Corrección:** token único por *ejecución*, no por proceso ni únicamente por task_id; claim NX, renovación y liberación atómicas.

```python
execution_token = uuid.uuid4().hex
acquired = redis.set(key, execution_token, nx=True, px=ttl_ms)

RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
```

Para cerrar el caso de lease perdido, usar versión/fencing en publicación de artefactos y estado. La comparación debe ejecutarse donde se publica, no solo justo antes de enviar el HTTP. Scratch y temporales por execution_token; nunca compartir `*.partial` entre intentos. La marca de tarea completada debe asociarse a su propia revisión de resultado: `geocode_task_id` se guarda antes de correr y `bool(geocoded_path)` puede referirse a un resultado anterior.

### H09 — Media: filas sin evidencia desaparecen del contador residual

Ubicación: `smart_import/geocoding/runner.py:214` y `smart_import/worker/handlers.py:218`.

```python
report.skipped_low_confidence += 1
continue
# Después:
remaining = int(report.not_found or 0) + int(report.errors or 0)
```

Entrada `x`: fila sellada sin coordenadas, `skipped_low_confidence=1`, `not_found=0`. El worker puede anunciar `needs_geocode=0` aunque hay entregas pendientes. Los contadores terminales dejan de sumar el total.

**Corrección pequeña y concreta:**

```python
report.skipped_low_confidence += 1  # subconjunto diagnóstico
report.not_found += 1              # categoría terminal real
```

No volver a sumar skipped al total residual si se aplica esta corrección. Centralizar también el callback de progreso: los `continue` de filas con coordenadas, vacías o sin evidencia lo saltan.

### H10 — Media: 429 es permanente y geocode oculta errores transitorios a la cola

Ubicación: `smart_import/artifacts/http.py:204`; `smart_import/worker/handlers.py:231`.

```python
error = ArtifactRejected if codigo < 500 else ArtifactTransferError
# El handler geocode captura cualquier Exception, marca failed y retorna.
```

504 se reintenta en transferencia (control aprobado), pero 429 no: se clasifica como rechazo permanente. Esto aplica al intercambio API-worker; **no hay proveedor externo de mapas al que atribuir estos errores**. Si se agotan transferencias durante geocode, el handler devuelve normalmente y el consumidor puede hacer ACK sin activar la política de reintento de tareas.

**Corrección:** distinguir códigos transitorios, usar `Retry-After` acotado/backoff con jitter y un presupuesto total, y propagar el error transitorio hasta el consumidor.

```python
if codigo in (408, 429) or codigo >= 500:
    raise ArtifactTransferError(f"Transferencia transitoria: HTTP {codigo}")
if codigo >= 400:
    raise ArtifactRejected(f"Transferencia rechazada: HTTP {codigo}")
```

La clasificación debe acompañarse de idempotencia/fencing de publicación para reintentar PUT con seguridad. `httpx timeout=120` es por fases de I/O, no un deadline total del job; tres intentos no constituyen una SLA interactiva de MCP.

### H11 — Media: `index` permite seleccionar rutas fuera de index_dir

Ubicación: `smart_import/worker/handlers.py:146`.

```python
index_path = Path(cfg.index_dir) / index_name
if not index_path.exists():
    raise FileNotFoundError(...)
```

Una ruta absoluta reemplaza `index_dir`; `../` y symlinks también pueden escapar. La reproducción confirmó que un índice sintético fuera de la raíz llega al runner. SQLite se abre read-only: **no demuestra ejecución de código ni lectura arbitraria completa de cualquier archivo**, pero sí selección de datasets no autorizados y divulgación de rutas/errores. No es SSRF.

**Corrección:** resolución y comprobación de pertenencia en API y worker; preferiblemente IDs de un catálogo, no rutas.

```python
def resolve_allowed_index(root, name):
    base = Path(root).resolve()
    candidate = (base / name).resolve()
    if (Path(name).is_absolute() or candidate.parent != base
            or candidate.suffix != ".sqlite" or not candidate.is_file()):
        raise ValueError("Índice no permitido")
    return candidate
```

Este contrato limita a archivos directamente bajo la raíz. Si se necesitan subdirectorios, usar una allowlist explícita o `is_relative_to(base)` y conservar restricciones de identidad. Verificar integridad del schema del índice y permitir escritura en ese directorio solo al constructor confiable.

### H12 — Alta: la admisión y el límite de archivo llegan después del multipart

Ubicación: `smart_import/api/app.py:633`, `:682`.

```python
file: UploadFile = File(...)
async with _admitted():
    while chunk := await file.read(...):
        if size > limit:
            raise HTTPException(413, ...)
```

FastAPI/Starlette ya parsearon el multipart para inyectar `UploadFile`. El comentario “antes de leer una sola línea del body” no describe esa primera lectura. La prueba satura admisión y observa **8.192 bytes escritos en UploadFile antes del 429**; con archivos mayores el spool puede pasar a disco. El límite actual evita copiar todo al job pero no controla el ingreso previo.

**Corrección:** límite total de request en proxy y/o middleware ASGI anterior al parseo, también para cuerpos sin Content-Length. Reservar admisión antes de consumir `receive`, liberar al cerrar request/cancelar. Un chequeo de Content-Length por sí solo no cubre chunked.

```nginx
# Ejemplo: 10 MiB de archivo + margen multipart, ajustar al contrato.
client_max_body_size 11m;
client_body_timeout 15s;
```

Este fragmento sirve únicamente si todo el tráfico pasa por ese proxy. El overlay de producción también publica un puerto directo; protegerlo con la misma política o deshabilitarlo. El middleware debe contar bytes realmente recibidos, no confiar en la cabecera.

### H13 — Media: piso/departamento y Unicode alteran calle/altura

Ubicación: `smart_import/addresses/heuristic.py:207`, `:262`, `:306`; `smart_import/geocoding/address.py:100`.

```python
raw = (text or "").strip()
self._take_street(raw, consumed, components, evidence)
take(_unit_re().search(raw), "unit")
```

Reproducciones con heurístico sin libpostal:

- `José Hernández 1234, Piso 2 Depto B` → `house_number='2'`, `road='Depto B'`.
- `José Hernández 1234` con tildes NFD → `road='ndez'`, pese a que el texto visualmente es el mismo. `normalize_text` quita tildes, pero el parser de componentes recibe el original sin normalización canónica.

**Corrección:** NFC en la vista interna usada por el parser; original conservado. Para extraction con spans, normalizar el canvas completo o mantener un mapa de offsets, porque NFC puede cambiar longitudes. No aplicar índices de la cadena normalizada sobre la original.

```python
original = text or ""
raw = unicodedata.normalize("NFC", original).strip()
# Detectar spans de unidad/piso; excluirlos de candidatos de calle/altura.
```

El filtro de spans debe preservar `Apt 4B, 350 5th Ave`, direcciones con unidades iniciales y alturas alfanuméricas. No basta elegir siempre “el primer número”, ni mover indiscriminadamente la extracción de CP antes de calle, pues reintroduce regresiones ya documentadas.

### H14 — Alta si existe acceso no autorizado: la API confía en el perímetro, sin aislamiento por tenant

Ubicación: `smart_import/api/app.py:860` y endpoints de lectura/escritura; `docker-compose.apiprod.yml:23`.

```python
@app.get("/imports")
def list_imports(...):
    ...  # inventario global, sin principal/tenant
```

Solo `/internal` exige token; endpoints de listar, leer, descargar, remapear, geocodificar y borrar no autentican ni verifican propiedad. El overlay declara un bind público y documenta una whitelist externa en DOCKER-USER. Por ello **no afirmo que producción esté abierta a Internet**: esa regla no se verificó desde este repositorio. Tampoco CORS es una barrera de acceso para servidores o agentes.

**Corrección para MCP remoto:** identidad autenticada en el transporte/gateway, ownership por job en backend y cierre del puerto directo. Resolver el principal desde la sesión verificada; nunca aceptar `tenant_id` del LLM como prueba de autorización.

```python
# Nueva consulta autorizada; no debe obtener primero el job global y filtrarlo después.
job = store.get_for_tenant(authenticated_principal.tenant_id, job_id)
if job is None:
    raise HTTPException(404, "Job no disponible")
```

Si el servicio seguirá siendo exclusivamente interno y de confianza única, el aislamiento en gateway puede ser una decisión válida; documentar exactamente esa frontera antes de abrir un servidor MCP.

### H15 — Media: PII residual en INFO y excepciones públicas

Ubicación: `smart_import/api/app.py:1277`, `smart_import/geocoding/runner.py:140`, `smart_import/worker/handlers.py:98/:235`, `smart_import/jobs.py:as_dict`.

```python
stage(logger, ..., depot=f"{origin_lat},{origin_lon}", ...)
job.error = str(exc)
```

Los detalles por fila ya se movieron a DEBUG: es una protección existente, no un hallazgo nuevo. Aun así, INFO incluye coordenadas del depot; excepciones internas completas se guardan y retornan al cliente. Muestras en reportes incluyen direcciones: son datos funcionales que requieren autorización/retención, no deberían copiarse automáticamente a logs MCP. Las query strings geográficas pueden llegar además a access logs del proxy/servidor.

**Corrección:** eventos estructurados sin direcciones/coordenadas, códigos de error estables y correlation ID, detalles restringidos. No devolver `str(ValidationError)`, pues incluye valores de entrada.

```python
stage(logger, "GEOCODE", "solicitud aceptada", job=job.id, has_origin=origin is not None)
job.error = "GEOCODE_TEMPORARILY_UNAVAILABLE"  # detalle sensible fuera del payload público
```

Secretos: `.env*` no está versionado y `/config` excluye worker_token/passwords conocidos. Dockerfile usa COPY selectivo; no se encontró copia directa de `.env` a imagen. Agregar `.env*` a `.dockerignore` es endurecimiento barato del contexto de build, **no evidencia de un secreto publicado**.

### H16 — Baja / operativa: retención y limpieza incompletas fuera del camino feliz

Ubicación: `smart_import/geocoding/cache.py:97`, `smart_import/geocoding/runner.py:422`, `smart_import/api/app.py:lifespan`.

```python
# created_at se guarda, pero no participa en get ni en una política de purga.
report.samples.append(sample)
# as_dict recorta a [:50] recién al serializar.
```

La caché mantiene direcciones normalizadas/coordenadas y negativos sin caducidad; borrar un job no borra esa PII ni entradas antiguas. El reporte guarda muestras de todas las fallas aunque exponga solo 50. Hay `finally` para cerrar caché/geocoder en el loop, pero si crear caché falla después de abrir geocoder se salta ese bloque. Lifespan cancela tareas sin esperarlas y no cierra explícitamente el pool global.

**Correcciones acotadas:** TTL positivo/negativo diferenciado, límite de tamaño y purga; no cachear errores transitorios; `ExitStack`/context managers que cubran todas las inicializaciones; acumulación de muestras acotada.

```python
if len(report.samples) < 50:
    report.samples.append(sample)
```

Medir p95 y RSS antes de introducir pools compartidos de conexiones. Las conexiones SQLite actuales se crean en el hilo del trabajo: no convertirlas accidentalmente en una conexión global usada entre hilos.

## 3. Evaluación MCP y propuesta de adaptador

No hay servidor MCP ni dependencia SDK en `pyproject.toml`: existe FastAPI REST con respuestas amplias `dict[str, Any]`. El schema Vepathos de datos no equivale al `inputSchema` de una herramienta MCP.

La propuesta está en `mcp_adapter_proposal.py`. Genera schemas cerrados desde Pydantic, valida entrada y salida, devuelve errores semánticos sin PII y separa `smart_input` de `smart_input_status`. La geocodificación requiere `geocode=true` explícito; la operación larga retorna `accepted` con job_id/poll_after_ms. La autenticación y el backend son dependencias explícitas, no implementaciones ficticias.

Ejemplo de invocación:

```json
{
  "name": "smart_input",
  "arguments": {
    "address": "Av. Corrientes 1234, Piso 2 Depto B",
    "city": "Buenos Aires",
    "country": "AR",
    "origin": {"lat": -34.6037, "lon": -58.3816},
    "geocode": true,
    "idempotency_key": "delivery_20260913_0001"
  }
}
```

Integración central:

```python
Tool(name="smart_input",
     input_schema=SmartInput.model_json_schema(),
     output_schema=ToolOutput.model_json_schema())

CallToolResult(
    content=[TextContent(type="text", text=json.dumps(data, allow_nan=False))],
    structured_content=data,
    is_error=output.state == "error",
)
```

En el SDK Python v2 esos nombres son snake_case; en el protocolo se serializan como `inputSchema`, `outputSchema`, `structuredContent`, `isError`. La especificación distingue fallo de ejecución de herramienta y error del protocolo; resultados estructurados deben respetar el schema publicado. [MCP Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools), [SDK low-level](https://py.sdk.modelcontextprotocol.io/advanced/low-level-server/).

**Pendiente real para habilitar el adaptador:**

1. Implementar backend que cree/consulte jobs de una sola dirección con la misma lógica de negocio. No hacer POST de geocode hasta que normalize termine ni inferir consentimiento desde texto libre.
2. Persistir `(tenant, idempotency_key, payload_hash) → job_id` de manera atómica. El ID aleatorio actual y la deduplicación del broker no deduplican reintentos del cliente. Mismo key con distinto payload debe producir CONFLICT. Resolver el caso commit exitoso + respuesta perdida mediante la misma clave.
3. Implementar `principal_for` con identidad verificada y ownership en backend. El adaptador propuesto no ofrece un modo anónimo por defecto.
4. Instalar y fijar una versión probada de SDK 2.x en un entorno aislado; verificar initialize/tools/list/tools/call, JSON-RPC inválido, desconexión, cancelación y serialización real. La documentación consultada identifica v2 como línea estable; no se debe copiar código v1 sin revisar compatibilidad. [SDK Python](https://py.sdk.modelcontextprotocol.io/).
5. Para Streamable HTTP: límites antes del parser, autenticación y validación de Origin; para stdio reservar stdout al protocolo y logs a stderr. El logger actual ya usa stderr. [Transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

**Tiempo y estado:** no existe un único “timeout estándar MCP” universal. Propongo presupuesto interactivo de 5 s para admisión/consulta y trabajo costoso por jobs; es una decisión de servicio, no una norma del protocolo. Construir índices/cortar PBF puede durar minutos. `asyncio.timeout` cancela espera cooperativa; no mata un hilo CPU ni `osmium`. El backend debe encolar rápido y usar workers terminables o presupuestos/cancelación propios. El modo embedded actual tiene cola interna de geocode sin techo global y `subprocess.run` sin timeout: no es adecuado para ejecutar indiscriminadamente cada tool call hasta su final.

No anunciar `idempotentHint=true` para la mutación antes de implementar la deduplicación durable. Una sesión MCP no debe ser el único almacén de jobs. El schema de país de la propuesta valida formato; el backend debe usar el catálogo ISO del proyecto para validar pertenencia.

## 4. Dominio geográfico: qué conservar y qué medir

- **Fallback existente:** FTS estricto, relajación por palabras, candidatos de altura/calle, fuzzy scoring y reintento sin cláusulas de unidad. `GEOCODER_FALLBACK` es configuración informativa sin proveedor implementado. No hay reverse-geocoding online. No atribuir 429/504 de mapas a una integración inexistente.
- **Typos graves:** RapidFuzz solo puntúa candidatos recuperados; si FTS/los fallbacks no encuentran la calle, fuzzy no puede rescatarla. Evaluar índice de trigramas/prefijos limitado a la región antes de añadir un servicio pago. No convertir todos los typos en un match aproximado verde.
- **Intersecciones:** el índice trabaja con puntos/representantes y candidatos de calle; no demuestra cálculo del punto topológico de cruce de dos vías. `Av. Corrientes y Av. Callao` no debería prometer precisión de puerta/intersección. Fallback a calle/localidad debe indicarse explícitamente y requerir revisión según el contrato MCP.
- **Abreviaturas:** `Av.` se expande; los ejemplos `Diag. 74 1234` y `Cme. Test 1234` preservan esas abreviaturas en la normalización revisada. El parser sí extrae altura de Diag. No expandir `Cme.` sin una equivalencia regional confirmada: agregar datos al lexicón con pruebas del país.
- **Score vs precisión:** `band_for` ignora `precision` de forma deliberada y documentada. Un punto a nivel calle puede verse verde por score alto: es contrato de producto actual, no lo conté como bug accidental. Para MCP, separar `confidence`, `precision` y `requires_review`: 0.97 de similitud textual no implica 97% de certeza ni precisión métrica.
- **Conservar:** original visible, coordenadas válidas ya suministradas, opt-in de geocode, guards por distancia, límites de candidatos, consultas parametrizadas y tests regionales existentes.

SSRF/SQL/shell: no se encontró un camino desde address hacia una URL arbitraria. HttpArtifactStore toma base_url de configuración; no sigue URLs contenidas en direcciones. SQLite usa parámetros y FTS sanitiza/entrecomilla tokens. Osmium se invoca con lista de argumentos, no shell=True. Esto reduce esas superficies específicas; no constituye una prueba formal de ausencia de vulnerabilidades en todas las dependencias.

## 5. Matriz ejecutable de validación

Los archivos `test_review_regressions.py` y `test_adapter_contract.py` son la matriz ejecutable. No dependen de red ni de producción. Los índices y direcciones usados para coordenadas exactas son sintéticos.

```bash
# Controles que pasan + regresiones conocidas explícitas:
.venv/bin/python -m pytest -q docs/audit-2026-09-13

# Gate de lanzamiento: hace fallar los bugs aún abiertos en vez de xfail:
.venv/bin/python -m pytest -q docs/audit-2026-09-13 --runxfail
```

Casos y salida esperada:

1. **Homónimos/contexto:** Corrientes 1234, mismo dataset, bbox BA → Córdoba. Esperado: -34.6037 → -31.4201; nunca reutilizar el pin fuera de bbox. H01, falla actual.
2. **Resultado sospechoso 0.91:** frío/caché/issues. Esperado: review en los tres. H02, dos fallos actuales.
3. **Mapping tras geocode:** esperado preview normalizado nuevo; ninguna coordenada derivada antigua como vigente. H03, falla actual.
4. **Mapping durante geocode:** esperado 409 y ninguna ejecución nueva sobre ese job. H05, falla actual.
5. **bbox no numérico:** `x,0,0,0`; esperado 400/422, nunca 500. H04, falla actual.
6. **Coords imposibles/no finitas:** (91,181), (NaN,0), (0,Infinity); esperado rechazo previo a encolar. H04, tres fallos actuales.
7. **Expansión gigante:** un padre, 20 paquetes, límite 3; esperado límite efectivo o error explícito. H06, falla actual.
8. **JSON patológico:** 200 aperturas `{"address":` sin cierre; esperado escaneo global acotado. H06, falla actual.
9. **Reparación sin alterar strings:** dirección `Calle NaN 42.` con coma final inválida; esperado texto original intacto. H07, falla actual.
10. **Dos ejecuciones mismo worker/job:** memoria y Redis falso; esperado segundo claim=False. H08, dos fallos actuales.
11. **Dirección sin evidencia:** `x`; esperado not_found=1, needs_geocode residual=1. H09, falla actual del contador base.
12. **Transferencia 504→200:** esperado reintento y éxito; control aprobado.
13. **Transferencia 429:** esperado clasificación transitoria, política Retry-After/backoff al integrarla. H10, clasificación falla actualmente.
14. **Índice externo a raíz:** ruta absoluta de SQLite sintético; esperado rechazo antes del runner. H11, falla actual.
15. **Servicio saturado:** multipart 8 KiB, admisión llena; esperado 429 sin escribir UploadFile. H12, falla actual.
16. **Piso/departamento:** `José Hernández 1234, Piso 2 Depto B`; esperado house=1234 y road=José Hernández. H13, falla actual.
17. **Unicode NFC/NFD:** `José Hernández 1234`; esperado mismos componentes. H13, falla actual.
18. **Typos graves/intersección:** `Av. Crrnts 1234, Buenos Aires` y `Av. Corrientes y Av. Callao, Buenos Aires`; esperado no afirmar puerta exacta. Dos controles aprobados; no validan geografía real.
19. **Texto malicioso:** comillas/SQL, URL de metadata, tokens inexistentes; esperado no llamada de red ni SQL ejecutable y not_found en fixture. Tres controles aprobados.
20. **Nulos JSON:** lista directa `[null,{"address":"José Hernández 1234","phone":null}]`; esperado una fila utilizable. Control aprobado. No implica tolerancia universal a cualquier wrapper.
21. **MCP input:** >2048 caracteres, vacío, NUL, NaN, lat=91, boolean como string, atributo URL extra, país largo. Esperado INVALID_ARGUMENT y backend no invocado. Ocho controles aprobados.
22. **MCP operación lenta/error interno:** esperado TIMEOUT recuperable o INTERNAL sin texto sensible; dos controles aprobados.
23. **MCP job aceptado:** principal autenticado llega al backend, geocode=False por defecto, job_id y polling; control aprobado.
24. **MCP precisión falsa:** status=matched + precision=street debe ser inválido; control aprobado.
25. **MCP schema cerrado:** propiedades extras prohibidas en entrada/salida; control aprobado.

Antes del lanzamiento, agregar pruebas de integración fuera de esta matriz local: dos tenants con acceso cruzado, lease perdido con Redis real y publicación tardía, caída después de commit antes de ACK, p95/p99 con índices fríos/calientes, timeout real de osmium, zip-bomb bajo límites de contenedor, cargas chunked por el puerto efectivo, y corpus etiquetado por país con tasa de falsos positivos/distancia al punto real. Esos resultados todavía no están medidos.

## Criterio de entrega

La revisión no requiere migrar toda la arquitectura. Las primeras correcciones deberían preservar el contrato web, añadir regresiones concretas y desplegarse por bloques pequeños. El adaptador MCP es una propuesta de frontera; habilitarlo sin corregir identidad de caché, revisión obligatoria, ownership e idempotencia trasladaría los bugs actuales a un cliente que reintenta automáticamente.
