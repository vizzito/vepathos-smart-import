"""Explicar una calle mal escrita con UNA calle real del indice.

Existe por las planillas de reparto (Tandil, 2026-09-15): la gente no escribe el
nombre oficial de OSM. Escribe el apellido ('alvarado 471' por 'General
Rudecindo Alvarado'), lo corta ('Trabajadores Mun'), usa iniciales ('Lisandro
dlt'), le pega un nombre adelante ('Mariela entre rios 711') o lo escribe de
oido ('Lungui' por 'Lunghi', 'Pelegrini', 'jasmines'). El geocoder las
descartaba con "la calle del candidato no coincide", aunque en el extract haya
una sola calle posible.

Reglas, en orden de conviccion:

  surname       la consulta es el FINAL del nombre oficial ('dufau' ⊂ 'intendente dufau')
  initials      un token son las iniciales de varios ('dlt' = 'de la torre')
  truncated     cada token es prefijo (>= 3 letras) del token oficial ('mun' → 'municipales')
  subsequence   la consulta saltea nombres del medio o usa iniciales ('jose artigas' →
                'José Gervasio Artigas', 'pedro i rivera' → 'Pedro Ignacio Rivera');
                el ultimo token ancla el apellido
  prefix_noise  sobran palabras adelante/atras que no aparecen en NINGUNA calle del
                extract ('mariela', 'almacen'); lo que queda matchea
  typo          misma cantidad de tokens, cada uno igual por clave fonetica o por
                parecido >= 0.80 ('lungui'~'lunghi', 'crisantelmos'~'crisantemos')

Y dos guardas que no se negocian:

  * UNICIDAD. Si dos calles distintas explican la consulta igual de bien, no se
    elige: 'moreno' con 'Perito Moreno' y 'Carlos Moreno' en el extract no se
    resuelve. Elegir por cercania al depot es exactamente el falso positivo que
    el resto del geocoder evita.
  * NUNCA pisa una calle que existe. Si el nombre pedido esta tal cual (con o
    sin tipo de via), no hay nada que resolver: 'Sarmiento' no se convierte en
    'Fragata Sarmiento'. Este modulo solo corre cuando la busqueda normal fallo.

Todo resultado resuelto sale en ambar (el geocoder marca `street_resolved`): el
operador ve la calle corregida y confirma. Corregir no puede pintar verde.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache

from ..resources import fold, label_set, name_glue_words
from .address import normalize_text

#: cuanto convence cada tipo de resolucion (desempate entre tipos)
KIND_RANK = {"abbreviation": 6, "surname": 5, "initials": 5, "truncated": 4, "subsequence": 4,
             "prefix_noise": 3, "typo": 2}
#: un apellido suelto mas corto que esto es demasiado comun ('paz', 'juan')
MIN_SURNAME_CHARS = 5
#: prefijo minimo de un token truncado ('mun' → 'municipales')
MIN_PREFIX_CHARS = 3
#: parecido minimo por token para 'typo' (rapidfuzz ratio, 0..1)
TYPO_TOKEN_RATIO = 0.80
#: parecido minimo del nombre entero para 'typo'
TYPO_NAME_RATIO = 0.85
#: dos resoluciones a menos de esto son empate → ambiguo
TIE_MARGIN = 0.03

_GLUE_EXTRA = {"de", "del", "la", "las", "el", "los", "y", "e", "da", "do", "dos",
               "das", "di", "du", "des", "le", "the", "of"}


@dataclass(frozen=True)
class StreetResolution:
    name: str
    kind: str
    score: float
    query: str

    def as_detail(self) -> dict:
        return {"from": self.query, "to": self.name, "kind": self.kind,
                "score": round(self.score, 3)}


@lru_cache(maxsize=1)
def _skip_words() -> frozenset[str]:
    return frozenset(label_set("street_tokens", None)) | name_glue_words() | frozenset(_GLUE_EXTRA)


def phonetic_key(word: str) -> str:
    """Clave para errores de oido (castellano/italiano/ingles de calles LATAM).

    gh/gu+i/e → g, ll → l (y dobles en general), z/ce/ci → s, v → b, qu/k → c,
    h muda. No es Soundex: la idea es que 'Lunghi' y 'Lungui' colisionen y que
    'Lima' y 'Lina' NO.
    """
    s = fold(word).lower()
    s = re.sub(r"[^a-z]", "", s)
    s = re.sub(r"gh", "g", s)
    s = re.sub(r"gu(?=[ei])", "g", s)
    s = re.sub(r"qu(?=[ei])", "c", s)
    s = re.sub(r"c(?=[ei])", "s", s)
    s = s.replace("z", "s").replace("v", "b").replace("k", "c").replace("ph", "f")
    s = re.sub(r"(?<![csp])h", "", s)
    s = re.sub(r"(.)\1+", r"\1", s)
    return s


def _ratio(a: str, b: str) -> float:
    from rapidfuzz import fuzz
    return fuzz.ratio(a, b) / 100.0


@lru_cache(maxsize=1)
def _title_abbreviations() -> dict[str, str]:
    from ..resources import string_map
    return {fold(k): v for k, v in string_map("geo_keywords.json", "title_abbreviations").items()}


def name_tokens(text: str, *, titles: bool = False) -> tuple[list[str], list[str]]:
    """(tokens completos, tokens de contenido) de un nombre de calle ya normalizado.

    `titles=True` expande titulos abreviados ('gral' → 'general'). Solo lo usa la
    resolucion: en la normalizacion global movia pines que hoy estan bien.
    """
    # fold ademas de normalize_text: 'Weißgasse' y 'WEISSGASSE' son la misma clave
    full = [fold(t) for t in normalize_text(text).split() if t]
    if titles:
        abbr = _title_abbreviations()
        full = [abbr.get(fold(t), t) for t in full]
    skip = _skip_words()
    content = [t for t in full if fold(t) not in skip and not t.isdigit()]
    return full, content


@lru_cache(maxsize=1)
def _suffixes() -> frozenset[str]:
    return frozenset(fold(x) for x in label_set("street_suffixes", None))


def _glued_suffix(official: str, typed: str) -> bool:
    """'mozartstraat' = 'mozart' + tipo de via pegado (nl/de/nordicos)."""
    rest = fold(official)[len(fold(typed)):]
    return bool(rest) and rest in _suffixes()


def _initials_match(token: str, words: list[str]) -> bool:
    if not (2 <= len(token) <= 4) or not token.isalpha():
        return False
    return len(words) == len(token) and "".join(w[0] for w in words) == token


class StreetVocabulary:
    """Nombres de calle del extract, agrupados por tokens de contenido."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._built = False
        self.by_key: dict[tuple[str, ...], str] = {}
        #: todas las grafias del mismo nombre ('Marzocca' way + 'Calle 2 - Marzocca' addr)
        self.names_by_key: dict[tuple[str, ...], list[str]] = {}
        self.full_tokens: dict[tuple[str, ...], list[str]] = {}
        self.tokens: set[str] = set()
        self._joined: list[str] = []
        self._joined_keys: list[tuple[str, ...]] = []

    def _build(self) -> None:
        if self._built:
            return
        counts: dict[tuple[str, ...], dict[str, int]] = {}
        rows = self._conn.execute(
            "SELECT street, COUNT(*) FROM places WHERE street IS NOT NULL AND street <> ''"
            " GROUP BY street"
            " UNION ALL "
            "SELECT name, 0 FROM places WHERE kind = 'highway' AND name IS NOT NULL"
            " AND name <> '' GROUP BY name").fetchall()
        for name, n in rows:
            full, content = name_tokens(name)
            if not content:
                continue
            key = tuple(content)
            bucket = counts.setdefault(key, {})
            bucket[name] = bucket.get(name, 0) + int(n or 0)
            self.tokens.update(content)
            if key not in self.full_tokens or len(full) > len(self.full_tokens[key]):
                self.full_tokens[key] = full
        for key, names in counts.items():
            # el nombre con mas direcciones es el que usan los addr:street del extract
            self.by_key[key] = max(names.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
            self.names_by_key[key] = sorted(names)
        self._joined_keys = list(self.by_key)
        self._joined = [" ".join(k) for k in self._joined_keys]
        self._built = True

    def exists(self, query_road: str) -> bool:
        """La calle pedida existe tal cual (con o sin tipo de via)."""
        self._build()
        _, content = name_tokens(query_road)
        return bool(content) and tuple(content) in self.by_key

    def canonical(self, side: str) -> tuple[StreetResolution | None, list[str]]:
        """(resolucion, grafias) de una calle nombrada suelta: exacta o resuelta."""
        self._build()
        _, content = name_tokens(side)
        key = tuple(content)
        if key and key in self.by_key:
            return (StreetResolution(self.by_key[key], "exact", 1.0, " ".join(content)),
                    self.names_by_key[key])
        res = self.resolve(side)
        if res is None:
            return None, []
        _, rcontent = name_tokens(res.name)
        return res, self.names_by_key.get(tuple(rcontent), [res.name])

    def resolve(self, query_road: str) -> StreetResolution | None:
        self._build()
        raw_full, raw = name_tokens(query_road)
        if not raw or tuple(raw) in self.by_key:
            return None
        q_full, q = name_tokens(query_road, titles=True)
        if tuple(q) in self.by_key:
            # 'Avenida Gral San Martin' es 'Avenida General San Martín' tal cual
            return StreetResolution(self.by_key[tuple(q)], "abbreviation", 1.0, " ".join(raw))
        found: dict[tuple[str, ...], tuple[str, float]] = {}

        def offer(key: tuple[str, ...], kind: str, score: float) -> None:
            prev = found.get(key)
            if prev is None or (KIND_RANK[kind], score) > (KIND_RANK[prev[0]], prev[1]):
                found[key] = (kind, score)

        self._surname_and_truncated(q, offer)
        self._subsequence(q, offer)
        self._initials(q_full, offer)
        self._noise(q, offer)
        self._typos(q, offer, q_full=q_full)
        if not found:
            return None

        ranked = sorted(found.items(), key=lambda kv: (KIND_RANK[kv[1][0]], kv[1][1]),
                        reverse=True)
        best_key, (best_kind, best_score) = ranked[0]
        if len(ranked) > 1:
            _, (kind2, score2) = ranked[1]
            if KIND_RANK[kind2] == KIND_RANK[best_kind] and best_score - score2 < TIE_MARGIN:
                return None                    # dos calles igual de buenas: no se elige
        return StreetResolution(self.by_key[best_key], best_kind, best_score,
                                " ".join(q))

    # -- reglas ---------------------------------------------------------------

    def _surname_and_truncated(self, q: list[str], offer) -> None:
        n = len(q)
        for key in self.by_key:
            if len(key) < n:
                continue
            window = list(key[len(key) - n:])
            if window == q:
                if len(key) > n and sum(len(t) for t in q) >= MIN_SURNAME_CHARS:
                    offer(key, "surname", 1.0)
                continue
            if all(k == t or (len(t) >= MIN_PREFIX_CHARS and k.startswith(t))
                   for k, t in zip(window, q)) and sum(len(t) for t in q) >= 6:
                # Un token solo truncado ('martin' → 'martinez') es un apellido
                # distinto, no una abreviatura. Solo vale si lo que falta es el
                # tipo de via pegado ('mozart' → 'mozartstraat').
                if n > 1 or _glued_suffix(window[0], q[0]):
                    offer(key, "truncated", 0.9 + 0.1 * (len(window) == len(key)))
            # truncado al principio del nombre ('amer rey' → 'americo reynoso', 'la pamp')
            head = list(key[:n])
            if n > 1 and head != window and all(
                    k == t or (len(t) >= MIN_PREFIX_CHARS and k.startswith(t))
                    for k, t in zip(head, q)) and sum(len(t) for t in q) >= 6 \
                    and any(k != t for k, t in zip(head, q)):
                offer(key, "truncated", 0.88)

    def _subsequence(self, q: list[str], offer) -> None:
        """Tokens de la consulta en orden dentro de un nombre mas largo.

        Cada token es igual, prefijo (>= 3 letras) o inicial de un token oficial;
        el ultimo tiene que ser el ultimo del nombre (el apellido ancla) y al menos
        uno tiene que ser una palabra entera de 4+ letras. Si la consulta es un
        nombre de calle de otro partido tal cual ('Artigas'), esto no corre: la
        consulta ('jose artigas') no existe y lo mas parecido es el nombre largo.
        """
        n = len(q)
        if n < 2:
            return

        def match(t: str, k: str) -> bool:
            return t == k or (len(t) >= MIN_PREFIX_CHARS and k.startswith(t)) or (
                len(t) == 1 and k.startswith(t))

        last = q[-1]
        # iniciales sueltas de mas en la consulta: 'perito f p moreno' → 'perito moreno'
        without_initials = tuple(t for t in q if len(t) > 1)
        if len(without_initials) < n and len(without_initials) >= 1 \
                and without_initials in self.by_key and sum(map(len, without_initials)) >= 6:
            offer(without_initials, "subsequence", 0.94)
        for key in self.by_key:
            if len(key) < n or not match(last, key[-1]):
                continue
            j, whole, skipped, loose = 0, 0, 0, 0
            ok = True
            for t in q[:-1]:
                while j < len(key) - 1 and not match(t, key[j]):
                    j += 1
                    skipped += 1
                if j >= len(key) - 1:
                    ok = False
                    break
                whole += int(t == key[j] and len(t) >= 4)
                loose += int(t != key[j])
                j += 1
            skipped += (len(key) - 1) - j
            whole += int(last == key[-1] and len(last) >= 4)
            loose += int(last != key[-1])
            if not ok or whole < 1 or (skipped == 0 and loose == 0):
                continue
            # cada nombre salteado resta: entre 'Francisco Suárez' y 'República y
            # Francisco Suárez', 'f suarez' es el primero
            offer(key, "subsequence", 0.93 - 0.04 * skipped)

    def _initials(self, q_full: list[str], offer) -> None:
        if not any(2 <= len(t) <= 4 and t.isalpha() and t not in self.tokens for t in q_full):
            return
        for key, full in self.full_tokens.items():
            for i, token in enumerate(q_full):
                if not (2 <= len(token) <= 4) or not token.isalpha() or token in self.tokens:
                    continue
                head, tail = q_full[:i], q_full[i + 1:]
                if tail or len(head) + len(token) > len(full):
                    continue
                if full[:len(head)] != head:
                    continue
                rest = full[len(head):]
                if _initials_match(token, rest):
                    offer(key, "initials", 0.97)

    def _noise(self, q: list[str], offer) -> None:
        """Sobran palabras adelante o atras ('mariela entre rios', 'la pamp almacen').

        Se prueba cada recorte. Una palabra sacada que no aparece en ninguna calle
        del extract es ruido gratis; una que si aparece cuesta un poco mas, porque
        podria ser parte de otro nombre.
        """
        n = len(q)
        if n < 2:
            return
        for lead in range(0, n):
            for trail in range(n, lead, -1):
                removed = q[:lead] + q[trail:]
                # '12e arrondissement', 'apt b022': lo que sobra con digitos es
                # direccion (distrito, unidad), no un nombre de persona o comercio
                if not removed or any(any(ch.isdigit() for ch in t) for t in removed):
                    continue
                core = q[lead:trail]
                if sum(len(t) for t in core) < MIN_SURNAME_CHARS:
                    continue
                # 'sladanha' no es ruido: es 'saldanha' mal escrito. Una palabra que
                # se parece mucho (sin ser igual) a una palabra de calle del extract
                # no se descarta; si se descarta, el resto elige otra calle.
                if any(t not in self.tokens and self._near_street_word(t) for t in removed):
                    continue
                penalty = sum(0.02 if t not in self.tokens else 0.05 for t in removed)
                key = tuple(core)
                if key in self.by_key:
                    offer(key, "prefix_noise", 0.95 - penalty)
                    continue
                hits: dict[tuple[str, ...], float] = {}

                def inner(k, kind, score, _hits=hits):
                    _hits[k] = max(score, _hits.get(k, 0.0))

                self._surname_and_truncated(core, inner)
                self._typos(core, inner)
                for k, score in hits.items():
                    offer(k, "prefix_noise", min(0.9, score) - penalty)

    def _near_street_word(self, token: str) -> bool:
        if len(token) < 5:
            return False
        cache = self.__dict__.setdefault("_near_cache", {})
        if token not in cache:
            from rapidfuzz import fuzz, process

            words = self.__dict__.get("_token_list")
            if words is None:
                words = self.__dict__["_token_list"] = sorted(self.tokens)
            hit = process.extractOne(token, words, scorer=fuzz.ratio)
            cache[token] = bool(hit and hit[1] >= 80 and hit[0] != token)
        return cache[token]

    def _typos(self, q: list[str], offer, q_full: list[str] | None = None) -> None:
        joined = " ".join(q)
        if len(joined.replace(" ", "")) < 5:
            return
        from rapidfuzz import fuzz, process

        n = len(q)
        # candidatos por parecido del final del nombre (el apellido manda)
        choices = {i: " ".join(k[len(k) - n:]) for i, k in enumerate(self._joined_keys)
                   if len(k) >= n}
        for _, score, idx in process.extract(joined, choices, scorer=fuzz.ratio, limit=12):
            if score / 100.0 < TYPO_NAME_RATIO - 0.1:
                continue
            key = self._joined_keys[idx]
            window = list(key[len(key) - n:])
            ok = True
            exactish = 0
            for k, t in zip(window, q):
                if k == t:
                    exactish += 1
                    continue
                if len(t) < 4:
                    ok = False
                    break
                if phonetic_key(k) == phonetic_key(t):
                    exactish += 1
                    continue
                if _ratio(k, t) < TYPO_TOKEN_RATIO or k[0] != t[0]:
                    ok = False
                    break
            if not ok:
                continue
            name_ratio = _ratio(" ".join(window), joined)
            if exactish == 0 and name_ratio < TYPO_NAME_RATIO:
                continue
            # un token corto sin coincidencia fonetica es un nombre de persona
            # tanto como una calle ('Santino' ~ 'Sandino'): pide mas parecido
            if n == 1 and exactish == 0 and len(joined) <= 7 and name_ratio < 0.9:
                continue
            if len(key) > n and sum(len(t) for t in q) < MIN_SURNAME_CHARS:
                continue
            # La consulta trajo tipo de via o articulos ('Chemin des Meures'): la
            # cola del nombre oficial tiene que parecerse ENTERA. Si no, un typo
            # sobre el ultimo token elige otra calle ('Chemin de Chez Le Meure').
            if q_full and len(q_full) > n:
                tail = self.full_tokens.get(key, [])[-len(q_full):]
                if _ratio(" ".join(q_full), " ".join(tail)) < TYPO_TOKEN_RATIO:
                    continue
            offer(key, "typo", max(name_ratio, 0.86 if exactish else 0.0))
