# El camino de una entrega

Cómo funciona Smart Import por dentro: qué hace cada etapa, con qué lógica
decide, y por qué está escrita así. Las referencias son `archivo:linea` para
poder ir al código.

Este documento es el nivel de código. Para el nivel de integración (HTTP,
acciones, UX) está [ARCHITECTURE.md §4](../ARCHITECTURE.md); para operar y
deployar, [SETUP.md](../SETUP.md).

**Dos cosas antes de empezar, porque gobiernan todas las decisiones de abajo:**

1. **No hay modelo de IA en ningún punto.** Todo es reglas, vocabulario curado y
   un índice OSM local. `mapping.ai_used` existe en el contrato pero nunca se
   activa, y `ai_calls` sale siempre en 0. Se probó delegar el mapeo de columnas
   a un modelo y devolvía el schema del prompt en lugar de razonar sobre él
   (`mapping/__init__.py:8`).
2. **El error que el sistema más teme no es perder un dato: es un pin confiado y
   equivocado.** Está escrito literal en `addresses/heuristic.py:82`. Perder una
   dirección se ve —queda ámbar, alguien la revisa—; un pin seguro treinta
   cuadras más allá no se ve, y el camión sale igual. Casi todo lo que sigue
   —`_cue_is_street`, `road_is_suspicious`, los geofences, el gate del scorer—
   existe para elegir el error visible sobre el invisible.

---

## 1. El mapa

Hay **dos entradas** y convergen antes de geocodificar:

```
archivo
  │
  ├── TABULAR (csv/xlsx/json con headers reconocibles)
  │      READ → DETECT (mapear columnas) → ─┐
  │                                          │
  └── TEXTO LIBRE (paste de WhatsApp, txt en prosa)
         READ → segmentar → clasificar → extraer campos → ─┤
                                                            │
                                     ┌──────────────────────┘
                                     ▼
                        el MISMO objeto de schema
                                     │
                    NORMALIZE → ASSEMBLE → EMIT
                                     │
                          (opt-in) GEOCODE
```

El orquestador es `run_normalize` (`pipeline.py:72`), la única pieza que conoce
el orden de las etapas. Que los dos caminos terminen en el mismo objeto es
deliberado (`extraction/address_parts.py:1`): de ahí en adelante hay un solo
código que mantener.

**El geocode nunca es parte de normalize.** Es un segundo request explícito
(`POST /imports/{id}/geocode`), porque el usuario primero confirma el mapeo y la
ciudad.

---

## 2. READ — de bytes a tabla

**El formato no se decide por la extensión.** `detect_format`
(`readers/base.py:82`) lee 8 bytes: `PK\x03\x04` es xlsx, la firma OLE2 es xls.
Si la extensión dice Excel y los magic bytes no lo confirman, la extensión
miente y se degrada a texto (`:98`).

**Encoding** (`text_reader.py:23`): BOM → UTF-8 → `charset_normalizer` →
`cp1252`. Se abre con `errors="replace"` para que un byte suelto no tumbe el
import.

**Delimitador** (`text_reader.py:43`): para cada candidato (`, ; \t |`) cuenta
campos por línea, toma el conteo modal y calcula qué fracción de líneas lo
respeta. El score es `campos × consistencia + consistencia × 2`, o sea **la
consistencia manda y el número de campos desempata**. `csv.Sniffer` solo no
alcanza: una dirección entrecomillada con comas le hace confundir `;` con `,`.

**Fila de encabezado** (`readers/base.py:111`): puntúa las primeras 12 filas con
`densidad×2 + distintos×2 + cortos − numéricos×3`. Una fila mayormente numérica
queda descalificada. Esto resuelve gratis el **preámbulo sucio** (título, logo,
fecha antes de la tabla): lo que quedó arriba se descarta y se anota en el
reporte como `N fila(s) de preambulo descartadas`.

### ¿Un `.txt` es tabla o texto libre?

`classify_text_mode` (`detection/text_mode.py:152`). Son **seis puertas AND**:
mínimo 3 líneas, algún delimitador que parta en 2+ campos, consistencia ≥ 0.80,
al menos 3 líneas compartiendo el conteo, ≤ 20% de líneas en prosa, y un header
plausible en la primera línea.

La asimetría es a propósito (`text_mode.py:10`): **romper un texto libre pierde
datos; tratar una tabla como texto libre solo cuesta parsing.** Ante la duda,
texto libre. `is_prose_line` mira forma, no comas —viñeta, oración con
puntuación final y 12+ palabras, o saludo del léxico multiidioma—, porque un
paste de WhatsApp no es un CSV por tener comas.

Si sale texto libre, el archivo se devuelve como **una sola columna `document`
con el texto entero** y el mapper no corre.

---

## 3. DETECT — mapear columnas

`RuleSchemaMapper.detect` (`mapping/mapper.py:31`) junta cuatro señales por cada
par (columna, campo del schema):

| Señal | Rango | Dónde |
|---|---|---|
| Nombre exacto / alias del schema | 1.00 / 0.99 / 0.97 | `mapping/rules.py:7` |
| Fuzzy sobre el nombre (solo si nada llegó a 0.97) | 0.78–0.95 | `mapping/fuzzy.py:14` |
| Contenido de las filas | ≤ 0.93 (0.95 coords) | `mapping/heuristics.py:223` |
| Contexto de las otras columnas | 0.80 | `mapping/mapper.py:173` |

La convención que ordena todo está en `heuristics.py:27`: **los scores dicen
cuánta evidencia hay, no si alcanza.** El corte lo pone el piso por nivel, y
subir un score para "pasar el corte" es mentirle al reporte.

Tres ajustes al combinar:

- **Acuerdo nombre + contenido**: `+0.08`.
- **Fuzzy contradicho por el contenido**: `×0.6`. El caso real: "Ontvanger"
  (destinatario en neerlandés) se parecía 0.82 a "container" y llevaba la
  columna de nombres a `packaging` (`mapper.py:63`). No se descarta —baja del
  piso pero **sigue visible en el reporte con su motivo**.
