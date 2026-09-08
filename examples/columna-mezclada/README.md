# Columna mezclada / texto hostil

Estos archivos **sí se usan**: son fixtures de regresión del pipeline
determinístico (reglas + `phonenumbers` + segmenter). El nombre viejo
`force-ai` mentía: ya no hay modelo ni `POST /extract`.

El `normalize` separa nombre + dirección + teléfono en milisegundos. Si alguno
de estos casos falla, se rompió el `FieldExtractionPipeline` / free-text.

```bash
python -m smart_import normalize -i examples/columna-mezclada/04_marketplace_whatsapp.csv \
    -o out/mk.csv --phone-region AR
```

| Archivo | Estilo del blob |
|---|---|
| `01_dash_name_address_phone.csv` | `Nombre - calle N, ciudad - tel …` |
| `02_entregar_a_telefono.csv` | `Entregar a X en Y. Telefono: …` |
| `03_parentesis_cel.csv` | `dirección (Nombre, cel …)` |
| `04_marketplace_whatsapp.csv` | mensaje WhatsApp / ML suelto |
| `05_pipe_messy_en_es.csv` | `Name: … \| Address: … \| Phone: …` |
| `06_paste_ready.txt` | texto pegable en la web |

Cada CSV tiene **5 filas**. Cubiertos por `tests/test_placeholders.py`.

## HTTP (sin extract)

```bash
FILE=examples/columna-mezclada/01_dash_name_address_phone.csv
curl -sf -X POST "localhost:8100/imports?phone_region=AR" -F "file=@$FILE" | python3 -m json.tool
```

El mapper puede marcar la columna como compuesta (`confidence ≤ 0.68` →
`needs_mapping_review`). La separación de campos ocurre en el normalize; no hay
acción `extract` aparte.

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
