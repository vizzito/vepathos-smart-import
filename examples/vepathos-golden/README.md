# Entradas que la web ya acepta (referencia)

Estos archivos (y los de `~/Downloads/input vepathos examples/`) son **inputs** que el
router-client parsea y el backend procesa hoy. No son “el schema de salida” en sí:
son dialectos / completitudes distintas que Smart Import tiene que **aceptar** y
convertir a `vepathos_flat_v1` (flat + nested).

## Regla de producto

- **Lo básico alcanza** para generar un archivo válido: tipicamente
  `lat`+`lng` (o `address` → geocode) y listo.
- **Packages, time windows, dims, peso, zona, etc. son opcionales.**
  Si vienen, se conservan; si no, el nested sale igual con `packages: []`
  o sin esos campos.
- Cuanto más completo el input, más rico el output — pero **nunca** exigir
  paquetes/TW para “pasar”.

Eso coincide con el parser de la web (`coords-only` → stops sin packages;
TW a nivel delivery sin bultos; multi-package cuando hay).

## Completitud observada en los ejemplos

| Input | Tiene packages | Tiene TW | Notas |
|---|---|---|---|
| `basic-with-packages.json` | sí (solo `package_id`) | no | mínimo nested |
| `basic-with-packages-caba.*` | sí + dims | no | canónico rico |
| `vepathos-orders-ba.*` | sí (id+peso) | no | BA chico |
| `simple-coordinates-seattle.csv` | algunos | sí | mezcla filas con/sin bultos |
| `dbo1-with-packages-8k.json` | sí + dims/valor | a veces | escala; sin `delivery_id` |
| `tandil_250_…json` | sí + dims | null | + wrapper optimize (`vehicles`…) |
| `tandil_…ordenes.xlsx` | peso/volumen plano | sí (cols) | dialecto OptimoRoute ES |
| `tommy-lot-198-full.json` | en `extra.packages` | schedules | shape lot/API |
| `shopify/meli/tiendanube-*.json` | varía | — | exports ecommerce |

## Qué hace Smart Import hoy con ellos

| Input | Resultado | Gap |
|---|---|---|
| caba / vepathos-orders / seattle | OK | — |
| dbo1 / tandil nested | OK básico | objeto `time_window` no aplana bien → `needs_review` |
| tandil ordenes xlsx | sale archivo | `ID de orden` → `package_id` (debería `delivery_id`) |
| shopify | 14 stops, 0 packages | OK si “básico sin bultos” es válido |
| tommy lot | sale, pero sin lat/lng | `destination=+lat+lng` no se parsea; `company_id`→`quantity` falso |

## Salida (una sola)

Sigue siendo **un** schema: `schemas/vepathos_flat_v1.json`.

```
entrada (cualquiera de arriba, completa o mínima)
        → normalize
        → flat CSV/XLSX  +  nested {"version":1,"addresses":[...]}
```

Solo se emiten los campos que había (o se pudieron mapear). No se inventan
packages ni ventanas.