- **Nombre lat/lng contradicho por los valores**: `×0.55`, o `×0.35` si son
  enteros ("Nylon" se parece a "lon", y 1..10 no es una longitud geográfica).

La asignación es **greedy global** (`mapper.py:118`): todos los pares ordenados
por score, 1 columna → 1 campo, y cada candidato tiene que superar el piso de su
nivel.

### El piso es distinto por nivel

| Nivel | Piso | Derivación |
|---|---|---|
| `delivery` | **0.70** | `review_threshold + 0.00` |
| `timewindow` | **0.65** | `review_threshold − 0.05` |
| `package` | **0.60** | `review_threshold − 0.10` |

Porque **equivocarse no cuesta lo mismo** (`config.py:119`): un `address` mal
mapeado manda el camión a otra dirección; un `weight_kg` mal mapeado es un
número feo en un reporte, que se ve y se corrige. Un solo piso obliga a elegir
entre perder pesos o arriesgar destinos.

Que sean **relativos** a `review_threshold` y no absolutos también es
deliberado: mover el knob global mueve los tres y la escalera entre ellos no
cambia.

Una columna asignada por debajo de `auto_accept_threshold` (0.90) entra en
`ambiguous` y la UI la ofrece para corregir; la corrección manual pisa con
confianza 1.00. Las columnas que no ganaron ningún campo se reportan como
`unmapped` y no bloquean nada.

### Una columna que mezcla varios campos

Cuando una columna mapeada a `address` trae teléfonos o textos largos embebidos
(`"Juan Perez - Corrientes 1250 - tel 11..."`), `_flag_composite_columns`
(`mapper.py:227`) la detecta por dos fracciones —celdas con teléfono adentro ≥
0.5, o celdas de >45 chars con 6+ espacios ≥ 0.7— y hace tres cosas: capa la
confianza a 0.68 (así cae en `ambiguous`), avisa al operador, y **marca la
evidencia con el literal `"varios campos"`**.

Esa marca **es el contrato**: `_expand_composite_column` (`pipeline.py:359`)
busca exactamente ese string más `target == "address"`, y pasa **solo esa
columna** por el pipeline de texto libre. El resto del archivo sigue el camino
rápido. Después se re-mapea de cero, porque ahora hay columnas que antes no
existían.

---

## 4. TEXTO LIBRE — de un paste a filas

`FreeTextExtractor.run_document` (`extraction/free_text.py:125`): segmentar el
documento, clasificar cada segmento como entrega o ruido, y extraer campos de
los que quedaron.

**La clasificación** (`detection/delivery_classifier.py`) puntúa cada línea y
compara contra `delivery_accept_threshold` (0.55). Medido sobre 100 entregas
reales contra 18 líneas de saludo: el ruido topa en 0.05 y la mediana de las
entregas es 0.83 — dos poblaciones separadas por un abismo, así que cualquier
valor entre 0.20 y 0.55 da el mismo resultado.

**La extracción de campos** (`extraction/pipeline.py:73`) es un pipeline de 8
pasos sobre un `Canvas`, y el orden importa: teléfono, email y paquetería corren
**antes** que la dirección. El `Canvas`
(`extraction/canvas.py:29`) reemplaza lo ya consumido **por espacios,
manteniendo el largo**, así que los offsets siguen siendo válidos y la dirección
no puede robarse un número de teléfono.

---

## 5. ADDRESS — el camino de una dirección

Es la pieza más delicada del pipeline y donde vive casi toda la lógica no
obvia.

```
texto crudo
  ↓  aislar el tramo que es dirección          (ancla calle+altura)
  ↓  parsear en componentes                     road, house_number, unit, …
  ↓  [gate] libpostal, solo si hace falta       fusiona, no reemplaza
  ↓  puntuar                                    ¿es geocodificable?
  ↓  volcar a campos del schema
  ↓  componer el `address` VISIBLE
  ↓  enriquecer con contexto geográfico
  ↓  construir la QUERY interna                 (nunca se persiste)
  ↓  geocoder
```

### 5.1 Aislar: el ancla es calle + altura

`find_street_span` (`heuristic.py:238`) corre **cinco patrones** sobre todo el
texto y acumula candidatos. Desde el ancla, `_expand_left` / `_expand_right`
(`extraction/address_extract.py:88`, `:102`) estiran el span hacia los lados
mientras lo que encuentran siga pareciendo dirección.

Los cinco patrones, con su **boost** (más negativo gana):

| Patrón | Captura | Boost |
|---|---|---|
| `_NUMBERED_STREET` | vía que lleva número en el nombre: `Calle 50 nro 1234`, `Carrera 7 # 45-10` | −4 |
| `_NUMBER_THEN_CARDINAL_ORDINAL` | `350 NE 1st Ave` (el `NE` no es la calle) | −4 |
| `_NUMBER_THEN_ORDINAL_ROAD` | `1171 1st Ave` | −3 |
| `_ROAD_THEN_NUMBER` | `Corrientes 100`, `Santa Fe al 137`, `Av. Paulista, 1578` | −2 con token de vía |
| `_NUMBER_THEN_ROAD` | `23 MG Road`, `507 Broadway` | −2 con token de vía / 0 |

**El desempate** (`heuristic.py:250`) es `min` por la tupla `(boost, posición)`:
primero gana el patrón más específico o el que tiene token de vía; a igual
boost, **gana el que empieza más a la izquierda**.

Ese segundo criterio produjo el peor bug que tuvo el extractor, y vale contarlo
porque explica la forma del código (`heuristic.py:69`). El lookbehind de los
patrones `number+road` era `(?<!\d)`: la altura solo tenía prohibido venir
pegada a otro **dígito**, no a una letra. Entonces en
`"mensajero1 Av Corrientes 1800 2B"` el `1` de `mensajero1` pasaba el filtro,
armaba el candidato `road='Av Corrientes'` + `num='1'`, **empataba en boost** con
el candidato correcto (los dos traen el token `av`) y ganaba por posición.
Resultado: `Av Corrientes 1` en vez de `1800`, treinta cuadras, con la misma
confianza. Y disparaba con todo lo que abunda en un paste de despacho: ids de
mensajero, móviles, rutas, zonas, números de pedido.

