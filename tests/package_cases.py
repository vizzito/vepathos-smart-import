"""Catalogo de como escribe la gente sus bultos (150+ frases reales).

No es un test: es el DATASET. `test_package_battery.py` lo corre y falla si algo
se degrada; `python -m pytest tests/test_package_battery.py -s` imprime el mapa
de cobertura por idioma y por patron, que es lo que sirve para decidir que
mejorar despues.

Cada caso declara SOLO lo que le importa. Un caso que no dice `weight_total_kg`
no se chequea el peso: asi se puede agregar una frase para probar la cantidad sin
tener que decidir que hace el parser con todo lo demas.

Apuntado al usuario chico: WhatsApp, mail de despacho, planilla escrita a mano.
Nada de EDI ni de formatos de operador grande.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PackageCase:
    id: str
    text: str
    lang: str = "es"
    quantity: int | None = None
    weight_total_kg: float | None = None
    weight_per_unit_kg: float | None = None
    packaging: str | None = None
    dimensions: tuple[float, float, float] | None = None
    volume_cm3: float | None = None
    #: True = el parser no tiene que encontrar NADA (anti falso positivo)
    empty: bool = False
    #: patron que ejercita, para el reporte de cobertura
    tags: tuple[str, ...] = ()


def C(*args, **kwargs) -> PackageCase:
    return PackageCase(*args, **kwargs)


# --------------------------------------------------------------- castellano

SPANISH = [
    C("es-qty-bultos", "2 bultos", quantity=2, packaging="package", tags=("qty",)),
    C("es-qty-paquetes", "3 paquetes", quantity=3, packaging="parcel", tags=("qty",)),
    C("es-qty-cajas", "4 cajas", quantity=4, packaging="box", tags=("qty",)),
    C("es-qty-cajones", "2 cajones", quantity=2, packaging="box", tags=("qty",)),
    C("es-qty-sobres", "2 sobres", quantity=2, packaging="envelope", tags=("qty",)),
    C("es-qty-bolsas", "3 bolsas", quantity=3, packaging="bag", tags=("qty",)),
    C("es-qty-pallets", "2 pallets", quantity=2, packaging="pallet", tags=("qty",)),
    C("es-qty-tarimas", "2 tarimas", quantity=2, packaging="pallet", tags=("qty", "mx")),
    C("es-qty-piezas", "5 piezas", quantity=5, packaging="unit", tags=("qty",)),
    C("es-qty-unidades", "12 unidades", quantity=12, packaging="unit", tags=("qty",)),
    C("es-qty-botellas", "6 botellas", quantity=6, packaging="bottle", tags=("qty",)),
    C("es-qty-bidones", "2 bidones", quantity=2, packaging="drum",
      tags=("qty", "plural")),
    C("es-qty-rollos", "3 rollos", quantity=3, packaging="roll", tags=("qty",)),
    C("es-qty-cartones", "4 cartones", quantity=4, packaging="carton",
      tags=("qty", "plural")),
    C("es-qty-tambores", "2 tambores", quantity=2, packaging="drum",
      tags=("qty", "plural")),
    C("es-qty-frascos", "6 frascos", quantity=6, packaging="jar",
      tags=("qty", "plural")),
    C("es-qty-barriles", "2 barriles", quantity=2, packaging="barrel",
      tags=("qty", "plural")),
    C("es-qty-contenedores", "1 contenedor", quantity=1, packaging="container",
      tags=("qty",)),

    C("es-word-un", "un paquete", quantity=1, tags=("qty", "numero-en-letras")),
    C("es-word-una", "una caja", quantity=1, tags=("qty", "numero-en-letras")),
    C("es-word-dos", "dos cajas", quantity=2, tags=("qty", "numero-en-letras")),
    C("es-word-tres", "tres bultos", quantity=3, tags=("qty", "numero-en-letras")),
    C("es-word-diez", "diez sobres", quantity=10, tags=("qty", "numero-en-letras")),
    C("es-word-docena", "una docena de cajas", quantity=12,
      tags=("qty", "agrupador")),
    C("es-word-media-docena", "media docena de botellas", quantity=6,
      tags=("qty", "agrupador")),
    C("es-word-par", "un par de bultos", quantity=2, tags=("qty", "agrupador")),

    C("es-abbr-bto", "3 btos", quantity=3, tags=("qty", "abreviatura")),
    C("es-abbr-cj", "2 cjs", quantity=2, packaging="box", tags=("qty", "abreviatura")),
    C("es-abbr-pza", "4 pzas", quantity=4, tags=("qty", "abreviatura")),
    C("es-abbr-uds", "6 uds", quantity=6, tags=("qty", "abreviatura")),
    C("es-abbr-x", "x3 cajas", quantity=3, tags=("qty", "multiplicador")),
    C("es-abbr-2x", "2x sobres", quantity=2, tags=("qty", "multiplicador")),

    C("es-typo-paketes", "4 paketed", quantity=4, packaging="parcel", tags=("typo",)),
    C("es-typo-paqete", "2 paqetes", quantity=2, packaging="parcel", tags=("typo",)),
    C("es-typo-kajas", "3 kajas", quantity=3, packaging="box", tags=("typo",)),
    C("es-typo-bultoss", "2 bultoss", quantity=2, tags=("typo",)),
    C("es-typo-bolsass", "3 bolsass", quantity=3, packaging="bag", tags=("typo",)),

    C("es-peso-kg", "3 kg", weight_total_kg=3.0, tags=("peso",)),
    C("es-peso-kg-decimal", "2.5 kg", weight_total_kg=2.5, tags=("peso",)),
    C("es-peso-coma", "2,75 kg", weight_total_kg=2.75, tags=("peso", "coma-decimal")),
    C("es-peso-pegado", "2.5kg", weight_total_kg=2.5, tags=("peso",)),
    C("es-peso-kilos", "4 kilos", weight_total_kg=4.0, tags=("peso",)),
    C("es-peso-kilogramos", "3 kilogramos", weight_total_kg=3.0, tags=("peso",)),
    C("es-peso-k", "2 k", weight_total_kg=2.0, tags=("peso", "abreviatura")),
    C("es-peso-gramos", "800 g", weight_total_kg=0.8, tags=("peso", "gramos")),
    C("es-peso-grs", "500grs", weight_total_kg=0.5, tags=("peso", "gramos")),
    C("es-peso-gramos-largo", "250 gramos", weight_total_kg=0.25, tags=("peso", "gramos")),
    C("es-peso-libras", "10 libras", weight_total_kg=4.536, tags=("peso", "conversion")),
    C("es-peso-tonelada", "1 tonelada", weight_total_kg=1000.0, tags=("peso", "conversion")),
    C("es-peso-etiqueta", "peso: 3", weight_total_kg=3.0, tags=("peso", "etiqueta")),
    C("es-peso-aprox", "aprox 3 kg", weight_total_kg=3.0, tags=("peso", "ruido")),
    C("es-peso-unos", "unos 5 kg", weight_total_kg=5.0, tags=("peso", "ruido")),
    C("es-peso-tilde", "~3 kg", weight_total_kg=3.0, tags=("peso", "ruido")),
    C("es-peso-medio", "medio kilo", weight_total_kg=0.5, tags=("peso", "fraccion")),
    C("es-peso-kilo-y-medio", "kilo y medio", weight_total_kg=1.5, tags=("peso", "fraccion")),
    C("es-peso-fraccion", "1/2 kg", weight_total_kg=0.5, tags=("peso", "fraccion")),

    C("es-cada-uno", "4 paquetes de 4 kilos cada uno", quantity=4,
      weight_per_unit_kg=4.0, weight_total_kg=16.0, tags=("cada-uno",)),
    C("es-cada-una", "3 cajas de 2 kg cada una", quantity=3,
      weight_per_unit_kg=2.0, weight_total_kg=6.0, tags=("cada-uno",)),
    C("es-cu", "3 bultos 2,5 kg c/u", quantity=3,
      weight_per_unit_kg=2.5, weight_total_kg=7.5, tags=("cada-uno",)),
    C("es-cu-antes", "2,5 kilos c/u, 4 bultos", quantity=4,
      weight_total_kg=10.0, tags=("cada-uno", "orden-invertido")),
    C("es-cada-uno-antes", "3 cajas, cada una de 2 kg", quantity=3,
      weight_total_kg=6.0, tags=("cada-uno", "orden-invertido")),
    C("es-por-bulto", "5 bultos, 3 kg por bulto", quantity=5,
      weight_total_kg=15.0, tags=("cada-uno",)),
    C("es-de-implicito", "2 cajas de 3 kg", quantity=2,
      weight_per_unit_kg=3.0, weight_total_kg=6.0, tags=("cada-uno", "conector-de")),
    C("es-por-x", "2 cajas x 3 kg", quantity=2, weight_total_kg=6.0,
      tags=("cada-uno", "multiplicador")),
    C("es-multiplicador-peso", "3 x 5kg", quantity=3, weight_total_kg=15.0,
      tags=("cada-uno", "multiplicador")),

    C("es-total", "4 bultos, 10 kg en total", quantity=4, weight_total_kg=10.0,
      tags=("total",)),
    C("es-total-peso", "3 cajas, peso total 12 kg", quantity=3,
      weight_total_kg=12.0, tags=("total",)),
    C("es-sin-pista", "2 cajas 3 kg", quantity=2, weight_total_kg=3.0,
      tags=("total", "default")),

    C("es-dims", "40x30x20", dimensions=(40.0, 30.0, 20.0), tags=("medidas",)),
    C("es-dims-cm", "40 x 30 x 20 cm", dimensions=(40.0, 30.0, 20.0), tags=("medidas",)),
    C("es-dims-etiqueta", "medidas 50x40x30 cm", dimensions=(50.0, 40.0, 30.0),
      tags=("medidas", "etiqueta")),
    C("es-dims-metros", "1 x 1 x 2 m", dimensions=(100.0, 100.0, 200.0),
      tags=("medidas", "conversion")),
    C("es-dims-mm", "400 x 300 x 200 mm", dimensions=(40.0, 30.0, 20.0),
      tags=("medidas", "conversion")),
    C("es-dims-por", "40 por 30 por 20 cm", dimensions=(40.0, 30.0, 20.0),
      tags=("medidas", "lenguaje")),
    C("es-volumen-m3", "0.8 m3", volume_cm3=800000.0, tags=("volumen",)),
    C("es-volumen-litros", "20 litros", volume_cm3=20000.0, tags=("volumen",)),

    C("es-frase-1", "Ana Perez, Av Corrientes 100, 2 bultos de 5 kg cada uno",
      quantity=2, weight_total_kg=10.0, tags=("frase-real",)),
    C("es-frase-2", "entregar 1 caja grande 6kg en Cabildo 174",
      quantity=1, weight_total_kg=6.0, packaging="box", tags=("frase-real",)),
    C("es-frase-3", "son 3 paquetes chicos, 800 gramos cada uno",
      quantity=3, weight_total_kg=2.4, tags=("frase-real", "cada-uno")),
    C("es-frase-4", "mandar 2 cajas (40x30x20) de 4 kg c/u",
      quantity=2, weight_total_kg=8.0, dimensions=(40.0, 30.0, 20.0),
      tags=("frase-real", "combinado")),
    C("es-frase-5", "1 sobre con documentacion, 200 g",
      quantity=1, weight_total_kg=0.2, packaging="envelope", tags=("frase-real",)),
    C("es-frase-6", "retiro de 6 bultos, peso aproximado 30 kilos en total",
      quantity=6, weight_total_kg=30.0, tags=("frase-real", "total")),
    C("es-frase-7", "2 pallets de 300 kg cada uno, medidas 120x100x150",
      quantity=2, weight_total_kg=600.0, packaging="pallet",
      dimensions=(120.0, 100.0, 150.0), tags=("frase-real", "combinado")),
]

# ------------------------------------------------------------------- ingles

ENGLISH = [
    C("en-qty-boxes", "3 boxes", "en", quantity=3, packaging="box", tags=("qty",)),
    C("en-qty-parcels", "2 parcels", "en", quantity=2, packaging="parcel", tags=("qty",)),
    C("en-qty-packages", "4 packages", "en", quantity=4, tags=("qty",)),
    C("en-qty-envelopes", "2 envelopes", "en", quantity=2, packaging="envelope",
      tags=("qty",)),
    C("en-qty-bags", "5 bags", "en", quantity=5, packaging="bag", tags=("qty",)),
    C("en-qty-pallets", "2 pallets", "en", quantity=2, packaging="pallet", tags=("qty",)),
    C("en-qty-crates", "3 crates", "en", quantity=3, packaging="crate", tags=("qty",)),
    C("en-qty-pieces", "10 pieces", "en", quantity=10, packaging="unit", tags=("qty",)),

    C("en-word-two", "two boxes", "en", quantity=2, tags=("qty", "numero-en-letras")),
    C("en-word-three", "three parcels", "en", quantity=3, tags=("qty", "numero-en-letras")),
    C("en-word-dozen", "a dozen boxes", "en", quantity=12, tags=("qty", "agrupador")),
    C("en-word-couple", "a couple of boxes", "en", quantity=2, tags=("qty", "agrupador")),

    C("en-abbr-pkgs", "3 pkgs", "en", quantity=3, tags=("qty", "abreviatura")),
    C("en-abbr-pcs", "6 pcs", "en", quantity=6, tags=("qty", "abreviatura")),
    C("en-abbr-ctns", "4 ctns", "en", quantity=4, packaging="carton",
      tags=("qty", "abreviatura")),

    C("en-peso-kg", "10 kg", "en", weight_total_kg=10.0, tags=("peso",)),
    C("en-peso-lb", "10 lb", "en", weight_total_kg=4.536, tags=("peso", "conversion")),
    C("en-peso-lbs", "22 lbs", "en", weight_total_kg=9.979, tags=("peso", "conversion")),
    C("en-peso-pounds", "3 pounds", "en", weight_total_kg=1.361,
      tags=("peso", "conversion")),
    C("en-peso-oz", "16 oz", "en", weight_total_kg=0.454, tags=("peso", "conversion")),
    C("en-peso-grams", "500 grams", "en", weight_total_kg=0.5, tags=("peso", "gramos")),
    C("en-peso-half", "half a kilo", "en", weight_total_kg=0.5,
      tags=("peso", "fraccion")),

    C("en-each", "three boxes of 10 pounds each", "en", quantity=3,
      weight_per_unit_kg=4.536, weight_total_kg=13.608, tags=("cada-uno",)),
    C("en-each-short", "2 boxes 5kg each", "en", quantity=2, weight_total_kg=10.0,
      tags=("cada-uno",)),
    C("en-per-box", "4 parcels, 3 kg per box", "en", quantity=4,
      weight_total_kg=12.0, tags=("cada-uno",)),
    C("en-apiece", "2 crates 20 lbs apiece", "en", quantity=2,
      weight_total_kg=18.144, tags=("cada-uno",)),
    C("en-total", "3 boxes, 15 kg total", "en", quantity=3, weight_total_kg=15.0,
      tags=("total",)),
    C("en-combined", "2 bags, 8 kg combined", "en", quantity=2, weight_total_kg=8.0,
      tags=("total",)),

    C("en-dims", "20x30x40 cm", "en", dimensions=(20.0, 30.0, 40.0), tags=("medidas",)),
    C("en-dims-inches", "10 x 8 x 6 in", "en", dimensions=(25.4, 20.32, 15.24),
      tags=("medidas", "conversion")),
    C("en-dims-label", "dimensions 50x40x30cm", "en", dimensions=(50.0, 40.0, 30.0),
      tags=("medidas", "etiqueta")),
    C("en-volume-cbm", "1.5 cbm", "en", volume_cm3=1500000.0, tags=("volumen",)),

    C("en-frase-1", "Please deliver 2 boxes (10 lbs each) to 350 5th Ave",
      "en", quantity=2, weight_total_kg=9.072, tags=("frase-real", "cada-uno")),
    C("en-frase-2", "1 envelope with documents, 200g", "en", quantity=1,
      weight_total_kg=0.2, packaging="envelope", tags=("frase-real",)),
    C("en-frase-3", "pickup: 4 cartons, 25 kg total, 60x40x40 cm", "en",
      quantity=4, weight_total_kg=25.0, packaging="carton",
      dimensions=(60.0, 40.0, 40.0), tags=("frase-real", "combinado")),
]

# ---------------------------------------------------------------- portugues

PORTUGUESE = [
    C("pt-qty-caixas", "3 caixas", "pt", quantity=3, packaging="box", tags=("qty",)),
    C("pt-qty-volumes", "2 volumes", "pt", quantity=2, tags=("qty",)),
    C("pt-qty-pacotes", "4 pacotes", "pt", quantity=4, packaging="parcel", tags=("qty",)),
    C("pt-qty-sacos", "2 sacos", "pt", quantity=2, packaging="bag", tags=("qty",)),
    C("pt-word-duas", "duas caixas", "pt", quantity=2, tags=("qty", "numero-en-letras")),
    C("pt-word-duzia", "uma duzia de caixas", "pt", quantity=12, tags=("qty", "agrupador")),
    C("pt-peso-quilos", "5 quilos", "pt", weight_total_kg=5.0, tags=("peso",)),
    C("pt-peso-gramas", "800 gramas", "pt", weight_total_kg=0.8, tags=("peso", "gramos")),
    C("pt-cada-um", "3 caixas de 2 kg cada uma", "pt", quantity=3,
      weight_total_kg=6.0, tags=("cada-uno",)),
    C("pt-total", "4 volumes, 10 kg no total", "pt", quantity=4,
      weight_total_kg=10.0, tags=("total",)),
    C("pt-frase-1", "entregar 2 caixas de 3 quilos cada uma na Rua Augusta 100",
      "pt", quantity=2, weight_total_kg=6.0, tags=("frase-real", "cada-uno")),
]

# ------------------------------------------------------- frances / italiano / aleman

OTHERS = [
    C("fr-qty-colis", "2 colis", "fr", quantity=2, packaging="parcel", tags=("qty",)),
    C("fr-qty-cartons", "3 cartons", "fr", quantity=3, packaging="carton", tags=("qty",)),
    C("fr-word-deux", "deux colis", "fr", quantity=2, tags=("qty", "numero-en-letras")),
    C("fr-chacun", "2 colis de 3 kg chacun", "fr", quantity=2, weight_total_kg=6.0,
      tags=("cada-uno",)),
    C("fr-palette", "une palette", "fr", quantity=1, packaging="pallet", tags=("qty",)),

    C("it-qty-scatole", "3 scatole", "it", quantity=3, packaging="box", tags=("qty",)),
    C("it-word-due", "due pacchi", "it", quantity=2, tags=("qty", "numero-en-letras")),
    C("it-ciascuno", "2 scatole da 3 kg ciascuno", "it", quantity=2,
      weight_total_kg=6.0, tags=("cada-uno",)),

    C("de-qty-pakete", "3 Pakete", "de", quantity=3, packaging="parcel", tags=("qty",)),
    C("de-word-zwei", "zwei Kartons", "de", quantity=2, tags=("qty", "numero-en-letras")),
    C("de-je", "3 Pakete je 5 kg", "de", quantity=3, weight_total_kg=15.0,
      tags=("cada-uno",)),

    C("nl-qty-pakketten", "3 pakketten", "nl", quantity=3, packaging="parcel",
      tags=("qty", "plural")),
    # Colision real entre idiomas: 'dozen' es 12 en ingles y el plural de 'doos'
    # (caja) en neerlandes. Gana el ingles, que es el idioma que mas aparece en
    # una lista de entregas; el neerlandes solo pierde este caso.
    C("nl-colision-dozen", "3 dozen", "nl", empty=True, tags=("colision-idiomas",)),
]

# ------------------------------------------- lo que NO tiene que encontrar nada

NEGATIVES = [
    C("neg-address-1", "Av. Corrientes 100, Palermo, CABA", empty=True, tags=("anti-fp",)),
    C("neg-address-2", "Av. Rivadavia 211 Caballito CABA", empty=True, tags=("anti-fp",)),
    C("neg-address-3", "Av. del Libertador 887, Buenos Aires", empty=True, tags=("anti-fp",)),
    C("neg-address-4", "San Martín 3534, Buenos Aires", empty=True, tags=("anti-fp",)),
    C("neg-address-5", "11 de Septiembre Nro 1913", empty=True, tags=("anti-fp",)),
    C("neg-address-6", "23 MG Road, Bengaluru 560001", "en", empty=True, tags=("anti-fp",)),
    C("neg-address-7", "350 5th Ave, New York, NY 10118", "en", empty=True,
      tags=("anti-fp",)),
    C("neg-address-8", "Rua Augusta 1500, Sao Paulo", "pt", empty=True, tags=("anti-fp",)),
    C("neg-address-9", "Las Cajas 123, Belgrano", empty=True, tags=("anti-fp", "trampa")),
    C("neg-address-10", "Los Pallets 450, Pilar", empty=True, tags=("anti-fp", "trampa")),
    C("neg-piso", "piso 3 depto 2", empty=True, tags=("anti-fp",)),
    C("neg-unidad", "unidad funcional 3, Callao 1123", empty=True, tags=("anti-fp",)),
    C("neg-unit", "Unit 5, 20 Main Street", "en", empty=True, tags=("anti-fp",)),
    C("neg-timbre", "Timbre 3B, dejar en porteria", empty=True, tags=("anti-fp",)),
    C("neg-ruta", "Ruta 8 km 45, Pilar", empty=True, tags=("anti-fp",)),
    C("neg-telefono", "11 4000-1000", empty=True, tags=("anti-fp",)),
    C("neg-telefono-2", "+54 9 11 6048-5121", empty=True, tags=("anti-fp",)),
    C("neg-horario", "entregar de 9 a 18 hs", empty=True, tags=("anti-fp",)),
    C("neg-fecha", "el 3 de marzo de 2026", empty=True, tags=("anti-fp",)),
    C("neg-precio", "cobrar $ 4500 contra entrega", empty=True, tags=("anti-fp",)),
    C("neg-preposicion", "dejar 2 sobre la mesa", empty=True, tags=("anti-fp", "trampa")),
    C("neg-nombre", "Ana Perez y Juan Lopez", empty=True, tags=("anti-fp",)),
    C("neg-vacio", "", empty=True, tags=("anti-fp",)),
    C("neg-saludo", "Hola! te paso las entregas de hoy", empty=True, tags=("anti-fp",)),
]


CATALOG: tuple[PackageCase, ...] = tuple(
    [*SPANISH, *ENGLISH, *PORTUGUESE, *OTHERS, *NEGATIVES])


def by_tag(tag: str) -> list[PackageCase]:
    return [c for c in CATALOG if tag in c.tags]
