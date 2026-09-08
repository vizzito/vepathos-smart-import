"""De una frase de despacho a bultos, sin modelo.

    "entrega 4 paketed de 4 kilos cada uno"
        -> 4 bultos tipo parcel, 4 kg cada uno, 16 kg en total
    "three boxes of 10 pounds each"
        -> 3 bultos tipo box, 4.536 kg cada uno, 13.608 kg en total

El orden de lectura no es casual. Primero se leen las MEDIDAS y el PESO, que
traen unidad y por lo tanto no se pueden confundir con otra cosa; recien despues
se buscan cantidades sobre el texto que quedo. Sin ese orden, "3 kilos" se lee
como "3 <sustantivo>" y la entrega termina con tres bultos que nadie mando.

Dos numeros y una unidad no alcanzan para saber si el peso es de cada bulto o de
la entrega entera. Eso lo decide el idioma:

    "2 cajas de 3 kg"        de + cantidad>1  -> 3 kg CADA UNA   (6 kg total)
    "2 cajas 3 kg"           sin pista        -> 3 kg EN TOTAL   (1.5 c/u)
    "2 cajas 3 kg cada una"  pista explicita  -> 3 kg CADA UNA   (6 kg total)

El resultado expone el peso de las dos formas (`weight_total_kg` y
`weight_per_unit_kg`) porque los dos consumidores son distintos: en texto libre
el schema guarda el total y `assemble` lo reparte; en tabular el peso de la
columna es unitario y se clona.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from ..resources import fold
from .lexicon import CONF_BARE, CONF_MULTIPLIER, PackageLexicon, get_package_lexicon

#: hasta donde se mira, despues de un peso, buscando "cada uno" / "en total"
CUE_WINDOW = 28
#: abreviaturas de 1-2 letras ('u', 'pz') solo si estan pegadas al numero o
#: llevan punto: "2u" y "2 u." son bultos, "2 u" suelto es cualquier cosa
SHORT_ABBREV_LEN = 2
#: solo lo que quede entre la cantidad y el peso puede ser un conector
_GAP_NOISE = re.compile(r"[^\w]+", re.UNICODE)
#: un sustantivo suelto seguido de un numero es una calle ('Las Cajas 123')
_STREET_NUMBER = re.compile(r"\s*\d{1,5}\b")


def _number(text: str) -> float | None:
    """'3' · '2,5' · '1/2' -> 3.0 · 2.5 · 0.5."""
    if text is None:
        return None
    raw = str(text).strip().replace(",", ".")
    if "/" in raw:
        top, _, bottom = raw.partition("/")
        try:
            divisor = float(bottom)
            return float(top) / divisor if divisor else None
        except (TypeError, ValueError):
            return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class PackageItem:
    """Un grupo de bultos del mismo tipo: '2 cajas de 3 kg'."""
    count: int
    packaging: str | None = None
    code: str = ""
    weight_kg: float | None = None          # tal como se escribio
    per_unit: bool = False                  # ese peso es de CADA bulto
    confidence: float = 0.0
    method: str = ""
    span: tuple[int, int] = (0, 0)
    evidence: tuple[str, ...] = ()

    @property
    def total_weight_kg(self) -> float | None:
        if self.weight_kg is None:
            return None
        return round(self.weight_kg * (self.count if self.per_unit else 1), 3)

    def as_dict(self) -> dict:
        return {"count": self.count, "packaging": self.packaging, "code": self.code,
                "weight_kg": self.weight_kg, "per_unit": self.per_unit,
                "total_weight_kg": self.total_weight_kg,
                "confidence": round(self.confidence, 3), "method": self.method,
                "evidence": list(self.evidence)}


@dataclass(frozen=True)
class PackageParse:
    """Lo que la frase dijo sobre los bultos, con su trazabilidad."""
    items: tuple[PackageItem, ...] = ()
    quantity: int | None = None
    weight_total_kg: float | None = None
    weight_per_unit_kg: float | None = None
    weight_declared_per_unit: bool = False
    packaging: str | None = None
    packaging_code: str = ""
    length_cm: float | None = None
    width_cm: float | None = None
    height_cm: float | None = None
    volume_cm3: float | None = None
    confidence: float = 0.0
    #: todo lo que el parser leyo, para que el extractor lo saque del canvas
    spans: tuple[tuple[int, int], ...] = ()
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not (self.items or self.weight_total_kg is not None
                    or self.length_cm is not None or self.volume_cm3 is not None)

    @property
    def dimensions(self) -> tuple[float, float, float] | None:
        if None in (self.length_cm, self.width_cm, self.height_cm):
            return None
        return (self.length_cm, self.width_cm, self.height_cm)

    def as_dict(self) -> dict:
        return {
            "quantity": self.quantity,
            "weight_total_kg": self.weight_total_kg,
            "weight_per_unit_kg": self.weight_per_unit_kg,
            "weight_declared_per_unit": self.weight_declared_per_unit,
            "packaging": self.packaging, "packaging_code": self.packaging_code,
            "length_cm": self.length_cm, "width_cm": self.width_cm,
            "height_cm": self.height_cm, "volume_cm3": self.volume_cm3,
            "confidence": round(self.confidence, 3),
            "items": [i.as_dict() for i in self.items],
            "evidence": list(self.evidence),
        }


EMPTY = PackageParse()


@dataclass
class _Weight:
    kg: float
    span: tuple[int, int]          # incluye la pista ("cada uno")
    core: tuple[int, int]          # solo el numero y la unidad
    per_unit: bool = False
    explicit: bool = False         # la pista estaba escrita, no inferida
    unit: str = ""
    #: cantidad que venia pegada al peso: '3 x 5kg'
    count: int | None = None


@dataclass
class _Quantity:
    count: int
    canonical: str | None
    code: str
    confidence: float
    method: str
    span: tuple[int, int]
    matched: str


class PackageParser:
    """Reglas puras sobre un lexico de datos. Sin estado entre llamadas."""

    def __init__(self, lexicon: PackageLexicon | None = None,
                 locales: tuple[str, ...] | None = None):
        self.lexicon = lexicon or get_package_lexicon(locales)

    # ---------- API ----------

    def parse(self, text: str) -> PackageParse:
        original = text or ""
        if not original.strip():
            return EMPTY
        mask = bytearray(len(original))
        evidence: list[str] = []
        spans: list[tuple[int, int]] = []

        dims, volume = self._measures(original, mask, spans, evidence)
        weights = self._weights(original, mask, spans, evidence)
        quantities = self._quantities(original, mask, spans, evidence)
        if not quantities:
            quantities = self._from_multiplier(weights, evidence)
        if not quantities and (weights or dims):
            quantities = self._bare_noun(original, mask, spans, evidence)

        items, bound = self._bind(original, quantities, weights, evidence)
        return self._aggregate(items, weights, bound, dims, volume,
                               tuple(spans), evidence)

    def has_signal(self, text: str) -> bool:
        """Evidencia logistica: cantidad con sustantivo, peso o medidas."""
        parse = self.parse(text)
        return not parse.is_empty

    # ---------- etapas ----------

    def _masked(self, text: str, mask: bytearray) -> str:
        """Mismo largo que el original: los offsets siguen valiendo."""
        return "".join(" " if used else ch for ch, used in zip(text, mask))

    @staticmethod
    def _take(mask: bytearray, spans: list, span: tuple[int, int]) -> None:
        for i in range(max(0, span[0]), min(len(mask), span[1])):
            mask[i] = 1
        spans.append(span)

    def _measures(self, text, mask, spans, evidence):
        """Medidas (LxAxH) y volumen: la forma es inequivoca, van primero."""
        lex = self.lexicon
        dims = None
        if match := lex.dimension_re.search(self._masked(text, mask)):
            factor = lex.dimension_units.get(fold(match.group("unit") or "") or "", 1.0)
            values = [_number(match.group(k)) for k in ("l", "w", "h")]
            if all(v is not None for v in values):
                dims = tuple(round(v * factor, 2) for v in values)
                self._take(mask, spans, self._widen(text, match.span()))
                evidence.append(f"medidas '{match.group(0).strip()}'")

        volume = None
        if match := lex.volume_re.search(self._masked(text, mask)):
            value = _number(match.group("n"))
            factor = lex.volume_units.get(fold(match.group("unit")))
            if value is not None and factor:
                volume = round(value * factor, 2)
                self._take(mask, spans, self._widen(text, match.span()))
                evidence.append(f"volumen '{match.group(0).strip()}'")
        return dims, volume

    def _weights(self, text, mask, spans, evidence) -> list[_Weight]:
        """Todo numero con unidad de peso, con su pista de cada-uno/total."""
        lex = self.lexicon
        work = self._masked(text, mask)
        found: list[_Weight] = []

        for match in lex.weight_re.finditer(work):
            value = self._weight_number(match)
            factor = lex.weight_units.get(fold(match.group("unit")).rstrip("."))
            # Un peso que redondea a cero no es un peso: es un numero que cayo
            # al lado de algo que parecia unidad.
            if value is None or not factor or round(value * factor, 3) <= 0:
                continue
            core = self._widen(text, match.span())
            end, per_unit, explicit = self._cue(work, match.end())
            start, per_unit, explicit = self._cue_before(
                work, core[0], per_unit, explicit)
            count, start, end = self._multiplier(work, start, end)
            found.append(_Weight(kg=round(value * factor, 4), span=(start, end),
                                 core=core, per_unit=per_unit or count is not None,
                                 explicit=explicit, unit=match.group("unit"),
                                 count=count))
            evidence.append(f"peso con unidad '{match.group(0).strip()}'")

        if not found:
            # "peso 3" / "weight: 10": la etiqueta dice que es peso, y el schema
            # canonico es kg, asi que se asume kg (con menos confianza).
            if match := lex.labelled_weight_re.search(work):
                value = _number(match.group("n"))
                if value is not None:
                    end, per_unit, explicit = self._cue(work, match.end())
                    found.append(_Weight(kg=value, span=(match.start(), end),
                                         core=match.span(), per_unit=per_unit,
                                         explicit=explicit, unit="kg"))
                    evidence.append(
                        f"etiqueta '{match.group('label')}' sin unidad: se asume kg")

        for weight in found:
            self._take(mask, spans, weight.span)
        return found

    def _weight_number(self, match) -> float | None:
        """'3' · '1/2' · 'un' · 'medio' · (unidad) 'y medio'."""
        lex = self.lexicon
        value = _number(match.group("n"))
        if value is None and (word := match.group("nword")):
            key = fold(word)
            value = lex.number_words.get(key)
            if value is None:
                value = lex.fraction_words.get(key)
        half = match.groupdict().get("half")
        if half:
            # 'kilo y medio' sin numero adelante es 1 + 1/2
            value = (value if value is not None else 1) + \
                lex.fraction_words.get(fold(half), 0.0)
        return float(value) if value is not None else None

    def _cue_before(self, text: str, start: int, per_unit: bool,
                    explicit: bool) -> tuple[int, bool, bool]:
        """'cada una de 2 kg' / '3 Pakete je 5 kg': la pista va adelante.

        Solo cuenta si no hay nada mas que conectores entre la pista y el
        numero; asi 'cada uno' de una frase anterior no contamina este peso.
        """
        if explicit:
            return start, per_unit, explicit
        window = text[max(0, start - CUE_WINDOW):start]
        match = self.lexicon.cue_before_re.search(window)
        if not match:
            return start, per_unit, explicit
        return max(0, start - CUE_WINDOW) + match.start("cue"), True, True

    def _multiplier(self, text: str, start: int, end: int):
        """'3 x 5kg' y '5kg x 3': el numero pegado al peso es la cantidad."""
        lex = self.lexicon
        if match := lex.count_before_re.search(text[:start]):
            return int(match.group("n")), match.start(), end
        if match := lex.count_after_re.match(text[end:]):
            return int(match.group("n")), start, end + match.end()
        return None, start, end

    def _cue(self, text: str, after: int) -> tuple[int, bool, bool]:
        """(fin del span, es_por_unidad, estaba_escrito) mirando lo que sigue."""
        lex = self.lexicon
        window = text[after:after + CUE_WINDOW]
        if match := lex.per_unit_re.search(window):
            if not window[:match.start()].strip(" ,.;:-()/"):
                return after + match.end(), True, True
        if match := lex.total_re.search(window):
            if not window[:match.start()].strip(" ,.;:-()/"):
                return after + match.end(), False, True
        return after, False, False

    def _quantities(self, text, mask, spans, evidence) -> list[_Quantity]:
        """Numero (digito o palabra) + sustantivo de bulto del catalogo."""
        lex = self.lexicon
        work = self._masked(text, mask)
        found: list[_Quantity] = []

        for match in lex.quantity_re.finditer(work):
            count = self._count(match)
            if count is None or not 1 <= count <= 999:
                continue
            raw_noun = match.group("noun")
            key = fold(raw_noun).strip(".")
            # un numero nunca es un bulto: sin esto el fuzzy lee 'docena' como
            # el neerlandes 'dozen' (cajas) y la docena se pierde.
            if key in lex.number_words or key in lex.group_words:
                continue
            hit = lex.noun(raw_noun)
            if hit is None:
                continue
            if lex.guarded(raw_noun, work[match.end():match.end() + 24]):
                continue
            if len(fold(raw_noun).strip(".")) <= SHORT_ABBREV_LEN \
                    and not self._short_abbrev_ok(match, raw_noun):
                continue
            span = self._widen(text, match.span())
            found.append(_Quantity(count=count, canonical=hit.canonical,
                                   code=hit.code, confidence=hit.confidence,
                                   method=hit.method, span=span,
                                   matched=raw_noun))
            note = f"cantidad con sustantivo '{match.group(0).strip()}'"
            if hit.method == "typo":
                note += f" (se leyo '{raw_noun}' como '{hit.canonical}')"
            elif hit.method == "abbreviation":
                note += f" (abreviatura de '{hit.canonical}')"
            evidence.append(note)

        for quantity in found:
            self._take(mask, spans, quantity.span)
        return found

    @staticmethod
    def _short_abbrev_ok(match, raw_noun: str) -> bool:
        """'2u' y '2 u.' si; '2 u' suelto no: una letra sola no es evidencia."""
        if raw_noun.endswith("."):
            return True
        before = match.group(0)[:-len(raw_noun)]
        return not re.search(r"\s", before)

    def _count(self, match) -> int | None:
        """'2' | 'dos' | 'una docena' -> 2 | 2 | 12."""
        if digits := match.group("digits"):
            base = int(digits)
        elif word := match.group("word"):
            base = self.lexicon.number_words.get(fold(word))
        else:
            base = None
        if base is None:
            return None
        group = match.group("group")
        return base * self.lexicon.group_words.get(fold(group), 1) if group else base

    def _from_multiplier(self, weights, evidence) -> list[_Quantity]:
        """'3 x 5kg' son 3 bultos aunque nadie haya escrito el sustantivo."""
        out: list[_Quantity] = []
        for weight in weights:
            if weight.count is None:
                continue
            evidence.append(f"'{weight.count} x {weight.kg:g} kg': "
                            f"{weight.count} bultos de {weight.kg:g} kg")
            out.append(_Quantity(count=weight.count, canonical=None, code="",
                                 confidence=CONF_MULTIPLIER, method="multiplier",
                                 span=weight.span, matched="x"))
        return out

    def _bare_noun(self, text, mask, spans, evidence) -> list[_Quantity]:
        """'paquete fragil 1.2kg' es UN bulto: el sustantivo va solo.

        Solo se acepta cuando el texto ya trajo peso o medidas —si no, cualquier
        calle que se llame 'Las Cajas' se convierte en un bulto— y nunca si al
        sustantivo le sigue un numero, que es la forma de una direccion.
        """
        lex = self.lexicon
        work = self._masked(text, mask)
        for match in lex.bare_noun_re.finditer(work):
            raw = match.group("noun")
            hit = lex.noun(raw)
            if hit is None or hit.method == "typo":
                continue
            if len(fold(raw).strip(".")) <= SHORT_ABBREV_LEN:
                continue
            if lex.guarded(raw, work[match.end():match.end() + 24]):
                continue
            if _STREET_NUMBER.match(work[match.end():]):
                continue
            span = self._widen(text, match.span())
            self._take(mask, spans, span)
            evidence.append(f"sustantivo de bulto sin cantidad '{raw}': 1 bulto")
            return [_Quantity(count=1, canonical=hit.canonical, code=hit.code,
                              confidence=CONF_BARE, method="bare", span=span,
                              matched=raw)]
        return []

    # ---------- juntar cantidad con peso ----------

    def _bind(self, text, quantities, weights, evidence):
        """Cada peso se pega a la cantidad que tiene mas cerca a la izquierda."""
        items: list[PackageItem] = []
        bound: set[int] = set()
        for index, quantity in enumerate(quantities):
            weight = self._weight_for(text, quantities, weights, index, evidence)
            if weight is not None:
                bound.add(id(weight))
            items.append(PackageItem(
                count=quantity.count, packaging=quantity.canonical,
                code=quantity.code,
                weight_kg=weight.kg if weight else None,
                per_unit=bool(weight and weight.per_unit),
                confidence=quantity.confidence, method=quantity.method,
                span=quantity.span,
                evidence=(f"'{quantity.matched}' -> {quantity.canonical}",)))
        return items, bound

    def _weight_for(self, text, quantities, weights, index, evidence):
        quantity = quantities[index]
        following = quantities[index + 1].span[0] if index + 1 < len(quantities) \
            else len(text)
        for weight in weights:
            if not quantity.span[1] <= weight.core[0] < following:
                continue
            if not weight.explicit and quantity.count > 1:
                gap = _GAP_NOISE.sub(" ", text[quantity.span[1]:weight.core[0]])
                tokens = [fold(t) for t in gap.split() if t]
                if tokens and all(t in self.lexicon.per_unit_connectors
                                  for t in tokens):
                    weight.per_unit = True
                    evidence.append(
                        f"'{quantity.count} {quantity.matched} "
                        f"{' '.join(tokens)} {weight.kg:g}': el peso es de cada bulto")
            return weight
        return None

    # ---------- resultado ----------

    def _aggregate(self, items, weights, bound, dims, volume, spans,
                   evidence) -> PackageParse:
        lex_conf = [i.confidence for i in items] or [0.0]

        quantity = sum(i.count for i in items) or None

        total = None
        parts = [i.total_weight_kg for i in items if i.total_weight_kg is not None]
        # un peso que no quedo pegado a ninguna cantidad pero dice "c/u" igual
        # habla de cada bulto: '2,5 kilos c/u, 4 bultos' son 10 kg.
        loose = [w.kg * quantity if (w.per_unit and quantity) else w.kg
                 for w in weights if id(w) not in bound]
        if parts or loose:
            total = round(sum(parts) + sum(loose), 3)

        per_unit = None
        if total is not None:
            per_unit = round(total / quantity, 3) if quantity else total

        packaging = None
        code = ""
        types = {i.packaging for i in items if i.packaging}
        if len(types) == 1:
            packaging = types.pop()
            code = next((i.code for i in items if i.packaging == packaging), "")
        elif len(types) > 1:
            evidence.append("hay bultos de tipos distintos: no se declara packaging")

        confidence = min(lex_conf) if items else (0.9 if (weights or dims) else 0.0)
        return PackageParse(
            items=tuple(items), quantity=quantity, weight_total_kg=total,
            weight_per_unit_kg=per_unit,
            weight_declared_per_unit=any(i.per_unit for i in items),
            packaging=packaging, packaging_code=code,
            length_cm=dims[0] if dims else None,
            width_cm=dims[1] if dims else None,
            height_cm=dims[2] if dims else None,
            volume_cm3=volume, confidence=confidence, spans=spans,
            evidence=tuple(dict.fromkeys(evidence)),
        )

    # ---------- spans ----------

    def _widen(self, text: str, span: tuple[int, int]) -> tuple[int, int]:
        """Se come la etiqueta de la izquierda ('qty:', 'peso', 'medidas')."""
        start, end = span
        while start > 0:
            left = start
            while left > 0 and text[left - 1].isspace():
                left -= 1
            match = self.lexicon.label_left_re.search(text[:left])
            if not match:
                break
            start = match.start()
        return start, end


@lru_cache(maxsize=4)
def _default(locales: tuple[str, ...] | None = None) -> PackageParser:
    return PackageParser(locales=locales)


def parse_packages(text: str, locales: tuple[str, ...] | None = None) -> PackageParse:
    """Atajo para quien no quiere construir el parser."""
    return _default(locales).parse(text)
