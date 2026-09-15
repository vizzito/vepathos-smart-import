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
5. [Deploy desde cero — VM worker (PBFs)](#5-deploy-desde-cero--vm-worker-pbfs) · [5.6 VM worker #2+](#56-agregar-worker-vm-2-o-más)
6. [Agregar una Mac como worker (libpostal)](#6-agregar-una-mac-como-worker-libpostal) · [6.7 Actualizar código en la Mac](#67-actualizar-código-en-la-mac-apunta-a-prod)
7. [Cómo reparte la cola (¿quién procesa?)](#7-cómo-reparte-la-cola-quién-procesa)
8. [config_drift y libpostal](#8-config_drift-y-libpostal)
9. [Comandos útiles](#9-comandos-útiles)
10. [Verificación y checklist](#10-verificación-y-checklist)
11. [Troubleshooting](#11-troubleshooting)
12. [Rollback](#12-rollback)
13. [Actualizar prod — rollout de código](#13-actualizar-prod--rollout-de-código) · **[receta cada vez](#130-receta--cada-vez-que-cambiás-código)** · [alias OSM](#130a-mapa-de-alias-osm-no-va-en-git)

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

### 5.6 Agregar worker VM #2 (o más)

Sumar **capacidad** a la flota existente: misma API, misma cola, mismo Redis.
**No** es un Smart Import aislado — el worker nuevo entra al pool round-robin con
w1 y la Mac.

#### Qué necesita la VM nueva

| Requisito | Detalle |
|-----------|---------|
| Docker + compose v2 | Igual que w1 |
| PBFs montados | `ROUTE_OPTIMIZER_DATA` apuntando a la raíz `data/` del optimizer |
| Cobertura OSM | Idealmente **los mismos extracts/PBFs** que w1; si w2 solo tiene Miami y un job es de CABA, puede fallar o dar peor resultado |
| Repo | `git clone` + branch `feat/smart-import-produccion` |
| Credenciales | Mismo `SMART_IMPORT_WORKER_TOKEN` que api-prod; mismos `GEOCODE_*` que api y w1 |
| Nombre único | `SMART_IMPORT_WORKER_NAME=vepathos-smart-import-worker-w2` (evita choque en logs y fleet) |

#### Paso 1 — Descubrir IP(s) de salida de la VM nueva

Hetzner y DOCKER-USER whitelistean **IPs de origen**, no el hostname. Una VM
puede tener **más de una** IP de egress (w1 usa `46.224.217.160` y
`46.224.84.34`). Hay que abrir **todas** las que use la VM nueva.

Desde la **VM nueva** (antes de levantar el worker):

```bash
curl -s ifconfig.me && echo
curl -s icanhazip.com && echo
# Anotar cada IP distinta
```

Si ya levantaste el worker y `--check` falla solo en **archivos** pero cola y
estado van bien, casi seguro falta whitelistear la IP de salida real (no la
IP “principal” del panel de Hetzner).

#### Paso 2 — Hetzner Cloud Firewall (`fw-api-prod`)

En [Hetzner Console](https://console.hetzner.cloud/) → Firewalls →
`fw-api-prod` → Inbound rules, **por cada IP** `<IP_W2>/32` de la VM nueva,
agregar las mismas tres reglas que tiene w1:

| Source IP | Protocol | Port | Uso |
|-----------|----------|------|-----|
| `<IP_W2>/32` | TCP | 6379 | Redis |
| `<IP_W2>/32` | TCP | 5672 | RabbitMQ |
| `<IP_W2>/32` | TCP | **8100** | API — descarga de archivos (`/internal/...`) |

Si la VM tiene **dos** IPs de salida, repetir las 6 reglas (3 × 2).

Sin **8100**: `--check` puede marcar cola/estado ok y **archivos** timeout;
jobs quedan en `analyzing`.

#### Paso 3 — DOCKER-USER en api-prod (iptables)

Además del firewall de Hetzner, el host api-prod filtra el puerto publicado.
Agregar reglas **antes** del DROP final (mismo criterio que §3.2):

```bash
ssh deploy@178.105.42.199

# Ver reglas actuales (modelo w1)
sudo iptables -L DOCKER-USER -n -v | grep -E '8100|6379|5672'

# Por cada IP de la VM nueva (<IP_W2>):
sudo iptables -I DOCKER-USER 1 -s <IP_W2> -p tcp -m tcp --dport 8100 -j ACCEPT
# Si Redis/Rabbit también están filtrados por IP (como w1), repetir:
sudo iptables -I DOCKER-USER 1 -s <IP_W2> -p tcp -m tcp --dport 6379 -j ACCEPT
sudo iptables -I DOCKER-USER 1 -s <IP_W2> -p tcp -m tcp --dport 5672 -j ACCEPT
```

Verificar contadores suben cuando w2 hace `--check`:

```bash
sudo iptables -L DOCKER-USER -n -v | grep <IP_W2>
```

> Persistencia: si api-prod reinicia y pierde reglas manuales, re-aplicarlas o
> integrarlas al script de firewall del host (mismo lugar donde están las de w1).

#### Paso 4 — Config en la VM nueva

```bash
ssh <usuario>@<ip-vm-nueva>
git clone git@github.com:vizzito/vepathos-smart-import.git ~/vepathos-smart-import
cd ~/vepathos-smart-import
git checkout feat/smart-import-produccion

cp deploy/templates/worker-vm.env.template .env.worker
# Editar:
#   SMART_IMPORT_WORKER_NAME=vepathos-smart-import-worker-w2
#   SMART_IMPORT_WORKER_TOKEN=<mismo que api-prod>
#   ROUTE_OPTIMIZER_DATA=/ruta/real/a/route-optimizer-app/data
#   IMAGE_TAG=<mismo tag que w1 y api-prod>
#   RABBITMQ_* / REDIS_* → passwords del optimizer
#   GEOCODE_* → copiar del .env de api-prod (§8 config_drift)

# Opcional: heredar umbrales del despliegue
scp deploy@178.105.42.199:~/vepathos-smart-import/.env .env
# .env.worker queda como overlay (compose lee los dos)
```

#### Paso 5 — Conectividad desde el host (antes del container)

```bash
curl -s --max-time 5 http://178.105.42.199:8100/health | python3 -c \
  "import json,sys; print(json.load(sys.stdin)['status'])"
# → ok   (si timeout → revisar pasos 2 y 3)
```

#### Paso 6 — Build, preflight y arranque

```bash
DOCKER_BUILDKIT=1 docker compose --env-file .env.worker \
  -f docker-compose.worker.yml \
  build worker

docker compose --env-file .env.worker \
  -f docker-compose.worker.yml \
  run --rm worker worker --check
# → cola ok | estado ok | archivos ok

docker compose --env-file .env.worker \
  -f docker-compose.worker.yml \
  up -d

docker logs vepathos-smart-import-worker-w2 --tail 10
```

**No** usar `docker-compose.samevm.worker.yml` ni `docker-compose.mac.worker.yml`
en una VM Linux con IP pública.

#### Paso 7 — Verificar en flota

Desde api-prod o Mac (túnel `:8110`):

```bash
curl -s http://127.0.0.1:8100/health | python3 -m json.tool | grep -A40 '"fleet"'
```

Checklist w2:

- [ ] Aparece `vepathos-smart-import-worker-w2` (o el nombre que elegiste)
- [ ] `config_drift: []` respecto a api y w1 (mismos `GEOCODE_*`, sin libpostal)
- [ ] Upload de prueba → logs en w2 con `listo: normalize/...` o `geocode/...`
- [ ] Contadores iptables en api-prod suben para `<IP_W2>`

#### Bajar w2 sin afectar prod

```bash
docker compose --env-file .env.worker -f docker-compose.worker.yml down
```

La cola sigue; w1 y Mac absorben el tráfico. Opcional: quitar reglas Hetzner/iptables
de `<IP_W2>` si la VM se da de baja permanentemente.

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
7. Copiar `street_aliases.sqlite` al volume de cache ([§13.0a](#130a-mapa-de-alias-osm-no-va-en-git))
8. `/health` → 2 workers, `config_drift` solo con el nodo Mac

### 6.7 Actualizar código en la Mac (apunta a prod)

Esto **no** es `docker compose restart smart-import`. Ese comando es la API
local en `:8100` (override de desarrollo). El worker de flota es
`si-worker-prod-mac`: corre la **imagen**, sin bind del working tree.

```bash
# Túnel (si no está)
nc -z 127.0.0.1 8110 || autossh -M 0 \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes -N vepathos-tunnel &

cd ~/workspace/vepathos-smart-import
# Sin commit+push, git pull en api-prod/VM no trae nada.
# En ESTA Mac el build usa el working tree: podés buildear cambios locales.

DOCKER_BUILDKIT=1 docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml \
  -f docker-compose.mac.worker.yml \
  build worker

docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml \
  -f docker-compose.mac.worker.yml \
  up -d --force-recreate worker

docker cp data/street_aliases.sqlite si-worker-prod-mac:/data/cache/street_aliases.sqlite

docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml \
  -f docker-compose.mac.worker.yml \
  run --rm worker worker --check
docker logs si-worker-prod-mac --tail 10
```

`up -d` sin `--force-recreate` deja el container con la imagen vieja.

Jobs en vuelo vuelven a la cola (grace 90 s). El lab `:8100`
(`vepathos-smart-import`) no entra: puede estar parado.

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
`restart` **tampoco** trae código nuevo al worker Mac: no hay bind de `./smart_import`.
Para código: [§6.7](#67-actualizar-código-en-la-mac-apunta-a-prod) o [§13.4](#134-paso-3--mac-worker-libpostal-opcional).

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
- [ ] `/data/cache/street_aliases.sqlite` en VM y Mac si el rollout usa alias OSM ([§13.0a](#130a-mapa-de-alias-osm-no-va-en-git))
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

## 13. Actualizar prod — rollout de código

Para pasar de una versión ya corriendo a código nuevo (mismo layout api-prod +
VM + Mac). **No es lo mismo que “deploy desde cero”** (§4–§6): acá asumís que
`.env`, firewall y RouteHub ya están bien.

En `docker ps` de esta Mac:

| Container | Puertos | Rol |
|---|---|---|
| `vepathos-smart-import` | `127.0.0.1:8100` | Lab. **Opcional.** `docker stop vepathos-smart-import`. |
| `si-worker-prod-mac` | `8100/tcp` sin bind | Worker de la flota. Este es el que hay que rebuildar. |

api-prod y la VM no aparecen acá (son SSH). El lab no entra en el rollout.

Igual que el cutter: **una API + N workers**. Cada nodo tiene su imagen.
`git pull` solo no alcanza; hay que **build + `up -d --force-recreate`** en
cada uno. Orden: API primero, después workers.

### 13.0 Receta — cada vez que cambiás código

Copiar y pegar. Detalle y rollback: §13.2–§13.6.

**Qué tocar**

| Cambio | api-prod | VM worker | Mac `si-worker-prod-mac` |
|---|---|---|---|
| Python / compose / schemas | sí | sí | sí |
| Solo docs | no | no | no |
| `street_aliases.sqlite` (no va en git) | no | `docker cp` | `docker cp` |

**0 — esta Mac (repo)**

```bash
cd ~/workspace/vepathos-smart-import
git status
git push origin feat/smart-import-produccion
```

Sin push, `git pull` en los servers no trae nada. El lab `:8100` no hace falta.

**1 — API (api-prod). Primero, para que siga encolando.**

```bash
ssh deploy@178.105.42.199
cd ~/vepathos-smart-import
git fetch origin && git checkout feat/smart-import-produccion
git pull origin feat/smart-import-produccion

DOCKER_BUILDKIT=1 docker compose \
  -f docker-compose.yml -f docker-compose.apiprod.yml \
  build smart-import
docker compose \
  -f docker-compose.yml -f docker-compose.apiprod.yml \
  up -d --force-recreate smart-import

curl -sf http://127.0.0.1:8100/health | python3 -m json.tool | grep -E '"(status|role|state)"'
# esperado: role api, state redis
exit
```

**2 — Worker VM (PBFs). El piso de prod.**

```bash
ssh martin@46.224.217.160
cd ~/vepathos-worker/vepathos-smart-import
git fetch origin && git checkout feat/smart-import-produccion
git pull origin feat/smart-import-produccion

DOCKER_BUILDKIT=1 docker compose --env-file .env.worker \
  -f docker-compose.worker.yml build worker
docker compose --env-file .env.worker \
  -f docker-compose.worker.yml up -d --force-recreate
docker compose --env-file .env.worker \
  -f docker-compose.worker.yml run --rm worker worker --check
docker logs vepathos-smart-import-worker --tail 15
exit
```

Si este rollout usa alias OSM, copiá el sqlite **después** del recreate
(el volume `cache` no se borra):

```bash
scp data/street_aliases.sqlite martin@46.224.217.160:/tmp/street_aliases.sqlite
ssh martin@46.224.217.160 \
  'docker cp /tmp/street_aliases.sqlite vepathos-smart-import-worker:/data/cache/street_aliases.sqlite
   docker restart vepathos-smart-import-worker'
```

**3 — Worker Mac (libpostal). Esta máquina, no SSH.**

```bash
# túnel, si nc falla
nc -z 127.0.0.1 8110 || autossh -M 0 \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes -N vepathos-tunnel &

cd ~/workspace/vepathos-smart-import
git pull origin feat/smart-import-produccion   # si buildeaste antes del push, igual recrear

DOCKER_BUILDKIT=1 docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml -f docker-compose.mac.worker.yml \
  build worker
docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml -f docker-compose.mac.worker.yml \
  up -d --force-recreate worker

docker cp data/street_aliases.sqlite si-worker-prod-mac:/data/cache/street_aliases.sqlite

docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml -f docker-compose.mac.worker.yml \
  run --rm worker worker --check
docker logs si-worker-prod-mac --tail 15
```

No uses `docker compose` a secas acá: eso es el lab `:8100`.

**4 — ¿quedó la flota?**

```bash
curl -s http://127.0.0.1:8110/health | python3 -m json.tool | grep -A30 '"fleet"'
docker logs -f si-worker-prod-mac    # y mandá un import por RouteHub
```

Esperado: `role: api`, ≥2 workers (VM + Mac), colas en 0 si idle,
`config_drift` **solo** el nodo Mac (libpostal).

Jobs en vuelo de un worker que recreás vuelven a la cola (grace 90 s).

### 13.0a Mapa de alias OSM (no va en git)

`data/street_aliases.sqlite` está en `.gitignore`. El geocoder lo lee de
`SMART_IMPORT_STREET_ALIASES` = `/data/cache/street_aliases.sqlite` (volume
`cache`, sobrevive recreate). Sin el archivo, el geocoder arranca; Bélgica
`Avenue Mozart` no encuentra `Mozartlaan` en **índices viejos**.

No hace falta recrear PBF ni índices. El cache de geocode se invalida solo
cuando sube `GEOCODER_VERSION` en el código.

**Copiar el sqlite ya barrido** (desde la Mac que lo generó):

```bash
# VM worker
scp data/street_aliases.sqlite martin@46.224.217.160:/tmp/street_aliases.sqlite
ssh martin@46.224.217.160 \
  'docker cp /tmp/street_aliases.sqlite vepathos-smart-import-worker:/data/cache/street_aliases.sqlite'

# Mac worker (esta máquina)
docker cp data/street_aliases.sqlite si-worker-prod-mac:/data/cache/street_aliases.sqlite

# API local :8100
docker cp data/street_aliases.sqlite vepathos-smart-import:/data/cache/street_aliases.sqlite
docker compose restart smart-import
```

**O barrer en el worker** (tiene los PBF montados; horas si es el mundo entero):

```bash
docker compose --env-file .env.worker -f docker-compose.worker.yml run --rm worker \
  build-street-aliases --only belgium,luxembourg,romania,switzerland \
  -o /data/cache/street_aliases.sqlite
```

Después de copiar/barrer, un restart del worker alcanza (el geocoder abre el
sqlite al instanciarse). `--force-recreate` no borra el volume de cache.

### 13.0 Antes de tocar prod (en tu Mac / repo)

1. **Decidir qué sube:** solo lo commiteado en git. Los cambios locales sin
   commit **no llegan** a api-prod ni a la VM con `git pull`.
2. **Tests mínimos** (repo local, venv **`.venv`** de este repo — ver
   [SETUP.md §10.0](SETUP.md#100-levantar-venv-recordatorio)):

```bash
cd ~/workspace/vepathos-smart-import
source .venv/bin/activate   # o: .venv/bin/python -m pytest ...
.venv/bin/python -m pytest tests/test_broker_concurrencia.py \
  tests/test_queue_consumer.py tests/test_config_roles.py tests/test_fleet.py -q
```

3. **Commit + push** del branch (ej. `feat/smart-import-produccion`):

```bash
git push -u origin feat/smart-import-produccion
```

4. **Elegir `IMAGE_TAG`** — piná la misma fecha en los tres nodos para poder
   rollback. Ejemplo: `IMAGE_TAG=2026.09.14`. Actualizalo en:
   - api-prod → `.env`
   - VM → `.env.worker` (y el `.env` base si hereda umbrales)
   - Mac → `.env.prod.smart.local` (sección worker, `SMART_IMPORT_TARGET=runtime-libpostal`)

   Imagen resultante: `vepathos/smart-import:runtime-2026.09.14` (api/VM) y
   `vepathos/smart-import:runtime-libpostal-2026.09.14` (Mac).

> **`up -d` solo no alcanza.** Si el container ya existía, Docker lo deja
> corriendo con la imagen vieja. Siempre: **`build` + `up -d --force-recreate`**.

### 13.1 Orden de rollout (obligatorio)

```
1. api-prod (API)     ← primero: sigue encolando mientras actualizás workers
2. VM worker          ← el piso de prod (PBFs)
3. Mac worker         ← opcional; podés dejarlo para el final o bajarlo antes
```

Los jobs en vuelo en un worker que recreás **vuelven a la cola** (grace period
90 s). No hace falta ventana de mantenimiento si actualizás de a un nodo.

### 13.2 Paso 1 — api-prod

```bash
ssh deploy@178.105.42.199
cd ~/vepathos-smart-import

git fetch origin
git checkout feat/smart-import-produccion
git pull origin feat/smart-import-produccion

# Opcional: IMAGE_TAG=2026.09.14 en .env (runtime, sin libpostal)

DOCKER_BUILDKIT=1 docker compose \
  -f docker-compose.yml \
  -f docker-compose.apiprod.yml \
  build smart-import

docker compose \
  -f docker-compose.yml \
  -f docker-compose.apiprod.yml \
  up -d --force-recreate smart-import

curl -sf http://127.0.0.1:8100/health | python3 -m json.tool | grep -E '"(status|role|state)"'
docker ps --filter name=vepathos-smart-import --format '{{.Names}} {{.Image}} {{.Status}}'
```

Esperado: `"role": "api"`, `"state": "redis"`, container healthy.

**Comprobar heartbeat nuevo** (commit `19bee4f` en adelante):

```bash
docker exec vepathos-smart-import grep -n heartbeat /app/smart_import/queue/broker.py | head -3
# NO debe aparecer "HEARTBEAT_S = 600"
```

### 13.3 Paso 2 — VM worker (PBFs)

```bash
ssh martin@46.224.217.160
cd ~/vepathos-worker/vepathos-smart-import   # ajustar path si difiere

git fetch origin
git checkout feat/smart-import-produccion
git pull origin feat/smart-import-produccion

# Mismo IMAGE_TAG que api-prod en .env.worker (runtime, no libpostal)

DOCKER_BUILDKIT=1 docker compose --env-file .env.worker \
  -f docker-compose.worker.yml \
  build worker

docker compose --env-file .env.worker \
  -f docker-compose.worker.yml \
  up -d --force-recreate

docker compose --env-file .env.worker \
  -f docker-compose.worker.yml \
  run --rm worker worker --check
# → cola ok | estado ok | archivos ok

docker logs vepathos-smart-import-worker --tail 15
```

Copiar alias OSM ([§13.0a](#130a-mapa-de-alias-osm-no-va-en-git)) si este
rollout los necesita y el volume aún no los tiene.

Desde la VM:

```bash
curl -s --max-time 5 http://178.105.42.199:8100/health | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['status'], len(d.get('fleet',{}).get('nodes',[])), 'workers')"
```

### 13.4 Paso 3 — Mac worker (libpostal, opcional)

```bash
# Túnel (si no está)
pkill -f 'autossh.*vepathos-tunnel' 2>/dev/null
autossh -M 0 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes -N vepathos-tunnel &
nc -z 127.0.0.1 8110 && echo 'tunel OK'

cd ~/workspace/vepathos-smart-import
git pull origin feat/smart-import-produccion

# IMAGE_TAG=2026.09.14 en .env.prod.smart.local (runtime-libpostal)

DOCKER_BUILDKIT=1 docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml \
  -f docker-compose.mac.worker.yml \
  build worker

docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml \
  -f docker-compose.mac.worker.yml \
  up -d --force-recreate worker

docker cp data/street_aliases.sqlite si-worker-prod-mac:/data/cache/street_aliases.sqlite

docker compose --env-file .env.prod.smart.local \
  -f docker-compose.worker.yml \
  -f docker-compose.mac.worker.yml \
  run --rm worker worker --check

docker exec si-worker-prod-mac grep -n heartbeat /app/smart_import/queue/broker.py | head -3
docker logs si-worker-prod-mac --tail 10
```

### 13.5 Verificación E2E en prod

Con túnel Mac (`curl :8110`) o desde api-prod (`curl :8100`):

```bash
curl -s http://127.0.0.1:8110/health | python3 -m json.tool | grep -A30 '"fleet"'
```

Checklist rápido:

- [ ] `deployment.role` = `api`, `state` = `redis`
- [ ] Flota: ≥1 worker (VM); 2 si Mac arriba
- [ ] `config_drift`: vacío sin Mac; **solo nodo Mac** si libpostal
- [ ] Subir CSV de prueba por RouteHub → logs VM/Mac con `listo: normalize/...`
- [ ] Geocode termina → descarga nested con coords
- [ ] Sin `Transport indicated EOF` en logs worker (heartbeat 30 s)

### 13.6 Rollback de una versión (solo código)

Mismo procedimiento con el `IMAGE_TAG` anterior (ej. `2026.09.07`):

1. Cambiar `IMAGE_TAG` en `.env` / `.env.worker` / Mac
2. `docker compose ... up -d` **sin rebuild** — Docker usa la imagen ya buildeada
3. Si borraste la imagen vieja: `git checkout <tag-anterior>` + `build` + recreate

Los jobs en Redis sobreviven un rollback de API/workers si no cambiás
`SMART_IMPORT_QUEUE_PREFIX` ni el token.

---

*Última actualización: arquitectura api-prod + VM worker + Mac libpostal (2026-09).*