Hoy el lookbehind es `_NUM_START = (?:(?<!\w)|(?<=[nN][ºª°]))` (`heuristic.py:91`).
La segunda alternativa existe porque en `"Nº1234 Calle Falsa"` la altura **sí**
viene pegada a una palabra, y Python cuenta `º`/`ª` como `\w`.

**`_trim_road`** (`heuristic.py:166`) recorta el relleno a la izquierda del
nombre de calle, y su regla es contraintuitiva: acepta **toda** palabra
alfabética salvo un conjunto chico y cerrado de palabras de narración. Antes
exigía que la palabra fuera capitalizada o token de vía, y los exports reales no
cumplen eso —`Av cabildo 834`, `Av. Ramos mejia 1358`—: **una sola minúscula
tiraba la dirección entera, y era el 19% de las que sí tenían altura.**

El precio de esa regla es que el nombre del cliente pegado adelante también es
"toda palabra alfabética". Por eso `extract_address` busca el ancla con **lo ya
consumido como corte**, no como blanco (`canvas.remaining(fill=CONSUMED)`): en
`"Juan Lopez 1133334444 Gorriti 4500"` el teléfono ya salió del texto y la calle
no puede cruzarlo, así que queda `Gorriti 4500` y `Juan Lopez` le llega a
`extract_customer_name`. Vale igual para un email o unos bultos en el medio. Sin
nada consumido entre los dos (`"Juan Lopez Gorriti 4500"`) sigue siendo ambiguo
—se escribe igual que `Juan B Justo 4500`— y no se parte.

### 5.2 Parsear: el primero en reclamar, gana

`HeuristicAddressParser.parse` (`heuristic.py:262`) reclama componentes en un
orden fijo, y mantiene una lista de spans `consumed`. El closure `take`
(`:271`) implementa el contrato: si el campo ya tiene valor, sale; si el span
del match **solapa** algo ya consumido, descarta el candidato en silencio.

| # | Componente | Por qué en esa posición |
|---|---|---|
| 1 | `road` + `house_number` | **primero, y es la decisión clave** (abajo) |
| 2 | `road` sin altura | sin esto, `"Yerbal, CABA"` caía en `suburb` y el gate pedía libpostal para copiar la misma palabra |
| 3 | `postcode` | |
| 4 | `level` | `3er piso`, `2nd floor` |
| 5 | `unit` con etiqueta | `depto 4B` — exige prefijo a propósito |
| 6 | `unit` sin etiqueta | `1A` pegado a la altura; se ancla al final del span de la calle |
| 7-8 | `landmark`, `neighbourhood` | lo que no entiende, en vez de tirarlo |
| 9 | `postcode` de 4 dígitos al final | necesita que la altura ya esté decidida |
| 10 | `suburb`, `city` | trabaja sobre "los segmentos que sobran" |

**Por qué la calle va primero** (`heuristic.py:283`): un `03845` pegado al nombre
de calle es la altura, no un ZIP. Pero `_POSTCODE` acepta `\d{5,6}`, así que
calificaría. Si el CP corriera antes, marcaría ese span como consumido y la
dirección quedaría **sin calle Y sin altura**: `"AV JUAN DE GARAY 03845"` →
solo un postcode. El orden no es estético, es el que evita perder los dos campos
que más importan.

**El parser nunca inventa componentes** (`addresses/base.py:55`). `state`,
`country` y `building` el heurístico no los produce nunca: solo pueden entrar
por libpostal.

### 5.3 El gate de libpostal

El heurístico **siempre** corre. libpostal es un enhancer *on-demand* que se
consulta solo cuando hace falta, porque cuesta ~2,6 GB de RSS y ~3 s el primer
uso. El import es lazy: `is_installed()` usa `find_spec` y nunca carga datos.

**Cuándo entra** — solo dos razones (`addresses/gate.py:48`):

- `missing_road`: el heurístico no encontró calle.
- `suspicious_road`: la encontró pero es sospechosa. Eso es: vacía, una palabra
  del blocklist, **una sola palabra que es token de vía sin nombre propio**
  (`Calle`, `Road`), o empieza con un prefijo de parcela/manzana/lote/plot.

No hay una condición "India", aunque se la mencione así. Lo que hay son los
prefijos de parcela/plot en los recursos —que cubren el direccionamiento sin
calle típico de India— y filtros de plausibilidad **a la salida**, calibrados
contra basura de libpostal ahí (`enhance.py:87`).

**Qué hace con el resultado: fusiona, nunca reemplaza** (`enhance.py:141`).
Arranca del heurístico y por cada campo del enhancer:

- si el campo estaba **vacío**, lo completa;
- si estaba **ocupado**, solo lo pisa cuando la road del heurístico era
  sospechosa **y** el campo es `road` o `house_number`.

Y cada valor pasa por un filtro de plausibilidad. `city`, `suburb`, `postcode`,
`level`, `unit` se aceptan siempre; `building` **no**, y está fuera de la lista
segura por una razón concreta: *libpostal mete nombres de persona*
(`enhance.py:28`). `house_number` necesita al menos un dígito y no puede empezar
con un prefijo de unidad (`flat 14b`). Todo rechazo queda en la evidencia.

Las stats (`enhance.py:218`) cuentan `helped` solo cuando el enhancer aportó algo
**material**, y material está definido estrictamente como `{road, house_number}`:
completar `city` no cuenta como "ayudó", porque no cambia el pin.

### 5.4 Puntuar: ¿esto es una dirección?

`AddressCandidateScorer` (`addresses/scoring.py`) suma señales:

