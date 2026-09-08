"""El vocabulario de paqueteria compilado una vez por proceso.

Dos fuentes, ninguna en el codigo:

  * los SUSTANTIVOS de bulto salen del catalogo de vocabulario (dominio
    `packaging`): 269 alias en 9 idiomas que ya usa el mapper de columnas para
    entender un header. Aca sirven para entender una frase.
  * el resto —numeros escritos con letras, abreviaturas de despacho, unidades
    con su factor al canonico, pistas de "cada uno"/"en total"— vive en
    `resources/package_lexicon.json`.

Agregar un idioma o un alias es editar datos, nunca este modulo.

El matcheo de sustantivos es por ESQUELETO, no por letra: 'paquetes' y el typo
'paketed' colapsan al mismo `paketes`/`paketed` y quedan a distancia 1. Sin esto
hay que enumerar los errores de tipeo, que es una lista infinita.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from ..resources import fold, package_lexicon, packaging_alias_map

#: largo minimo para arriesgar una correccion de typo. Con 4 letras o menos,
#: distancia 1 convierte una palabra en otra distinta ('caja' -> 'cana').
MIN_FUZZY_LEN = 5
#: confianzas por como se reconocio el sustantivo
CONF_EXACT = 0.92
CONF_ABBREV = 0.88
CONF_FUZZY = 0.80
#: un sustantivo sin numero delante ('paquete fragil 1.2kg') vale 1 bulto, pero
#: solo cuando el texto trae otra evidencia de paqueteria
CONF_BARE = 0.72
#: '3 x 5kg': la cantidad sale del multiplicador, sin sustantivo que la respalde
CONF_MULTIPLIER = 0.85

#: pares que colapsan al mismo sonido en castellano/portugues. El orden importa:
#: 'qu' antes que 'q', 'ck' antes que 'c'.
_SKELETON_RULES = (
    ("qu", "k"), ("ck", "k"), ("ch", "x"), ("ph", "f"), ("sh", "x"),
    ("gu", "g"), ("ll", "y"), ("rr", "r"), ("ss", "s"), ("nn", "n"),
    ("mm", "m"), ("tt", "t"), ("pp", "p"), ("cc", "k"),
    ("ce", "se"), ("ci", "si"), ("cy", "si"),
    ("c", "k"), ("z", "s"), ("v", "b"), ("w", "b"), ("h", ""), ("y", "i"),
)


def skeleton(word: str) -> str:
    """Forma fonetica cruda: 'paquetes' -> 'paketes', 'paketed' -> 'paketed'."""
    out = fold(word)
    for src, dst in _SKELETON_RULES:
        out = out.replace(src, dst)
    # letras repetidas que sobrevivieron ('bultoss' -> 'bultos')
    return re.sub(r"(.)\1+", r"\1", out)


def edit_distance_le1(a: str, b: str) -> bool:
    """True si `a` y `b` estan a distancia de edicion <= 1. Corta temprano."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = sum(1 for x, y in zip(a, b) if x != y)
        return diff <= 1
    shorter, longer = (a, b) if la < lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
            continue
        if skipped:
            return False
        skipped = True
        j += 1
    return True


@dataclass(frozen=True)
class NounHit:
    """Un sustantivo de bulto reconocido, con como se lo reconocio."""
    canonical: str
    code: str
    confidence: float
    method: str            # catalog | abbreviation | typo
    matched: str


