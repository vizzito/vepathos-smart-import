# Plantillas de entorno — Smart Import

Los `.env` reales **no se versionan** (credenciales). Estas plantillas son la
fuente de verdad para crear cada archivo en su máquina.

| Plantilla | Copiar a | Máquina |
|-----------|----------|---------|
| `api-prod.env.template` | `.env` | api-prod (`deploy@178.105.42.199`) |
| `worker-vm.env.template` | `.env.worker` | VM PBFs (`martin@46.224.217.160`) |
| `worker-mac.env.template` | `.env.prod.smart.local` | Mac (worker libpostal vía túnel) |
| `local-dev.env.template` | `.env` | Mac/Linux — API monolito local |

Guía operativa completa: [DEPLOY-PRODUCTION.md](../../DEPLOY-PRODUCTION.md).

En la Mac de desarrollo conviven **dos compose distintos**:

| Archivo env | Compose | Container | ¿Prod? |
|-------------|---------|-----------|--------|
| `.env` | `docker compose` (+ override) | `vepathos-smart-import` | No (`:8100` local) |
| `.env.prod.smart.local` | `worker.yml` + `mac.worker.yml` | `si-worker-prod-mac` | Sí (cola de api-prod) |

```bash
# Ejemplo Mac worker prod
cp deploy/templates/worker-mac.env.template .env.prod.smart.local
# Editar: token, passwords, ROUTE_OPTIMIZER_DATA
```