| Señal | Peso |
|---|---|
| Número de puerta junto a un nombre | +0.45 |
| El parser identificó un componente preciso | +0.30 |
| Token de vía (`Av.`, `Road`, o sufijo tipo `Hauptstrasse`) | +0.25 |
| Código postal | +0.20 |
| Cola de localidad separada por comas | +0.15 |
| 3+ palabras | +0.10 |

**No hay penalizaciones.** Solo dos cortes duros: menos de 4 caracteres da 0.0, y
el total se recorta en 0.99 (los pesos suman 1.45 a propósito). Cada score sale
con su tupla de evidencia, porque *sin las razones no se pueden mejorar las
reglas después* (`scoring.py:4`).

Como el objeto parseado suma +0.30 que el string crudo no tiene, **puntuar el
parseado en vez del texto sube el score** — y es lo que hace `extract_address`.

Este scorer es el **mismo** que usan el mapper de columnas y el geocoder, a
propósito, para que no se desincronicen (`heuristics.py:95`).

### 5.5 El `address` visible

`compose_address_from_parts` (`normalization/address.py:119`) es donde tabular y
texto libre convergen: cualquier formato de entrada termina en el mismo string.

- **Sin calle no compone nada**, ni con ciudad y CP: no es geocodificable como
  puerta y confundiría al gate y a la UI.
- **Orden calle/altura según el país**: `450 1st Ave` para US, `Corrientes 450`
  para el resto. Al revés, el parser se come el ordinal y no geocodifica.
- No duplica la altura si ya está en la calle, y compara por token exacto para
  que `4` no matchee `400`.
- Cierra deduplicando segmentos (`CABA, CABA` → `CABA`) y **forzando el país al
  final**.

Solo escribe si el resultado es **más rico** que lo que había (`:152`).

### 5.6 Enriquecer con contexto geográfico

`maximize_address_for_geocode` (`normalization/address.py:386`) agrega
provincia/país cuando faltan. Las fuentes, en orden de prioridad:

1. el `address` ya deduplicado;
2. **pistas del propio texto** (el primer grupo de expansión cuyo cue matchee);
3. tokens que pasa el caller (ciudad/provincia/país del depot);
4. el país implicado por cualquiera de los anteriores;
5. **`phone_region`, que es la fuente más débil de todas.**

Las dos protecciones son el corazón de la función:

**`phone_region` se ignora en silencio si el texto ya implica otro país.** El
motivo está en el docstring: *un paste de Miami no puede terminar en Argentina
solo porque RouteHub mandó `phone_region=AR`*.

**`_cue_is_street`** (`:192`): una pista de localidad **pegada a una altura** es
el nombre de la calle, no la ciudad. Sin esto, `"Av Callao 1219"` se enriquecía
con `Peru` y la query salía a buscar la dirección al país equivocado. Media
ciudad del mundo comparte nombre con una calle de otra —Callao, Asunción,
Córdoba— y el gazetteer no puede distinguirlas; **la posición sí.**

**Las pistas de GeoNames solo cuentan en posición de localidad**
(`_geonames_cue_position`, `:234`). El JSON curado son ~70 grupos elegidos a mano; GeoNames
son 32 mil ciudades de 15k+ habitantes, y muchas se llaman como un apellido o
una calle de otro país: Lopez y Rodriguez (Filipinas), Castro (Brasil), Medina
(Irak), Cabildo (Chile). Con el nombre pegado a la calle,
`"Juan Lopez Gorriti 4500, 3B"` salía con `Philippines` y el geocoder vetaba los
candidatos argentinos. Ahora una pista de GeoNames:

- en **su propio segmento** (no el primero, que es la calle; se admiten CP y
  sigla: `Campinas SP 13010`, pero no un tipo de vía: `Elgin Rd 114` es calle)
  cuenta, y puede pisar el país del depot o de `phone_region` **salvo que ese
  país también tenga una ciudad con el mismo nombre**: `Av Massey 100, Lincoln`
  con AR es Lincoln (Buenos Aires), no Nebraska;
- **cerrando la dirección sin coma**, después de la altura
  (`123 Main St Springfield`), es evidencia débil: completa el país solo si ni
  el depot ni `phone_region` dicen otro;
- **en cualquier otro lugar** (adentro del nombre o de la calle) no cuenta;
- si la pista **es un nombre de país** (`Mexico` también es un pueblo de
  Filipinas) no cuenta nunca: el segmento es el país.

Las pistas curadas siguen con la regla de siempre. Medido sobre las 49.748
direcciones de los corpus con su país verdadero y `phone_region` = ese país, las
salidas con un país equivocado bajaron de 5.740 a 647. El costo está en el caso
contrario —direcciones extranjeras con `phone_region=AR`—: 1.313 (10%) dejan de
recibir su país, casi todas porque la "ciudad" era el nombre de la calle
(`Rue de Lausanne`) o venía primera (`OSLO, 37, Heimdalsgata`).

**Un solo país por query.** Antes salían `Campinas, Brazil, Argentina` o
`Carrera 7 45, Colombia, Argentina`: dos países y el geocoder vetando a uno de los
dos. Ahora el país de `phone_region` no se agrega si ya lo implica un país de
GeoNames (`Brazil`, aunque el nombre preferido sea `Brasil`) o uno **escrito** en
su propio segmento (`Colombia`; no `Georgia`, que es un estado, ni `NICARAGUA,
4824`, que es la calle). Un país escrito además le gana a cualquier pista, y el
mismo país con dos nombres (`Czech Republic` / `Czechia`) va una vez. Las pistas
curadas tampoco pisan una homónima del país explícito: `San Isidro` con AR es el
partido y con PE sigue siendo Lima; `Paris, TX` con US no es Francia. Con
direcciones extranjeras y `phone_region=AR`, las queries con 2+ países bajaron de
6.413 a 203.

El recorrido de las ~32 mil filas no se hace entero: `_candidate_rows` indexa cada
pista por su primer token y solo evalúa las filas que pueden aparecer, en el
mismo orden (35 ms → 0,8 ms por dirección; idéntico sobre 149.244 llamadas).

