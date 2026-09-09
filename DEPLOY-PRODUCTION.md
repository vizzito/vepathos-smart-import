# Deploy en producción — Smart Import distribuido

Guía operativa con la topología **validada en prod** (api-prod + VM worker + Mac
worker opcional). Cubre deploy desde cero, agregar una Mac nueva, comandos útiles
y troubleshooting de lo que apareció en la migración real.

Documentos relacionados: [SETUP.md](SETUP.md) (general), [RUNBOOK.md](RUNBOOK.md)
(pruebas), [ARCHITECTURE.md](ARCHITECTURE.md) (diseño).

---

## Índice

1. [Topología](#1-topología)
2. [Qué compose va en cada máquina](#2-qué-compose-va-en-cada-máquina)
3. [Firewall y red](#3-firewall-y-red)
4. [Deploy desde cero — api-prod (API)](#4-deploy-desde-cero--api-prod-api)
5. [Deploy desde cero — VM worker (PBFs)](#5-deploy-desde-cero--vm-worker-pbfs)
6. [Agregar una Mac como worker (libpostal)](#6-agregar-una-mac-como-worker-libpostal)
7. [Cómo reparte la cola (¿quién procesa?)](#7-cómo-reparte-la-cola-quién-procesa)
8. [config_drift y libpostal](#8-config_drift-y-libpostal)
9. [Comandos útiles](#9-comandos-útiles)
10. [Verificación y checklist](#10-verificación-y-checklist)
11. [Troubleshooting](#11-troubleshooting)
12. [Rollback](#12-rollback)

---

## 1. Topología

```
                         RouteHub (api-prod, vepathos-net)
                                    │
                                    ▼
              ┌─────────────────────────────────────────┐
              │  API Smart Import (api-prod)            │
              │  178.105.42.199 — solo rol api          │
              │  Redis + RabbitMQ (misma máquina)       │
              └──────────────────┬──────────────────────┘
                                 │ colas RabbitMQ
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
    ┌────────────────────────┐    ┌────────────────────────┐
    │  Worker VM (w1/c1)       │    │  Worker Mac (opcional)  │
    │  46.224.217.160          │    │  NAT — túnel SSH        │
    │  runtime, PBFs montados  │    │  runtime-libpostal      │
    │  Redis/Rabbit/API directo│    │  todo vía túnel :8110   │
    └────────────────────────┘    └────────────────────────┘
```

| Nodo | IP / acceso | Rol | Imagen | PBFs |
|------|-------------|-----|--------|------|
| **api-prod** | `178.105.42.199`, SSH `deploy@` | API | `runtime` | No |
| **VM w1** | `46.224.217.160`, SSH `martin@` | Worker | `runtime` | Sí (`ROUTE_OPTIMIZER_DATA`) |
| **Mac** | local + túnel | Worker opcional | `runtime-libpostal` | Sí (bind local) |

La API **no procesa** jobs: encola en RabbitMQ, guarda archivos en disco y sirve
descargas. Los workers bajan el raw por HTTP (`/internal/jobs/.../artifacts/`).

---

## 2. Qué compose va en cada máquina

**No mezclar overlays entre hosts.** Cada máquina usa su par de archivos.

| Máquina | Comando `docker compose` | Archivos |
|---------|--------------------------|----------|
| **api-prod** | `-f docker-compose.yml -f docker-compose.apiprod.yml up -d smart-import` | Base + overlay api-prod |
| **VM worker** | `--env-file .env.worker -f docker-compose.worker.yml up -d` | Solo worker (sin samevm, sin mac) |
| **Mac worker** | `--env-file .env.prod.smart.local -f docker-compose.worker.yml -f docker-compose.mac.worker.yml up -d` | Worker + overlay Mac |

| Archivo | Dónde | Para qué |
|---------|-------|----------|
| `docker-compose.apiprod.yml` | **solo api-prod** | Bind IP privada/pública, red RouteHub, red Redis/Rabbit |
| `docker-compose.worker.yml` | VM y Mac | Base del worker |
| `docker-compose.mac.worker.yml` | **solo Mac** | Túnel → `host.docker.internal:16379/5673/8110` |
| `docker-compose.vm.worker.yml` | Solo si **no** podés abrir 8100 en Hetzner | Túnel local `:18100` → API (plan B) |
| `docker-compose.samevm.worker.yml` | **Obsoleto** en prod actual | Era cuando API y worker vivían en la misma VM |

### Plantillas de entorno

Los `.env` reales no se versionan. Copiá desde [deploy/templates/](deploy/templates/):

| Plantilla | Destino en la máquina |
|-----------|------------------------|
| `api-prod.env.template` | `.env` (api-prod) |
| `worker-vm.env.template` | `.env.worker` (VM) |
| `worker-mac.env.template` | `.env.prod.smart.local` (Mac) |
| `local-dev.env.template` | `.env` (desarrollo local) |

---

## 3. Firewall y red

### 3.1 Hetzner Cloud Firewall (`fw-api-prod`)

Inbound mínimo para la VM worker (mismas IPs que Redis/Rabbit):

| Source IP | Protocol | Port | Uso |
|-----------|----------|------|-----|
| `46.224.217.160/32` | TCP | 6379 | Redis |
| `46.224.217.160/32` | TCP | 5672 | RabbitMQ |
| `46.224.217.160/32` | TCP | **8100** | API archivos (worker VM) |
| `46.224.84.34/32` | TCP | 6379 | Redis (IP de salida real de la VM) |
| `46.224.84.34/32` | TCP | 5672 | RabbitMQ |
| `46.224.84.34/32` | TCP | **8100** | API archivos |

Sin **8100** el worker VM conecta a Redis/Rabbit pero **timeout** al bajar
archivos (`ConnectTimeout` o jobs colgados en `analyzing`).

La **Mac no necesita** 8100 en Hetzner: entra por túnel SSH a `127.0.0.1:8100`
en api-prod.

### 3.2 DOCKER-USER en api-prod (iptables)

Además de Hetzner, en el host api-prod Docker filtra el puerto publicado 8100.
Reglas esperadas (orden importa):

```
ACCEPT  46.224.217.160  → tcp dpt:8100
ACCEPT  10.0.0.0/16      → tcp dpt:8100
ACCEPT  46.224.84.34     → tcp dpt:8100
DROP    cualquier otro   → tcp dpt:8100
```

Verificar:

```bash
ssh deploy@178.105.42.199
sudo iptables -L DOCKER-USER -n | grep 8100
```

### 3.3 API — redes Docker (overlay apiprod)

- **`vepathos-net`**: RouteHub alcanza `http://vepathos-smart-import:8100`
- **`route-optimizer-app_api-network`**: API habla con
  `route-optimizer-redis` / `route-optimizer-rabbitmq` por nombre (evita
  DOCKER-USER en 6379/5672 desde el bridge)

Variables en `.env` de api-prod:

```bash
REDIS_HOST=route-optimizer-redis
RABBITMQ_HOST=route-optimizer-rabbitmq
SMART_IMPORT_ROLE=api
SMART_IMPORT_LIBPOSTAL_ENABLED=false
SMART_IMPORT_TARGET=runtime
```

RouteHub:

```bash
SMART_IMPORT_URL=http://vepathos-smart-import:8100
```

---

## 4. Deploy desde cero — api-prod (API)

**Usuario:** `deploy@178.105.42.199`  
**Path:** `~/vepathos-smart-import`

```bash
ssh deploy@178.105.42.199
cd ~/vepathos-smart-import

# .env: plantilla api-prod → token, binds, REDIS/Rabbit por nombre de container
cp deploy/templates/api-prod.env.template .env
# Editar: SMART_IMPORT_ROLE=api, token, BIND_PRIV/PUB, REDIS_HOST, RABBITMQ_HOST

DOCKER_BUILDKIT=1 docker compose \
  -f docker-compose.yml \
  -f docker-compose.apiprod.yml \
  build smart-import

docker compose \
  -f docker-compose.yml \
  -f docker-compose.apiprod.yml \
  up -d smart-import
```

Verificar:

```bash
curl -s http://127.0.0.1:8100/health | python3 -m json.tool | grep -A2 deployment
curl -s http://10.0.0.2:8100/health | python3 -c "import json,sys; print(json.load(sys.stdin)['deployment'])"
docker ps | grep smart-import   # solo vepathos-smart-import, healthy
```

**No** levantar worker en api-prod.

---

## 5. Deploy desde cero — VM worker (PBFs)

**Usuario:** `martin@46.224.217.160`  
**Path:** `~/vepathos-worker/vepathos-smart-import`

### 5.1 Prerequisitos

- Firewall Hetzner con 6379, 5672 y **8100** (ver §3.1)
- PBFs montados: `ROUTE_OPTIMIZER_DATA=/home/martin/vepathos-worker/vepathos-worker/data`
- Mismo `SMART_IMPORT_WORKER_TOKEN` que en el `.env` de api-prod

### 5.2 `.env.worker`

```bash
cp deploy/templates/worker-vm.env.template .env.worker
```

Claves mínimas:

```bash
SMART_IMPORT_API_URL=http://178.105.42.199:8100
SMART_IMPORT_TARGET=runtime
IMAGE_TAG=2026.09.07
SMART_IMPORT_WORKER_TOKEN=<mismo que api-prod>
RABBITMQ_HOST=178.105.42.199
REDIS_HOST=178.105.42.199
RABBITMQ_PASSWORD=...
REDIS_PASSWORD=...
SMART_IMPORT_CONSUME_NORMALIZE=true
SMART_IMPORT_CONSUME_GEOCODE=true
ROUTE_OPTIMIZER_DATA=/home/martin/vepathos-worker/vepathos-worker/data
```

**No** usar `docker-compose.samevm.worker.yml`.  
**No** usar `docker-compose.vm.worker.yml` si Hetzner tiene 8100 abierto.

### 5.3 Probar conectividad desde el host VM

```bash
curl -s --max-time 5 http://178.105.42.199:8100/health | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])"
# → ok
```

### 5.4 Preflight y arranque

```bash
cd ~/vepathos-worker/vepathos-smart-import

docker compose --env-file .env.worker \
  -f docker-compose.worker.yml \
  run --rm worker worker --check
# cola ok | estado ok | archivos ok

docker compose --env-file .env.worker \
  -f docker-compose.worker.yml up -d

docker logs vepathos-smart-import-worker --tail 10
```

### 5.5 Bajar la API vieja (si quedó en esta VM)

Tras verificar E2E con la API en api-prod:

```bash
docker stop vepathos-smart-import   # API monolítica vieja en la VM de PBFs
```

Solo debe quedar `vepathos-smart-import-worker`.

---

## 6. Agregar una Mac como worker (libpostal)

Para sumar capacidad de procesamiento con **libpostal** (mejor parseo en
direcciones difíciles). La Mac **no** puede llegar directo a Redis/Rabbit/API
por IP pública (firewall); usa **túnel SSH** como el cutter del optimizer.

### 6.1 Prerequisitos en la Mac

- Docker Desktop
- Repo clonado: `~/workspace/vepathos-smart-import`
- PBFs locales (mismo path que optimizer):  
  `ROUTE_OPTIMIZER_DATA=/Users/<tu>/workspace/route-optimizer-app/data`
- Acceso SSH a api-prod como `deploy` (clave en `~/.ssh/config`)
- Imagen `runtime-libpostal` buildeada al menos una vez:

```bash
cd ~/workspace/vepathos-smart-import
docker compose --env-file .env.prod.smart.local build worker
```

### 6.2 SSH — `~/.ssh/config`

```sshconfig
Host vepathos-tunnel
    HostName 178.105.42.199
    User deploy
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    LocalForward 16379 10.0.0.2:6379
    LocalForward 5673  10.0.0.2:5672
    LocalForward 8110  127.0.0.1:8100
    ServerAliveInterval 30
    ServerAliveCountMax 3
    TCPKeepAlive yes
    ExitOnForwardFailure yes
```

La derecha la resuelve **api-prod**: Redis/Rabbit por IP privada del host,
Smart Import API por loopback (`127.0.0.1:8100`).

### 6.3 Env de la Mac — `.env.prod.smart.local`

```bash
cp deploy/templates/worker-mac.env.template .env.prod.smart.local
```

Lo crítico:

```bash
SMART_IMPORT_TARGET=runtime-libpostal
SMART_IMPORT_LIBPOSTAL_ENABLED=true
SMART_IMPORT_WORKER_NAME=si-worker-prod-mac
SMART_IMPORT_WORKER_TOKEN=<mismo que api-prod>
ROUTE_OPTIMIZER_DATA=/Users/<tu>/workspace/route-optimizer-app/data
# El overlay mac.worker.yml pisa hosts/puertos; no hace falta API_URL acá
# pero puede estar documentado:
# SMART_IMPORT_API_URL=http://host.docker.internal:8110
```

Los **umbrales de geocode** (`GEOCODE_*`, `AUTO_ACCEPT_THRESHOLD`, etc.) deben
coincidir con api-prod y VM o el CSV sale distinto según qué nodo agarre el job.

### 6.4 Levantar túnel + worker

```bash
# 1) Túnel persistente (Mac)
autossh -M 0 \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -N vepathos-tunnel &

# 2) Verificar las 3 patas
nc -z 127.0.0.1 16379 && echo redis OK
nc -z 127.0.0.1 5673  && echo rabbit OK
curl -s http://127.0.0.1:8110/health | head -c 80

# 3) Preflight
cd ~/workspace/vepathos-smart-import
docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml \
  -f docker-compose.mac.worker.yml \
  run --rm worker worker --check

# 4) Up
docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml \
  -f docker-compose.mac.worker.yml up -d

docker logs si-worker-prod-mac --tail 8
```

### 6.5 Bajar solo la Mac (prod sigue en VM)

```bash
docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml \
  -f docker-compose.mac.worker.yml down
```

El túnel puede seguir corriendo para probar la API desde la Mac (`curl :8110`).

### 6.6 Nueva Mac — checklist rápido

1. Clonar repo + build `runtime-libpostal`
2. `cp deploy/templates/worker-mac.env.template .env.prod.smart.local` (umbrales alineados) y ajustar `ROUTE_OPTIMIZER_DATA`
3. Configurar `~/.ssh/config` → `vepathos-tunnel`
4. Probar túnel (`8110/health`)
5. `worker --check` → tres `ok`
6. `up -d` con `mac.worker.yml`
7. `/health` → 2 workers, `config_drift` solo con el nodo Mac

---

## 7. Cómo reparte la cola (¿quién procesa?)

- RabbitMQ entrega cada mensaje a **un** consumer libre (round-robin entre workers).
- Cada worker tiene `SMART_IMPORT_WORKER_SLOTS=2` → hasta 2 jobs en paralelo.
- **No** hay preferencia por libpostal ni por VM: quien esté libre agarra el job.
- Si la Mac está apagada → 100% VM. Si ambas arriba → reparten (~50/50 a largo plazo).
- Normalize y geocode del **mismo** job pueden ir a workers distintos (son mensajes
  separados); el estado vive en Redis y los archivos en la API.

Ver quién procesó:

```bash
docker logs si-worker-prod-mac --since 10m | grep imp_
docker logs vepathos-smart-import-worker --since 10m | grep imp_
```

---

## 8. config_drift y libpostal

| Nodo | `LIBPOSTAL_ENABLED` | Imagen |
|------|---------------------|--------|
| api-prod | `false` | `runtime` |
| VM worker | `false` | `runtime` |
| Mac worker | `true` | `runtime-libpostal` |

`libpostal_enabled` entra en la huella de flota (`fleet.py`). Con la Mac arriba:

```json
"config_drift": ["<node-id-mac>"]
```

**Es esperado y permanente** mientras solo la Mac tenga libpostal. Cualquier **otro**
nodo en drift es un bug de configuración (umbrales distintos).

---

## 9. Comandos útiles

### 9.1 Flota y colas (desde Mac con túnel, o api-prod local)

```bash
curl -s http://127.0.0.1:8110/health | python3 -m json.tool | grep -A25 '"fleet"'
```

### 9.2 Preflight worker

```bash
# VM
docker compose --env-file .env.worker -f docker-compose.worker.yml \
  run --rm worker worker --check

# Mac
docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml -f docker-compose.mac.worker.yml \
  run --rm worker worker --check
```

### 9.3 E2E manual (upload → normalize)

```bash
JOB=$(curl -s -F "file=@fixtures/es_sin_coords.csv" \
  "http://127.0.0.1:8110/imports?wait=90" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['job_id'])")
echo "job=$JOB"
curl -s "http://127.0.0.1:8110/imports/$JOB" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])"
curl -s "http://127.0.0.1:8110/imports/$JOB/download?format=flat" | head -3
```

### 9.4 Logs

```bash
# api-prod
docker logs vepathos-smart-import --since 5m

# VM
docker logs vepathos-smart-import-worker -f

# Mac
docker logs si-worker-prod-mac -f
```

### 9.5 Colas RabbitMQ (api-prod, como root si hace falta)

```bash
docker exec route-optimizer-rabbitmq rabbitmqctl list_queues name messages messages_unacknowledged | grep smart-import
```

### 9.6 Reiniciar servicios

```bash
# API api-prod
docker compose -f docker-compose.yml -f docker-compose.apiprod.yml restart smart-import

# Worker VM
docker compose --env-file .env.worker -f docker-compose.worker.yml restart

# Worker Mac
docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml -f docker-compose.mac.worker.yml restart
```

`restart` no recrea el container si no existe — usar `up -d` si lo bajaste con `down`.

### 9.7 Túnel Mac — recuperación

```bash
pkill -f 'autossh.*vepathos-tunnel' 2>/dev/null
autossh -M 0 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes -N vepathos-tunnel &
nc -z 127.0.0.1 8110 && curl -s http://127.0.0.1:8110/health | head -c 60
```

---

## 10. Verificación y checklist

### Prod mínimo (solo api-prod + VM)

- [ ] api-prod: `curl localhost:8100/health` → `role: api`, `state: redis`
- [ ] RouteHub: `SMART_IMPORT_URL=http://vepathos-smart-import:8100`
- [ ] Hetzner: 8100 abierto para IPs de la VM
- [ ] VM: `curl 178.105.42.199:8100/health` → `ok`
- [ ] VM: `worker --check` → cola, estado, **archivos** ok
- [ ] VM: upload real → logs con `READ → NORMALIZE → listo`
- [ ] `/health` → al menos 1 worker, `config_drift: []` (sin Mac)

### Con Mac libpostal

- [ ] Túnel Mac: 16379, 5673, 8110 responden
- [ ] Mac: `worker --check` → tres ok
- [ ] `/health` → 2 workers
- [ ] `config_drift` solo nodo Mac
- [ ] Bajar Mac → prod sigue (VM sola procesa)

---

## 11. Troubleshooting

| Síntoma | Causa probable | Fix |
|---------|----------------|-----|
| `archivos ConnectTimeout` (VM) | Hetzner sin 8100 o DOCKER-USER | Reglas §3.1 y §3.2 |
| `Connection refused` `:18100` | Túnel caído o overlay vm sin túnel en **VM** | Levantar `ssh -L 18100:...` **en la VM**, o usar Hetzner 8100 |
| Worker VM idle, Mac procesa todo | Normal si Mac más rápida/libre | OK; o bajar Mac para forzar VM |
| Job colgado `analyzing` | Worker no baja raw (API URL mal) | Ver `SMART_IMPORT_API_URL`, `--check` |
| `404` en `/internal/.../raw` | Worker apunta a API vieja (`samevm`) | Quitar samevm; URL → api-prod |
| `JSONDecodeError` curl `:8110` | Túnel Mac caído | Relanzar `vepathos-tunnel` |
| `Permission denied deploy@` desde VM | Usuario/clave incorrectos para túnel | Usar Hetzner 8100 (no SSH deploy desde VM) |
| `config_drift` con VM | Umbrales distintos en `.env` vs `.env.worker` | Alinear `GEOCODE_*` con api-prod |
| Mensajes en DLQ | Jobs fallidos durante migración | Re-subir archivo; purgar DLQ si hace falta |
| Mac en drift | Esperado | Solo Mac con libpostal |

### Plan B: VM sin Hetzner 8100

Si no podés abrir 8100, usar túnel SSH **en la VM** (mismo usuario que el cutter,
no necesariamente `deploy@`):

```bash
# En ~/.ssh/config de la VM, en el Host que ya usa el cutter:
LocalForward 18100 127.0.0.1:8100

ssh -f -N <host-del-cutter>
docker compose --env-file .env.worker \
  -f docker-compose.worker.yml \
  -f docker-compose.vm.worker.yml up -d
```

Ver `docker-compose.vm.worker.yml`.

---

## 12. Rollback

Volver a API+worker monolito en la VM de PBFs (ventana de mantenimiento):

1. Bajar workers Mac y VM distribuidos
2. Levantar API en VM con `docker-compose.yml` (sin apiprod)
3. Worker con `samevm.worker.yml`
4. RouteHub → URL anterior

Los jobs en Redis de la API distribuida no migran automáticamente.

---

*Última actualización: arquitectura api-prod + VM worker + Mac libpostal (2026-09).*
