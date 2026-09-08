"""Adaptador entre el parser de paqueteria y el pipeline de campos.

La inteligencia no vive aca: vive en `smart_import.packages`, que lee la frase
con un vocabulario de datos y devuelve bultos con evidencia. Este modulo hace
tres cosas y ninguna mas:

  1. traduce ese resultado a `FieldValue` del schema Vepathos,
  2. saca del canvas TODO lo que el parser leyo —etiquetas y pistas incluidas—
     para que 'entrega 4 paketed de 4 kilos cada uno' no le llegue al extractor
     de direcciones,
  3. mantiene en pie la API que ya existia (`extract_packages`,
     `widen_package_span`, `has_package_signal`).

Convencion de peso: `weight_kg` del schema es SIEMPRE el peso de UN bulto, venga
de una celda de Excel o de una frase de WhatsApp. Una sola convencion, y la
misma que ya tenia el camino tabular.

No es un detalle de estilo. El CSV plano se vuelve a leer —despues de
geocodificar, o porque el usuario lo reimporta— y esa relectura es tabular
siempre. Si el archivo dijera el total, cada round-trip multiplicaria el peso
por la cantidad: "4 paquetes de 4 kilos" salia 16 kg la primera vez y 64 la
segunda. Lo que se emite tiene que significar lo mismo que lo que se lee.
"""
from __future__ import annotations

from ..packages import PackageParse, get_package_lexicon, parse_packages
from .result import FieldValue

#: campos del schema que este paso puede llenar, y de donde salen del parse
_DIMENSIONS = (("length_cm", "length_cm"), ("width_cm", "width_cm"),
               ("height_cm", "height_cm"))


def widen_package_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Incluye etiquetas `qty:` / `peso` / `dimensions` a la izquierda del match."""
    lexicon = get_package_lexicon()
    while start > 0:
        left = start
        while left > 0 and text[left - 1].isspace():
            left -= 1
        match = lexicon.label_left_re.search(text[:left])
        if not match:
            break
        start = match.start()
    return start, end


def _raw(text: str, parse: PackageParse) -> str:
    """Lo que el parser leyo, en orden. Un campo agregado ('2 cajas y 1 sobre'
    -> quantity 3) no tiene UN span: tiene varios, y el span del FieldValue se
    deja en None a proposito en vez de mentir con un rango que tapa el medio."""
    return " ".join(text[start:end].strip()
                    for start, end in sorted(parse.spans) if text[start:end].strip())


def _field(name: str, value, parse: PackageParse, raw: str, method: str,
           extra: tuple[str, ...] = ()) -> FieldValue:
    return FieldValue(name, value, raw, parse.confidence, method, None,
                      (*parse.evidence, *extra))


def extract_packages(canvas, context) -> list[FieldValue]:
    """Bultos, peso, medidas y tipo de embalaje de lo que queda del texto."""
    text = canvas.remaining()
    parse = parse_packages(text, getattr(context, "locales", None))
    if parse.is_empty:
        return []
    raw = _raw(text, parse)

    # El paso consume su propio rastro: un solo FieldValue por campo no alcanza
    # para tapar dos frases de bultos ('2 cajas de 3kg y 1 sobre'), y lo que no
    # se tapa termina dentro de la direccion.
    for span in parse.spans:
        canvas.consume(span)

    out: list[FieldValue] = []
    if parse.dimensions:
        for name, attribute in _DIMENSIONS:
            out.append(_field(name, getattr(parse, attribute), parse, raw, "dimensions"))
    if parse.volume_cm3 is not None:
        out.append(_field("volume_cm3", parse.volume_cm3, parse, raw, "volume"))
    if parse.weight_per_unit_kg is not None:
        note = (f"peso por bulto ({parse.weight_total_kg:g} kg entre "
                f"{parse.quantity} bulto(s))",) if parse.quantity and parse.quantity > 1 \
            else ("peso de la entrega",)
        if parse.weight_declared_per_unit:
            note = (f"el texto declara {parse.weight_per_unit_kg:g} kg por bulto "
                    f"({parse.weight_total_kg:g} kg en total)",)
        out.append(_field("weight_kg", parse.weight_per_unit_kg, parse, raw,
                          "weight", note))
    if parse.quantity is not None:
        out.append(_field("quantity", int(parse.quantity), parse, raw, "quantity"))
    if parse.packaging:
        out.append(_field("packaging", parse.packaging, parse, raw, "packaging",
                          (f"tipo de bulto del catalogo ({parse.packaging_code})",)
                          if parse.packaging_code else ()))
    return out


def has_package_signal(text: str) -> bool:
    """Evidencia logistica para el clasificador de candidatos."""
    return not parse_packages(text or "").is_empty