Todo el matcheo es **por token, no por substring**: `ne` no puede pegar en
`biscayne`, y `Argentina` no está presente en `Avenida Patricias Argentinas`.

### 5.7 La query interna

**El `address` que cargó el cliente y la query que va al geocoder son dos
strings distintos.** El primero se persiste; la segunda se arma en memoria por
fila y **nunca se escribe** (`geocoding/query.py:1`).

`build_geocode_query` (`query.py:100`) toma el `address` visible, le agrega la
localidad que traiga la fila (zone → city → region → postcode → country) y
después los tokens del depot, **siempre solo lo que falta**.

Los tokens del depot salen únicamente de campos **estructurados**
(`depot_context.py:114`): el módulo nunca parsea el `address` libre del depot,
porque eso trae calle y barrio y ensucia la query de cada entrega.

Un detalle que cierra el diseño: **el gate del scorer se aplica sobre el
`address` visible, no sobre la query enriquecida**. Enriquecer no puede hacer
pasar una dirección que no tenía evidencia propia.

**Pero la forma del texto no es la única evidencia** (2026-09-15). El scorer
mira forma: `MORENO 245` (una palabra + 3 dígitos) da 0.45 y se descartaba sin
buscar, mientras `Remito 5561` da 0.65 porque los 4 dígitos se leen como código
postal. Antes de descartar, el runner le pregunta al índice
(`LocalOSMGeocoder.street_evidence`): si la calle existe, se resuelve contra una
calle real (ver 9.3 bis) o el texto es una esquina de dos calles del extract, la
fila se busca. Solo cambia filas que el gate iba a tirar; `Gonzalo` o `Caja 12`
siguen afuera porque el índice no tiene nada que las explique.

---

## 6. NORMALIZE — limpiar y clasificar

`RowNormalizer.run` (`normalization/row_normalizer.py:114`) recorre todas las
filas: coerción por tipo, descarte de vacías, libras → kg, celdas de bulto
escritas a mano, composición de `address`, validaciones, timezone, teléfono,
estado.

El invariante del módulo de valores es el **round-trip exacto**: un archivo que
ya viene en formato Vepathos tiene que salir idéntico, sin `22.0` donde decía
`22`. El separador decimal se resuelve por posición del más a la derecha, así que
`1.234,56` y `1,234.56` dan los dos 1234.56.

Tres reglas que definen el criterio de la etapa:

- **Teléfonos: si hay duda, se preserva el original.** Devuelve E.164 solo si el
  número es válido para la región; si es inválido lo deja como vino. Nunca
  descarta.
- **Coordenadas invertidas: se avisa, no se corrige.** Si `lat` excede 90 pero
  cabe como longitud y `lng` cabe como latitud, se marca la fila y sale un
  warning. Invertir en silencio manda entregas a otro país.
- **Celdas de bulto con palabras** entran al parser de paquetería solo si tienen
  letras, porque `coerce` sobre `"3 cajas de 10 lb c/u"` devuelve 310 —
  concatena los dígitos.

Nótese el contraste con DETECT: allá los rangos toleran outliers para que una
fila corrupta no reclasifique la columna entera; acá, fila por fila, no hay
tolerancia.

### Los cuatro estados

| Estado | Condición |
|---|---|
| `ok` | tiene `lat` **y** `lng` |
| `needs_geocode` | sin coordenadas, pero con `address` |
| `ignored` | sin destino y **sin ninguna seña de identidad** |
| `invalid` | sin destino, pero con nombre o teléfono |

La distinción `ignored` / `invalid` es el punto fino: contar el pie de página del
cliente como "entrega fallida" hace parecer roto un archivo que está bien. Un
`delivery_id` suelto puede ser "Totales:" y una cantidad suelta puede ser la
suma del pie.

---

## 7. ASSEMBLE — filas planas a entregas con bultos

`assemble` (`assemble/grouping.py:82`) absorbe las dos formas en que llega el
mismo dato: una fila por bulto repitiendo los datos de la entrega, o una fila por
entrega con "bultos: 3". Ambas terminan en la misma estructura.

**Agrupa solo por `delivery_id`** (`grouping.py:67`). Mismo id → un stop con N
packages. Distinto id → stops distintos **aunque compartan address o lat/lng**:
un edificio con 20 pedidos son 20 stops, no uno. Sin id → cada fila es su propia
entrega; no se fusiona por geo, porque eso mezclaba pedidos distintos en el mismo
pin.

> **`schema.group_by` es declarativo, no ejecutable.** Está en el JSON y aparece
> en el log y en la descripción del schema que expone la API, pero `group_key`
> recibe `schema` y no lo consulta: el agrupamiento está fijo en `delivery_id`.
> Cambiarlo en el JSON no cambia el comportamiento.

**Invariante de entrada**: cuando una fila llega acá, `weight_kg` ya es el peso
de **un** bulto. En texto libre lo reparte el extractor aguas arriba; en tabular
la columna ya es unitaria. Por eso el único caller pasa `weight_is_total=False`
en los dos caminos. Repartirlo acá otra vez rompía el round-trip: el flat se
relee como tabular después de geocodificar, así que el peso se multiplicaba en
cada vuelta.

---

## 8. EMIT — las salidas

| Salida | Ruta | Cuándo |
|---|---|---|
| Flat CSV / XLSX | `output_path` | si se pidió `flat` |
| Nested JSON | `{stem}.nested.json` | si se pidió `nested` |
| Report JSON | `{stem}.report.json` | **siempre** |

Las columnas del flat son solo lo que se mapeó, en orden de schema. El nested
tiene la forma canónica `{"version": 1, "addresses": [...]}` que ya consume el
optimizador.

El **report** es la trazabilidad completa: cada decisión del READ (encoding,
delimitador, fila de header, preámbulo descartado), el mapping entero con
confianza/método/evidencia por columna, las columnas sin mapear y a revisar, los
conteos por estado, los tiempos por etapa, y hasta 200 filas con sus issues
atados al campo exacto —para que la UI marque la columna y no muestre un texto
suelto.

---

