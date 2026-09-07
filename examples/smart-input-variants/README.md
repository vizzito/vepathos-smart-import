# Smart Input — variantes de ingreso (100)

Cada archivo es un escenario de paste/upload tipico (WhatsApp, CSV sucio,
email, JSON parcial, speech-to-text, logs, etc.). 5 ejemplos por variante
(3 de referencia + 2 extras).

Correr: `pytest -q tests/test_smart_input_variants.py`

Fuente de verdad de expectativas: `manifest.json`.

## Gaps documentados (`allow_empty`)

Tras el preproceso free_text (query-string + numeros hablados ES), los gaps
`v06/v12/v25/v52/v69` ya recuperan stops. Quedan como casos de **calidad por
campo** (nombre/address imperfectos en speech) en `expected/*.expected.json`.