@dataclass(frozen=True)
class PackageLexicon:
    """Todo lo que el parser necesita, ya compilado."""
    nouns: dict[str, dict]                 # alias foldeado -> {canonical, code}
    noun_skeletons: dict[str, dict]        # esqueleto -> {canonical, code}
    abbreviations: dict[str, str]          # abreviatura -> canonical
    number_words: dict[str, int]
    group_words: dict[str, int]            # 'docena' -> 12
    fraction_words: dict[str, float]       # 'medio' -> 0.5
    weight_units: dict[str, float]
    dimension_units: dict[str, float]
    volume_units: dict[str, float]
    per_unit_cues: tuple[str, ...]
    total_cues: tuple[str, ...]
    per_unit_connectors: frozenset[str]
    noun_guards: dict[str, frozenset[str]]
    quantity_re: re.Pattern[str]
    weight_re: re.Pattern[str]
    cue_before_re: re.Pattern[str]
    count_before_re: re.Pattern[str]
    count_after_re: re.Pattern[str]
    dimension_re: re.Pattern[str]
    volume_re: re.Pattern[str]
    label_left_re: re.Pattern[str]
    per_unit_re: re.Pattern[str]
    total_re: re.Pattern[str]
    bare_noun_re: re.Pattern[str]
    labelled_weight_re: re.Pattern[str]

    # ---------- lookup de sustantivos ----------

    def noun(self, word: str) -> NounHit | None:
        """Exacto -> abreviatura -> typo. Nunca al reves: el exacto manda."""
        key = fold(word).strip(".")
        if not key:
            return None
        if found := self.nouns.get(key):
            return NounHit(found["canonical"], found["code"], CONF_EXACT,
                           "catalog", key)
        if canonical := self.abbreviations.get(key):
            code = (self.nouns.get(canonical) or {}).get("code", "")
            return NounHit(canonical, code, CONF_ABBREV, "abbreviation", key)
        # Plural regular antes que fuzzy: 'bidones' es el plural de 'bidon', no
        # un typo. El catalogo no lista todos los plurales de 9 idiomas y no
        # tiene por que: la morfologia es una regla, no un dato.
        for stem in self._singulars(key):
            if found := self.nouns.get(stem):
                return NounHit(found["canonical"], found["code"], CONF_EXACT,
                               "plural", key)
        if len(key) < MIN_FUZZY_LEN:
            return None
        probe = skeleton(key)
        if found := self.noun_skeletons.get(probe):
            return NounHit(found["canonical"], found["code"], CONF_FUZZY,
                           "typo", key)
        # Dos letras iniciales iguales + distancia 1 sobre 5+ letras: suficiente
        # para 'paketed'~'paketes' y demasiado estrecho para que 'Caballito' o
        # 'Palermo' caigan en un sustantivo de bulto.
        for candidate, meaning in self.noun_skeletons.items():
            if len(candidate) >= MIN_FUZZY_LEN \
                    and abs(len(candidate) - len(probe)) <= 1 \
                    and candidate[:2] == probe[:2] \
                    and edit_distance_le1(candidate, probe):
                return NounHit(meaning["canonical"], meaning["code"], CONF_FUZZY,
                               "typo", key)
        return None

    @staticmethod
    def _singulars(key: str):
        """Candidatos a singular de un plural regular, del mas especifico al menos.

        'bidones'→'bidon', 'cajas'→'caja', 'boxes'→'box', 'sacchi'→'sacco'.
        No intenta ser un lematizador: solo desarma los plurales que romperian
        una lista de alias, y solo cuenta si el resultado ESTA en el catalogo.
        """
        # Una letra final duplicada es un dedazo, no una marca de plural:
        # 'bultoss' tiene que ir por el camino del typo (y su confianza), no
        # entrar como plural regular de 'bultos'.
        if len(key) > 2 and key[-1] == key[-2]:
            return
        if len(key) > 5 and key.endswith("es"):
            yield key[:-2]
            yield key[:-2] + "e"
        if len(key) > 3 and key.endswith("s"):
            yield key[:-1]
        if len(key) > 4 and key.endswith("i"):          # italiano: pacchi→pacco
            yield key[:-1] + "o"
        if len(key) > 4 and key.endswith("en"):         # aleman/neerlandes
            yield key[:-2]

    def guarded(self, matched: str, following: str) -> bool:
        """'2 sobre la mesa' no son dos sobres: `sobre` ahi es preposicion."""
        blocked = self.noun_guards.get(fold(matched).strip("."))
        if not blocked:
            return False
        head = fold(following).strip().split()
        return bool(head and head[0] in blocked)


def _alt(words) -> str:
    """Alternancia regex, mas largo primero para que gane el alias especifico."""
    parts = sorted({w for w in words if w}, key=len, reverse=True)
    if not parts:
        return "(?!)"
    return "|".join(re.escape(p) for p in parts)