## 9. GEOCODE — de `address` a coordenadas

### 9.1 Preparar el índice

**Elegir el PBF** (`pbf_registry.py:289`): el bbox se lee **del nombre del
archivo**, así que no hay que abrir un PBF de 400 MB para saber qué contiene. La
prioridad es extract que contenga el bbox pedido → extract que cubra el punto →
PBF de país → refinar con `zone_hint`.

Dos reglas con historia de bug: cuando dos países solapan (Río de la Plata) gana
**el de mayor margen interior**, no el más chico en disco —antes Uruguay (~56 MB)
le ganaba a Argentina (~400 MB) y CABA se geocodificaba contra calles
uruguayas—; y `zone_hint` matchea **estricto**, nunca por substring, porque
`"ar" ∈ "ashmore-cartier"` rompía Tandil.

**Cortar el extract** (`extract.py:309`): si hace falta, se recorta del PBF de
país con `osmium` — bbox del depot + 15 km, con techo de 80 km. El invariante del
módulo es *nunca construye `argentina.sqlite`*: **un país entero jamás se
indexa.**

**Construir el índice** (`osm_index.py:180`): SQLite con `places` + FTS5 +
RTree, atómico y bajo `flock` (escribe a temporal y `os.replace`), así que la
ruta definitiva no existe hasta que el build termina. La clave de búsqueda es un
único campo `normalized_text` que concatena altura, calle, nombre, barrio,
ciudad, provincia, CP y país. Las calles se indexan por su **centroide**, que es
lo correcto cuando solo se conoce el nombre.

### 9.2 Buscar y puntuar

**Solo los tokens de la CALLE entran al MATCH** de FTS (`osm_geocoder.py:107`).
Los del depot se anexan para *puntuar*, pero si entraran a la búsqueda,
`"919" AND (corrientes OR argentina)` devolvería 40 "Avenida Argentina" y cero de
Corrientes.

**AND primero, OR solo si la calle no apareció**: AND distingue
`"Fragata Sarmiento"` de `"Sarmiento"`; si AND ya trajo la calle no se afloja,
porque el OR reintroduciría la homónima y la proximidad al depot la elegiría.

Hay además una búsqueda de alturas cercanas, porque bm25 tiene un problema
concreto: `"corrientes"` devuelve las alturas 1 a 200 y Corrientes 919 no aparece
nunca. Si se pidió altura y ningún candidato la resolvió, se buscan calles
candidatas y sus alturas ordenadas por `|house_number − pedida|`.

**El score** es un promedio ponderado: calle 0.55, CP 0.18, localidad 0.15,
nombre 0.07, proximidad 0.05. CP y proximidad se **excluyen del denominador** si
no aplican, así que no penalizan por ausencia.

**La altura no entra en el promedio**: es bonus multiplicativo después (+0.15
exacta, +0.07 parcial, ×0.88 si se pidió y falta). Si pesara como las demás, una
calle acertada al 100% sin altura exacta caía debajo del umbral y se descartaba
—y un match a nivel calle es un resultado útil.

`street_match` es **la componente de calle**, no el total, y se evalúa como
guarda aparte. Sus reglas devuelven 0.0 duro, no un fuzzy degradado: compara
calle contra calle (sin esto `"Olazabal 1728 Belgrano"` matcheaba la calle
Belgrano al 1.00 y el pin quedaba a 5 km), saca el tipo de vía antes de comparar
(`"Avenida Las Heras"` vs `"Avenida Caseros"` daba 0.81 porque el token
`avenida` dominaba), y si la calle pedida tiene tokens que el candidato no
tiene, es 0.0.

**Una inicial es el nombre completo** (`_align_initials`, `_fts_name_term`): OSM
guarda las alturas de CABA bajo `Avenida Juan Bautista Justo` y la gente escribe
`Juan B Justo`. La `b` exacta no entraba al MATCH (ahora se busca `"b"*`) y quedaba
como token que el candidato no tiene (calle 0.0): la avenida entera salía sin
pin, o en la homónima `Juan B. Justo` de otro partido. Solo se alinea con el mismo
número de palabras y todas las demás iguales; una letra sola como nombre entero
(`AVE U`) sigue exacta.

**El mejor candidato se elige por `(street, house_number, score_total)`** — el
total es el **último** criterio de desempate. Antes, ordenando por total,
`"350 NE 71st"` (altura exacta, calle 0.80) le ganaba a `"300 NE 1st"` (calle
correcta) porque la proximidad al depot desempataba.

### 9.3 El árbol de status

En orden estricto:

1. **La calle no coincide** (`street_match < 0.70`) → sin pin, o pin ámbar si el
   score alcanza para soft-reject. Es la **primera** condición porque coincidir
   solo en la altura es el falso positivo más caro del índice:
   `"Av. Pueyrredón 359"` resuelve a `"Venezuela 359"`, a kilómetros.
2. **Altura exacta pero calle imperfecta** → ámbar **con pin, siempre**. Es la
   única rama que ignora la configuración de soft-reject.
3. **Match a nivel calle** (sin altura, o pedida y no resuelta) → ámbar si
   supera el piso de 0.60. **Nunca puede ser `matched`, por alto que puntúe.**
4. **`>= match_threshold` y `>= valid_band`** → `matched`.
5. **`>= low_confidence_threshold`** → `low_confidence`.
6. resto → sin pin.

Después el status todavía puede cambiar dos veces: por los geofences, y por la
guarda de banda.

### 9.3 bis Rescates cuando no hubo pin

`LocalOSMGeocoder.geocode` corre la búsqueda de arriba y, **solo si no dio un
pin publicable**, prueba cuatro rescates. Esa condición de entrada es lo que los
hace locales: una fila que hoy geocodifica no pasa por acá.

1. **Número pegado** (`Crisantemos1904` → `Crisantemos 1904`). Ámbar.
2. **Orden inglés con localidad** (`172 Avenue Charles Michiels, Bruxelles, Belgium`
   → `Avenue Charles Michiels 172, …`). Con 3+ segmentos detrás del número, el parser lo
   lee como un compuesto invertido de OpenAddresses CABA; el enrich del depot
   agrega justo esos segmentos.
