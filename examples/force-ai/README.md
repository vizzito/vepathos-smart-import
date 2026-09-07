> **Nota (refactor de extracción):** estos archivos ya NO necesitan el modelo.
> El `normalize` separa la columna compuesta con reglas + `phonenumbers` en
> milisegundos. Se conservan porque siguen siendo los casos más hostiles del
> corpus: si alguno se rompe, se rompió el `FieldExtractionPipeline`.
>
> ```bash
> python -m smart_import normalize -i examples/force-ai/04_marketplace_whatsapp.csv \
>     -o out/mk.csv --phone-region AR
> ```

# Ejemplos que fuerzan el uso de IA (`extract`)

Estos archivos mezclan **nombre + dirección + teléfono** en **una sola columna**.
El mapper los marca como compuestos (`confidence ≤ 0.68`) y ofrece `extract` si
`SMART_IMPORT_AI_ENABLED=true`.

| Archivo | Estilo del blob |
|---|---|
| `01_dash_name_address_phone.csv` | `Nombre - calle N, ciudad - tel …` |
| `02_entregar_a_telefono.csv` | `Entregar a X en Y. Telefono: …` |
| `03_parentesis_cel.csv` | `dirección (Nombre, cel …)` |
| `04_marketplace_whatsapp.csv` | mensaje WhatsApp / ML suelto |
| `05_pipe_messy_en_es.csv` | `Name: … \| Address: … \| Phone: …` |
| `06_paste_ready.txt` | texto pegable en la web |

Cada CSV tiene **5 filas** (~3–4 min de extract en CPU Docker si ~40 s/fila).

## Cómo forzar extract por HTTP

```bash
cd ~/workspace/vepathos-smart-import
FILE=examples/force-ai/01_dash_name_address_phone.csv

RESP=$(curl -sf -X POST "localhost:8100/imports?phone_region=AR" -F "file=@$FILE")
echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("job", d["job_id"]); print("actions", [a["action"] for a in d["next_actions"]]); print("warn", d["report"].get("warnings"))'
JOB=$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

# debe incluir "extract"
curl -sf -X POST "localhost:8100/imports/$JOB/extract?max_rows=5"
curl -sf "localhost:8100/imports/$JOB/progress" | python3 -m json.tool
```

## Textos listos para pegar (web / paste)

### A — guiones
```
Laura Méndez - Av. Corrientes 1234, Buenos Aires - tel 1144556677
Diego Ruiz - Av. Santa Fe 2500, Buenos Aires - tel +54 11 4789-0123
Camila Soto - Callao 900, Buenos Aires - tel 11 5555 1212
```

### B — “Entregar a…”
```
Entregar a Martín Gómez en Paraguay 1500, Buenos Aires. Telefono: 1179356380
Entregar a Sofía López en Libertad 1016, Buenos Aires. Telefono: 1164336233
Entregar a Pablo Fernández en Viamonte 1874, Buenos Aires. Telefono: 1155003351
```

### C — paréntesis + cel
```
Av. del Libertador 202, Buenos Aires (Elena Vargas, cel 1150395740)
Libertad 3495, Buenos Aires (Roberto Díaz, cel 1197722063)
Tucumán 1200, Buenos Aires (Natalia Cruz, cel 1149356150)
```

### D — WhatsApp / marketplace
```
Hola! Soy Carla Benítez, mandame el pedido a Honduras 4800 Palermo CABA, whatsapp +5491155667788 gracias
Buenas, Fernando Acosta - entrega en Scalabrini Ortiz 2100, Buenos Aires - mi cel es 11-6677-8899
Cliente: Valentina Ríos / Dir: Thames 1500, Palermo, Buenos Aires / Tel: 1144559900 / ref: portero
```

### E — pipe EN/ES
```
Name: Emily Johnson | Address: Av. Callao 1500, Buenos Aires | Phone: +54 11 4321-9876 | Notes: leave with doorman
Nombre: Pedro Sánchez | Dirección: Lavalle 800, Buenos Aires | Tel: 1166554433 | Comentario: oficina piso 3
Contact: María Eugenia Torres — Street: Florida 500, Buenos Aires — Mobile: 11 4000 2000 — Zone: Microcentro
```

## Qué tenés que ver

1. Warning: *parece contener varios campos… usa la accion 'extract'*
2. `next_actions` incluye **`extract`**
3. Tras `POST …/extract`, logs con `-> customer_name=… | address=… | phone=…`
4. Preview con columnas separadas (ya no un solo blob)

**Importante:** el proxy de la UI (`vepathos-router-client` → `/api/optimization/smart-import`)
ahora, si detecta columna mezclada / `next_actions.extract`, corre **`extract` → espera → `geocode`**
antes de devolver stops. Sin AI (`extract: false`) geocodea el blob y avisa.