@lru_cache(maxsize=4)
def get_package_lexicon(locales: tuple[str, ...] | None = None) -> PackageLexicon:
    """Compila una vez. `locales` queda por simetria con el resto del extractor:
    el catalogo de packaging no esta particionado por idioma y no conviene
    particionarlo — un archivo argentino trae 'pallet' y 'box' igual."""
    data = package_lexicon()
    catalog = packaging_alias_map()

    nouns = {alias: {"canonical": meaning["canonical"], "code": meaning["code"]}
             for alias, meaning in catalog.items()}
    # 'unit'/'piece' son del catalogo pero tambien palabras de direccion; el
    # parser las acepta solo con numero delante, nunca sueltas.
    skeletons: dict[str, dict] = {}
    for alias, meaning in nouns.items():
        skeletons.setdefault(skeleton(alias), meaning)

    abbreviations = {fold(k): v for k, v in (data.get("abbreviations") or {}).items()}
    numbers = {fold(k): int(v) for k, v in (data.get("number_words") or {}).items()}
    groups = {fold(k): int(v) for k, v in (data.get("group_words") or {}).items()}
    fractions = {fold(k): float(v)
                 for k, v in (data.get("fraction_words") or {}).items()}
    weight_units = {fold(k): float(v) for k, v in (data.get("weight_units") or {}).items()}
    dim_units = {(k if k == '"' else fold(k)): float(v)
                 for k, v in (data.get("dimension_units") or {}).items()}
    vol_units = {fold(k): float(v) for k, v in (data.get("volume_units") or {}).items()}
    per_unit = tuple(sorted((fold(c) for c in data.get("per_unit_cues") or ()),
                            key=len, reverse=True))
    totals = tuple(sorted((fold(c) for c in data.get("total_cues") or ()),
                          key=len, reverse=True))
    connectors = frozenset(fold(c) for c in data.get("per_unit_connectors") or ())
    guards = {fold(k): frozenset(fold(w) for w in v)
              for k, v in (data.get("noun_guards") or {}).items()}

    noun_alt = _alt([*nouns, *abbreviations])
    # cualquier palabra de 3+ letras puede ser un typo de un sustantivo: se deja
    # que el regex la capture y `noun()` decide. Sin esto un typo nunca llega.
    word_alt = r"[^\W\d_]{3,20}\.?"
    number_alt = _alt(numbers)

    # 'docena DE cajas', 'dozen OF boxes': el conector no cambia la cantidad,
    # pero sin el la frase no matchea.
    count_conn = _alt(c for c in connectors if len(c) > 1)

    quantity_re = re.compile(
        rf"(?<![\w/])(?:(?P<mult>x\s*)?(?P<digits>\d{{1,4}})"
        rf"|(?P<word>{number_alt})(?![^\W\d_]))"
        rf"\s*(?P<sep>[-x×]?\s*)(?:(?:{count_conn})\s+)?"
        rf"(?:(?P<group>{_alt(groups)})\s+(?:(?:{count_conn})\s+)?)?"
        rf"(?P<noun>(?:{noun_alt})\.?|{word_alt})(?![\w/])",
        re.IGNORECASE | re.UNICODE)

    # El numero de un peso viene de cuatro formas y las cuatro son comunes:
    #   '3 kg' · '1/2 kg' · 'un kilo' · 'medio kilo'
    # y ademas puede llevar el medio colgado atras: '2 kilos y medio'. Por eso
    # el numero es OPCIONAL: sin el, 'kilo y medio' no matchearia nunca.
    fraction_alt = _alt(fractions)
    article_alt = _alt(fold(a) for a in data.get("number_articles") or ())
    joiner_alt = _alt(fold(j) for j in data.get("half_joiners") or ())
    weight_re = re.compile(
        rf"(?<![\w.,])"
        rf"(?:(?:(?P<n>\d{{1,5}}(?:[.,]\d{{1,3}})?(?:\s*/\s*\d{{1,2}})?)"
        rf"|(?P<nword>{number_alt}|{fraction_alt})(?![^\W\d_]))"
        rf"\s*(?:(?:{article_alt})\s+)?)?"
        rf"(?P<unit>{_alt(weight_units)})(?![\w])"
        rf"(?:\s*(?:{joiner_alt})\s+(?P<half>{fraction_alt})(?![^\W\d_]))?",
        re.IGNORECASE)

    # 'cada una DE 2 kg' / 'je 5 kg': la pista viene ANTES del numero
    cue_before_re = re.compile(
        rf"(?<![\w])(?P<cue>{_alt(per_unit)})"
        rf"(?:\s+(?:{count_conn}))?[\s,;:.\-()/]*$",
        re.IGNORECASE)
    # '3 x 5kg' y '5kg x 3': el multiplicador da la cantidad
    count_before_re = re.compile(r"(?<!\d)(?P<n>\d{1,3})\s*[x×]\s*$", re.IGNORECASE)
    count_after_re = re.compile(r"^\s*[x×]\s*(?P<n>\d{1,3})(?!\d)", re.IGNORECASE)

    # '40x30x20', '40 x 30 x 20 cm' y tambien '40 POR 30 POR 20 cm'
    joiners = _alt(fold(j) for j in data.get("dimension_joiners") or ("x",))
    sep = rf"\s*(?:{joiners})\s*"
    dimension_re = re.compile(
        rf"(?<![\w.,])(?P<l>\d{{1,4}}(?:[.,]\d{{1,2}})?)\s*(?:{_alt(dim_units)})?{sep}"
        rf"(?P<w>\d{{1,4}}(?:[.,]\d{{1,2}})?)\s*(?:{_alt(dim_units)})?{sep}"
        rf"(?P<h>\d{{1,4}}(?:[.,]\d{{1,2}})?)\s*(?P<unit>{_alt(dim_units)})?(?![\w])",
        re.IGNORECASE)

    volume_re = re.compile(
        rf"(?<![\w.,])(?P<n>\d{{1,5}}(?:[.,]\d{{1,3}})?)\s*"
        rf"(?P<unit>{_alt(vol_units)})(?![\w])",
        re.IGNORECASE)

    labels = [*(data.get("quantity_labels") or ()), *(data.get("weight_labels") or ()),
              *(data.get("dimension_labels") or ()), *(data.get("volume_labels") or ())]
    label_left_re = re.compile(rf"(?:{_alt(fold(x) for x in labels)})\s*[:=]?\s*$",
                               re.IGNORECASE)

    labelled_weight_re = re.compile(
        rf"(?<![\w])(?P<label>{_alt(fold(x) for x in data.get('weight_labels') or ())})"
        rf"\s*[:=]?\s*(?P<n>\d{{1,5}}(?:[.,]\d{{1,3}})?)(?![\w])",
        re.IGNORECASE)

    per_unit_re = re.compile(rf"(?<![\w])(?:{_alt(per_unit)})(?![\w])", re.IGNORECASE)
    total_re = re.compile(rf"(?<![\w])(?:{_alt(totals)})(?![\w])", re.IGNORECASE)
    bare_noun_re = re.compile(rf"(?<![\w/])(?P<noun>{noun_alt}|{word_alt})(?![\w/])",
                              re.IGNORECASE | re.UNICODE)

    return PackageLexicon(
        nouns=nouns, noun_skeletons=skeletons, abbreviations=abbreviations,
        number_words=numbers, group_words=groups, fraction_words=fractions,
        cue_before_re=cue_before_re, count_before_re=count_before_re,
        count_after_re=count_after_re,
        weight_units=weight_units, dimension_units=dim_units,
        volume_units=vol_units, per_unit_cues=per_unit, total_cues=totals,
        per_unit_connectors=connectors, noun_guards=guards,
        quantity_re=quantity_re, weight_re=weight_re, dimension_re=dimension_re,
        volume_re=volume_re, label_left_re=label_left_re, per_unit_re=per_unit_re,
        total_re=total_re, bare_noun_re=bare_noun_re,
        labelled_weight_re=labelled_weight_re,
    )