3. **Calle resuelta contra el índice** (`street_resolver.py`): título abreviado
   (`Gral San Martin`), apellido (`dufau` → `Intendente Dufau`), truncada
   (`Trabajadores Mun`), iniciales (`Lisandro dlt`), nombres salteados
   (`jose artigas` → `General José Gervasio Artigas`), ruido adelante/atrás
   (`Mariela entre rios`; una palabra casi igual a una de calle no cuenta como ruido:
`Sladanha` es `Saldanha`) y typo fonético (`Lungui` → `Avenida Lunghi`). Guardas:
   **unicidad** (si dos calles explican igual, no se elige), **nunca pisar una
   calle que existe** (`Sarmiento` no pasa a `Fragata Sarmiento`) y **no resolver
   una localidad cercana** como apellido (`Uccle`). Siempre ámbar.
4. **Esquinas** (`intersection.py`): `Garibaldi y Montiel`, `X esq. Y`, `X & Y`,
   también entre paréntesis. El cruce sale de las polilíneas si el índice las
   tiene; si no, del par de nodos más cercano (≤ 40 m). Ámbar. Una esquina clara
   se prueba **antes** de la búsqueda normal: si no, la búsqueda encuentra una de
   las dos calles sola y el pin cae en su centroide.

**Todo pin rescatado pasa por `rescue_plausible`** (y también las filas que el
gate dejó entrar por evidencia del índice). Medido el 2026-09-15: sin esta guarda
los rescates caían en homónimos de otro partido o comuna (`CONDARCO 525` a 8 km,
`Marie du marché 16` a 12 km). Reglas, en orden:

- un rescate no termina en un centroide de calle ni en una altura que se repite
  en una calle homónima;
- el pin cae dentro del radio de la ciudad que nombra la consulta (GeoNames,
  `sqrt(población)/100` km + 2, solo ciudades a menos de 60 km del depot);
- la calle tiene que reconocerse en el índice y el pin tiene que estar sobre ella;
- si la calle es un solo grupo de nodos en el extract, alcanza;
- si hay homónimos, la localidad que nombra la consulta tiene que elegir uno: la
  ciudad de los vecinos del pin coincide y la de los otros grupos no.

El runner vuelve a pasar por la guarda las filas que entraron por evidencia del
índice. Para un rescate usa **lo que ya validó el geocoder** (los cruces de la
esquina, la calle resuelta), que viaja en el detalle y en la caché. Con la calle
leída del texto crudo se descartaban rescates correctos: `Garibaldi y Montiel`
buscaba una calle llamada así entera y `Los crisantelmos1904` buscaba `los`.

Tres cambios más en la búsqueda normal, los tres acotados:

- **Interpolación dentro de un tramo.** Las alturas de dos calles homónimas
  comparten `street` (`Condarco` en CABA y en Lanús). Juntas, el 501 de una y el
  549 de la otra quedaban como anclas y el pin caía entre los dos pueblos, a
  7,9 km. Si las anclas de siempre están a más de 400 m + 40 m por número de
  diferencia, no son el mismo tramo: se interpola solo dentro de un grupo de
  alturas (celdas de ~900 m) que nombre la consulta, y sin localidad que elija no
  se interpola. Por debajo del tope la cuenta es la de siempre: una avenida con
  huecos en OSM da 7–10 m por número (Via Emilia en Bolonia, anclas a 1,5 km)
  y agrupar por cercanía le cortaba la interpolación.

- **Misma calle y altura en otro nodo a más de 150 m** → ámbar. Una calle no
  repite su numeración: son dos direcciones (homónimo en otro suburbio o
  duplicado de OSM) y el depot desempataba a ciegas.
- **Localidad de los vecinos para una altura exacta sin `addr:city`**, solo para
  decidir si esa altura bloquea la interpolación en la ciudad pedida
  (`Av. Caseros 1800, CABA`). El candidato no se modifica: medido, mutarlo
  rechazaba `Kensington` contra vecinos `Melbourne`.

### 9.4 Geofences

Se miden contra el origin del depot, que **puede no ser el pin real**: si la
ciudad del formulario está lejos del depot, el origin pasa a ser el centroide de
esa ciudad. Sin eso, "el form dice Miami y el depot sigue en CABA" tumbaría todos
los pines por estar a ~7000 km.

- **Duro (500 km)**: cualquier match fuera del radio operativo → sin pin.
- **Blando (15 km)**: un `low_confidence` más lejos que esto → sin pin. La
  premisa es que un match débil lejos del depot es el falso positivo típico: la
  calle homónima de otra ciudad. Un `matched` lejos se respeta hasta los 500 km;
  un `low` lejos, no.

### 9.5 Las bandas de la UI

Tres: **verde** (usable tal cual), **ámbar** (que lo mire un humano), **sin pin**
(ubicar a mano).

**El color sigue el número, no la precisión** (`bands.py:47`), con
`force_review` como excepción: baja un verde a ámbar y es lo que traduce los
techos del árbol al color.

**Un match a nivel calle es ámbar aunque puntúe 0.99**
(`GEOCODE_STREET_LEVEL_REVIEW`, default `true`). Medido por el camino del
producto el 2026-09-15: en CABA 258 de esos verdes caían a más de 500 m de la
puerta. El score confirma la calle, no la entrega.

**El % de un ámbar nunca supera el piso verde** (`runner._stamp`): se recorta a
`GEOCODE_VALID_BAND - 0.01`. Un `Review 99%` al lado de un `Valid 98%` le decía
al operador que el ámbar era más seguro. El score textual real queda en
`geocode_raw_score`.

**El circuito de coherencia**: si la banda dio "sin pin" pero el resultado traía
coordenadas, se **borran** y el status se alinea (`runner.py:400`). Sin eso, un
soft-pin debajo del corte ámbar quedaría en el CSV con lat/lng y cualquier
consumidor que mire coordenadas en vez de banda lo tomaría por bueno. Y el
reporte cuenta por **banda**, no por status, así que lo que dice la API y lo que
colorea la UI son el mismo número por construcción.

### 9.6 Cache

SQLite. **No tiene TTL**: `created_at` se escribe pero nunca se consulta. La
invalidación es por `GEOCODER_VERSION` más la clave de contexto, que incluye la
región y **los seis umbrales**. O sea que **cambiar cualquier umbral por env
invalida la cache automáticamente**, sin borrado manual.

La versión se bumpea cuando cambia la lógica de matching, porque los `not_found`
son los que más envenenan: una dirección que antes no se encontraba se seguiría
reportando como perdida.

WAL y **autocommit** no son decorativos: con un único commit al final, la primera
fila tomaba el lock de escritura y no lo soltaba, así que con
`GEOCODE_WORKERS=2` el segundo job moría con "database is locked".

### 9.7 ¿De qué ciudad habla el archivo?

`detect_locality` (`locality/scan.py:249`) corre durante **normalize** y junta
evidencia de tres fuentes, de más fuerte a más débil: columnas normalizadas, la
cola de las direcciones, y el encabezado del documento.

**Esta capa no decide nada.** Publica evidencia en el reporte para que la UI
pregunte; lo que manda sigue siendo el selector del usuario. Existe porque el
usuario puede tener un depot en CABA y subir un paste de Miami, y geocodificar
con la región equivocada **no devuelve pines malos: no devuelve ninguno**, porque
el índice que se abre es de otro país.

La asimetría deliberada: una ciudad que viene de una **columna** se acepta tal
cual (puede ser un pueblo que GeoNames no lista); una ciudad **adivinada del
texto** solo se propone si GeoNames la conoce. Y el filtro del encabezado es
doble, porque "Hola" es una ciudad de Kenia y un paste que arranca con "Hola"
detectaría Kenia.

El scoring multiplica **acuerdo × volumen**: 1 de 100 no es evidencia, pero 1 de
1 tampoco — un archivo de una fila daría acuerdo perfecto y saldría con la misma
fuerza que 55 filas coincidiendo.

### 9.8 Paralelismo

**Dentro de un job es estrictamente secuencial**: un `for` fila por fila, con una
única conexión SQLite read-only reutilizada para todo el archivo. Para acelerar
un import el camino es el índice, no los hilos.

`SMART_IMPORT_GEOCODE_WORKERS` es paralelismo **entre** jobs: cuántos imports
distintos pueden geocodificar a la vez. El default es 1 porque este container
comparte la VM con el cutter.

---

## 10. Los umbrales

Defaults del código, sin ninguna variable de entorno seteada. **El `.env` del
repo pisa algunos** con valores medidos; ese archivo documenta cada medición y es
la fuente de verdad operativa. `/config` devuelve la configuración efectiva del
proceso.

| Variable | Default | `.env` | Qué decide |
|---|---|---|---|
| `SMART_IMPORT_ADDRESS_ACCEPT_THRESHOLD` | 0.50 | 0.50 | piso para aceptar una dirección y mandarla al geocoder |
| `SMART_IMPORT_DELIVERY_ACCEPT_THRESHOLD` | 0.55 | 0.55 | cuándo una línea del paste es una parada |
| `AUTO_ACCEPT_THRESHOLD` | 0.90 | 0.90 | por encima, la columna se acepta sola |
| `REVIEW_THRESHOLD` | 0.70 | 0.70 | piso de mapeo nivel `delivery` (los otros se derivan) |
| `SMART_IMPORT_LIBPOSTAL_ENABLED` | `false` | `true` | master switch del enhancer |
| `GEOCODE_MATCH_THRESHOLD` | 0.81 | 0.85 | piso de `matched` |
| `GEOCODE_VALID_BAND` | 0.81 | 0.85 | piso de la banda verde |
| `GEOCODE_LOW_CONFIDENCE_THRESHOLD` | 0.70 | 0.70 | piso para devolver coordenada |
| `GEOCODE_REVIEW_BAND` | 0.70 | 0.70 | piso de la banda ámbar |
| `GEOCODE_STREET_MATCH_MIN` | 0.70 | 0.70 | cuánto tiene que matchear la calle |
| `GEOCODE_STREET_LEVEL_FLOOR` | 0.60 | 0.60 | piso para pin a nivel calle |
| `GEOCODE_SOFT_REJECT_MIN` | 0.70 | 0.75 | piso para dar pin de respaldo |
| `GEOCODE_STREET_LEVEL_REVIEW` | `true` | — | un match a nivel calle (sin puerta) sale ámbar aunque puntúe alto |
| `GEOCODE_MAX_DISTANCE_KM` | 500 | 500 | geofence duro |
| `GEOCODE_MAX_LOW_CONFIDENCE_KM` | 15 | 15 | geofence blando |

**Tres alineaciones son obligatorias**, y hay tests que las fijan
(`tests/test_geocode_bands.py`):

- `VALID_BAND == MATCH_THRESHOLD` — si no, hay scores que salen
  `low_confidence` y se pintan **verde**.
- `REVIEW_BAND == LOW_CONFIDENCE_THRESHOLD` — uno es el piso para *devolver*
  coordenada y el otro para *mostrarla*; si el segundo fuera más alto se
  calcularían pines que la banda tira.
- `SOFT_REJECT_MIN >= REVIEW_BAND` — debajo de la banda ámbar el pin de respaldo
  se calcula y se descarta.

**Lo que no es configurable** son los pesos del scorer, los boosts de los
patrones de calle y los topes de palabras. Gobiernan el parseo en sí y moverlos
pide medir, no editar un `.env`.

**Nada de geografía está hardcodeado**: tokens de vía, prefijos de
unidad/barrio/landmark, palabras de narración y expansiones de localidad viven en
`resources/geo_keywords.json` y `resources/locality_expand.json`. El aviso *"no
hardcodear listas acá"* está en `heuristic.py:14`.
