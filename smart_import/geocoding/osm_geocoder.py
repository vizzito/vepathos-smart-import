"""Geocoder local contra el indice SQLite construido desde un .osm.pbf."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .address import ParsedAddress, normalize_text, parse
from .base import (
    STATUS_ERROR, STATUS_LOW, STATUS_MATCHED, STATUS_NOT_FOUND, GeocodeResult,
)
from .interpolate import HousePoint, interpolate_house, parse_polyline, MAX_SPAN
from .name_aliases import StreetAliasStore, open_alias_store
from .osm_index import touch_index
from .scoring import (
    Candidate, _house_int, _house_number_match, place_conflict,
    _street_score, _strip_way_type, compass_key, en_ordinal, ordinal_key, score,
)

CANDIDATE_LIMIT = 40
#: Si OSM no tiene la altura exacta, se piden mas filas de ESA calle (no el
#: top-N de bm25, que en avenidas largas son las alturas 1-200).
NEAR_HOUSE_STREETS = 12
NEAR_HOUSE_PER_STREET = 12
_STREET_PREFIXES = (
    "", "Avenida ", "Av. ", "Av ", "Calle ", "Paseo ", "Pasaje ",
    "Boulevard ", "Diagonal ",
    "Avenue ", "Ave. ", "Ave ", "Rue ", "Boulevard ", "Bd ", "Bd. ",
    "Strada ", "Str. ", "Str ", "Calea ", "Via ", "Ulica ", "Ul. ",
)
_FTS_UNSAFE = re.compile(r"[^\w\s]", re.UNICODE)
_HOUSE_SPLIT = re.compile(r"[-/]")
#: NE/NW se expanden a 'noreste' en normalize_text; OSM US indexa 'northeast'.
#: E/W/N/S → 'este'/'oeste'; OSM indexa 'east'/'west'.
_COMPASS_FTS = {
    "ne": ("northeast", "noreste"),
    "nw": ("northwest", "noroeste"),
    "se": ("southeast", "sureste"),
    "sw": ("southwest", "suroeste"),
    "noreste": ("northeast", "ne"),
    "noroeste": ("northwest", "nw"),
    "sureste": ("southeast", "se"),
    "suroeste": ("southwest", "sw"),
    "northeast": ("ne", "noreste"),
    "northwest": ("nw", "noroeste"),
    "southeast": ("se", "sureste"),
    "southwest": ("sw", "suroeste"),
    "e": ("east", "este"),
    "este": ("east", "e"),
    "east": ("este", "e"),
    "w": ("west", "oeste"),
    "oeste": ("west", "w"),
    "west": ("oeste", "w"),
    "n": ("north", "norte"),
    "norte": ("north", "n"),
    "north": ("norte", "n"),
    "s": ("south", "sur"),
    "sur": ("south", "s"),
    "south": ("sur", "s"),
}
_ORDINAL_FTS = {
    "1st": ("first",), "first": ("1st",),
    "2nd": ("second",), "second": ("2nd",),
    "3rd": ("third",), "third": ("3rd",),
    "4th": ("fourth",), "fourth": ("4th",),
    "5th": ("fifth",), "fifth": ("5th",),
    "7th": ("seventh",), "seventh": ("7th",),
}
_US_COMPASS_TITLE = {
    "ne": "Northeast", "nw": "Northwest",
    "se": "Southeast", "sw": "Southwest",
    "e": "East", "w": "West", "n": "North", "s": "South",
}
_US_WAY_TITLES = (
    "Avenue", "Street", "Drive", "Boulevard", "Court", "Terrace",
    "Place", "Lane", "Road", "Parkway", "Expressway",
    "Ave", "St", "Dr", "Blvd", "Pl", "Ln", "Rd", "Pkwy", "Expy",
)

SELECT_COLUMNS = ("p.id, p.lat, p.lon, p.kind, p.name, p.house_number, p.street,"
                  " p.city, p.district, p.state, p.postcode, p.country, p.normalized_text")


def _is_numbered_grid(words: list[str]) -> bool:
    """True si la calle es número + cardinal/letra (55 ST, AVE U), no un nombre."""
    if not words:
        return False
    for word in words:
        if compass_key(word):
            continue
        if word.isdigit() or ordinal_key(word):
            continue
        if len(word) == 1 and word.isalpha():
            continue
        return False
    return any(
        word.isdigit() or ordinal_key(word) or (len(word) == 1 and word.isalpha())
        for word in words
    )


def _expand_fts_words(words: list[str]) -> list[str]:
    expanded: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        key = token.casefold()
        if not token or key in seen:
            return
        seen.add(key)
        expanded.append(token)

    for word in words:
        add(word)
        key = word.casefold()
        for extra in _COMPASS_FTS.get(key, ()):
            add(extra)
        for extra in _ORDINAL_FTS.get(key, ()):
            add(extra)
        if word.isdigit() and 1 <= len(word) <= 3:
            nth = en_ordinal(word)
            add(nth)
            for extra in _ORDINAL_FTS.get(nth.casefold(), ()):
                add(extra)
    return expanded


def _house_sort_int(number: str) -> int | None:
    """Ancla para interpolar. '61-30' ≈ 61, no 6130 (CAST de SQLite)."""
    raw = (number or "").strip()
    if not raw:
        return None
    head = _HOUSE_SPLIT.split(raw, 1)[0]
    compact = _FTS_UNSAFE.sub("", head)
    if compact.isdigit():
        return int(compact.lstrip("0") or "0")
    return _house_int(raw)


class LocalOSMGeocoder:
    """Un indice = una region. Se abre una vez y se reutiliza para todo el archivo."""

    name = "osm"

    def __init__(self, index_path: str | Path, match_threshold: float = 0.90,
                 low_threshold: float = 0.70, street_level_floor: float = 0.60,
                 street_match_min: float = 0.80, review_band: float = 0.70,
                 valid_band: float = 0.80, soft_reject: bool = True,
                 soft_reject_min: float = 0.50,
                 aliases_path: str | Path | None = None):
        self.index_path = Path(index_path).expanduser()
        if not self.index_path.exists():
            raise FileNotFoundError(f"no existe el indice {self.index_path}. "
                                    f"Generalo con 'build-geocoder-index'.")
        self.match_threshold = match_threshold
        self.low_threshold = low_threshold
        self.street_level_floor = street_level_floor
        self.street_match_min = street_match_min
        self.review_band = review_band
        self.valid_band = valid_band
        self.soft_reject = soft_reject
        self.soft_reject_min = soft_reject_min
        self._aliases: StreetAliasStore | None = open_alias_store(aliases_path)
        self._conn = sqlite3.connect(f"file:{self.index_path}?mode=ro", uri=True)
        self._conn.execute("PRAGMA query_only = ON")
        touch_index(self.index_path)

    def close(self) -> None:
        self._conn.close()
        if self._aliases is not None:
            self._aliases.close()
            self._aliases = None

    def _prefix_street_variants(self, parsed: ParsedAddress) -> list[str]:
        """'AV CORRIENTES' → 'Avenida Corrientes' (nombre OSM, lookup por indice)."""
        if not parsed.road:
            return []
        raw = parsed.road.strip()
        nombre = _strip_way_type(normalize_text(raw)) or normalize_text(raw)
        titled = " ".join(w.capitalize() if not w.isdigit() else w
                          for w in nombre.split())
        titled_de = titled.replace(" De ", " de ")
        seen: set[str] = set()
        out: list[str] = []
        for seed in (nombre, titled, titled_de, raw, raw.title()):
            for pref in _STREET_PREFIXES:
                cand = f"{pref}{seed}".strip()
                if cand and cand not in seen:
                    seen.add(cand)
                    out.append(cand)
        compass = next((compass_key(w) for w in nombre.split() if compass_key(w)), None)
        ordinal = next((w for w in nombre.split() if ordinal_key(w)), None)
        if ordinal:
            key = ordinal_key(ordinal)
            forms = [ordinal]
            if key:
                nth = en_ordinal(key)
                if nth.casefold() != ordinal.casefold():
                    forms.append(nth)
            heads: list[str] = []
            if compass:
                if title := _US_COMPASS_TITLE.get(compass):
                    heads.append(title)
                heads.append(compass.upper())
            else:
                heads.append("")
            for way in _US_WAY_TITLES:
                for head in heads:
                    for form in forms:
                        cand = f"{head} {form} {way}".strip()
                        if cand and cand not in seen:
                            seen.add(cand)
                            out.append(cand)
        return out

    def _alias_variants(self, parsed: ParsedAddress) -> list[str]:
        if getattr(self, "_aliases", None) is None or not parsed.road:
            return []
        keys = [parsed.road, *self._prefix_street_variants(parsed)]
        return self._aliases.expand_many(keys)

    def _alias_norm_set(self, parsed: ParsedAddress) -> frozenset[str]:
        road = parsed.road or ""
        cached = getattr(self, "_alias_set_cache", None)
        if cached is not None and cached[0] == road:
            return cached[1]
        val = frozenset(normalize_text(v) for v in self._alias_variants(parsed) if v)
        self._alias_set_cache = (road, val)
        return val

    def _alias_fts_tokens(self, parsed: ParsedAddress) -> list[str]:
        """Tokens FTS de las grafias OSM emparejadas (Mozartstraat)."""
        from ..resources import fold, label_set, name_glue_words

        skip = label_set("street_tokens") | name_glue_words()
        out: list[str] = []
        seen: set[str] = set()
        for variant in self._alias_variants(parsed):
            for token in normalize_text(variant).split():
                clean = _FTS_UNSAFE.sub("", token)
                if not clean or fold(clean) in skip:
                    continue
                key = clean.casefold()
                if key in seen:
                    continue
                seen.add(key)
                out.append(clean)
        return out[:8]

    # ---------- busqueda ----------

    @staticmethod
    def _clean_tokens(parsed: ParsedAddress) -> tuple[list[str], list[str]]:
        """(palabras, numeros) del texto normalizado, listos para FTS."""
        words, numbers = [], []
        for token in parsed.tokens:
            clean = _FTS_UNSAFE.sub("", token)
            if not clean:
                continue
            if clean.isdigit():
                numbers.append(clean)
            elif len(clean) > 1:
                words.append(clean)
        return words[:8], numbers[:3]

    def _search_words(self, parsed: ParsedAddress) -> list[str]:
        """Palabras de la CALLE para FTS. Sin tipo de vía ni cola de país/ciudad.

        Si usamos todos los tokens, `"919" AND (corrientes OR argentina)` devuelve
        40 'Avenida Argentina' y ninguna fila de Corrientes. El depot/CABA se
        anexan a la query para scoring; no deben entrar al MATCH.

        Grilla US ('E 55 ST', '68 ST', 'AVE U'): el tipo de vía ES la identidad
        (55th Street ≠ 55th Avenue) y se conserva. '55' se expande a '55th';
        'este' (abbr de E) a 'east'.
        """
        from ..resources import fold, label_set, name_glue_words

        way_tokens = label_set("street_tokens")
        glue = name_glue_words()
        skip = way_tokens | glue
        if parsed.road:
            raw = normalize_text(parsed.road)
            words: list[str] = []
            ways: list[str] = []
            for token in raw.split():
                clean = _FTS_UNSAFE.sub("", token)
                if not clean:
                    continue
                # '11 de septiembre' tiene que buscar el 11; la altura de la
                # puerta no vive en road, no se cuela aca.
                if clean.isdigit():
                    words.append(clean)
                    continue
                folded = fold(clean)
                if folded in glue:
                    continue
                if folded in way_tokens:
                    ways.append(clean)
                    continue
                # 'AVE U': la letra es el nombre. 'st' ya salio como tipo.
                if len(clean) == 1 and not clean.isalpha():
                    continue
                if len(clean) > 1 or clean.isalpha():
                    words.append(clean)
            if words:
                core = list(words)
                if ways and _is_numbered_grid(core):
                    words.extend(ways)
                from ..text_script import cjk_tokens
                for tok in cjk_tokens(parsed.original or ""):
                    if tok not in words:
                        words.append(tok)
                return _expand_fts_words(words)[:16]
        words, _ = self._clean_tokens(parsed)
        return [w for w in words if fold(w) not in skip][:8]

    @staticmethod
    def _house_fts_variants(number: str) -> list[str]:
        compact = _FTS_UNSAFE.sub("", str(number))
        if not compact:
            return []
        variants = [compact]
        stripped = compact.lstrip("0")
        if stripped and stripped != compact:
            variants.append(stripped)
        return variants

    def _house_fts_clause(self, number: str) -> str | None:
        """MATCH de altura. El indice parte guiones: '40-49' → tokens 40 y 49.

        Esponente '15/B': OSM suele tener '15' o '15B'. No exigir el token 'B'
        (rompe el MATCH y el enhance manda 40124 como altura).
        """
        variants = self._house_fts_variants(number)
        chunks = [f'"{v}"' for v in variants]
        parts = [_FTS_UNSAFE.sub("", p) for p in _HOUSE_SPLIT.split(str(number).strip())]
        parts = [p for p in parts if p]
        if len(parts) >= 2 and parts[1].isalpha():
            head = parts[0]
            if head and f'"{head}"' not in chunks:
                chunks.append(f'"{head}"')
        elif len(parts) >= 2:
            chunks.append("(" + " AND ".join(f'"{p}"' for p in parts) + ")")
        if not chunks:
            return None
        uniq: list[str] = []
        seen: set[str] = set()
        for chunk in chunks:
            if chunk not in seen:
                seen.add(chunk)
                uniq.append(chunk)
        if len(uniq) == 1:
            return uniq[0]
        return "(" + " OR ".join(uniq) + ")"

    @staticmethod
    def _or_group(tokens: list[str]) -> str:
        uniq: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            key = token.casefold()
            if key not in seen:
                seen.add(key)
                uniq.append(token)
        if len(uniq) == 1:
            return f'"{uniq[0]}"'
        return "(" + " OR ".join(f'"{w}"' for w in uniq) + ")"

    def _street_fts(self, words: list[str], *, parsed: ParsedAddress | None = None,
                    strict: bool = True) -> str:
        """Nombre de calle para FTS.

        Sinónimos (este/east, 55/55th) van en OR; grupos distintos en AND.
        strict=True: AND de los tokens de nombre. 'fragata' AND 'sarmiento'
        no mete la homonima corta. Grilla US y calles-fecha siempre AND
        entre grupos, tambien en el fallback.
        strict=False: OR de los tokens de nombre. Solo cuando AND no encontro
        calle. Nunca afloja compass/ordinal/fecha.

        Alias bilingues (Avenue Mozart → Mozartstraat) van en OR con el nombre
        pedido: AND los romperia en indices viejos que solo indexaron `name`.
        """
        if not words:
            return ""
        from ..resources import fold, label_set

        way_tokens = label_set("street_tokens")
        compass, ordinals, ways, rest = [], [], [], []
        for word in words:
            if compass_key(word):
                compass.append(word)
            elif word.isdigit() or ordinal_key(word):
                ordinals.append(word)
            elif fold(word) in way_tokens:
                ways.append(word)
            else:
                rest.append(word)

        parts: list[str] = []
        if compass:
            parts.append(self._or_group(compass))
        if ordinals:
            parts.append(self._or_group(ordinals))
        if ways:
            parts.append(self._or_group(ways))
        alias_toks = self._alias_fts_tokens(parsed) if parsed is not None else []
        if rest:
            uniq_rest: list[str] = []
            seen: set[str] = set()
            for token in rest:
                key = token.casefold()
                if key not in seen:
                    seen.add(key)
                    uniq_rest.append(token)
            if len(uniq_rest) == 1:
                rest_clause = f'"{uniq_rest[0]}"'
            elif compass or ordinals:
                # 'West End': west AND end. No aflojar a OR.
                rest_clause = " AND ".join(f'"{w}"' for w in uniq_rest)
            else:
                joiner = " AND " if strict else " OR "
                rest_clause = joiner.join(f'"{w}"' for w in uniq_rest)
            if alias_toks:
                alias_clause = self._or_group(alias_toks)
                parts.append(f"({rest_clause} OR {alias_clause})")
            else:
                parts.append(rest_clause)
        elif alias_toks:
            parts.append(self._or_group(alias_toks))

        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return " AND ".join(parts)

    def _precise_query(self, parsed: ParsedAddress, *, strict: bool = True) -> str | None:
        """Altura AND nombre de calle. Es la consulta que encuentra direcciones."""
        words = self._search_words(parsed)
        _, numbers = self._clean_tokens(parsed)
        number = parsed.house_number or (numbers[0] if numbers else None)
        if not number or not words:
            return None
        num = self._house_fts_clause(number)
        if not num:
            return None
        calle = self._street_fts(words, parsed=parsed, strict=strict)
        if not calle:
            return None
        if " AND " in calle or calle.startswith("("):
            return f"{num} AND {calle}"
        return f"{num} AND ({calle})"

    def _fts_query(self, parsed: ParsedAddress, *, strict: bool = True) -> str:
        """Consulta amplia: tokens de calle (sin país / avenida)."""
        words = self._search_words(parsed)
        return self._street_fts(words, parsed=parsed, strict=strict) if words else ""

    def _run(self, query: str, bbox, limit: int) -> list[Candidate]:
        sql = (f"SELECT {SELECT_COLUMNS} FROM places_fts f"
               " JOIN places p ON p.id = f.rowid"
               " WHERE places_fts MATCH ?")
        params: list = [query]
        if bbox:
            north, south, east, west = bbox
            sql += (" AND p.id IN (SELECT id FROM places_rtree WHERE"
                    " max_lat >= ? AND min_lat <= ? AND max_lon >= ? AND min_lon <= ?)")
            params += [south, north, west, east]
        sql += " ORDER BY bm25(places_fts) LIMIT ?"
        params.append(limit)
        return [Candidate(*row) for row in self._conn.execute(sql, params)]

    def _add_fts(self, found: dict[int, Candidate], parsed: ParsedAddress,
                 bbox: tuple[float, float, float, float] | None, *,
                 strict: bool) -> None:
        if precise := self._precise_query(parsed, strict=strict):
            for cand in self._run(precise, bbox, CANDIDATE_LIMIT):
                found.setdefault(cand.id, cand)
        broad = self._fts_query(parsed, strict=strict)
        if broad:
            for cand in self._run(broad, bbox, CANDIDATE_LIMIT):
                found.setdefault(cand.id, cand)

    def _street_name_hit(self, parsed: ParsedAddress,
                         found: dict[int, Candidate]) -> bool:
        """True si ya hay un candidato de la calle pedida (no una homonima)."""
        if not found:
            return False
        if not parsed.road:
            return True
        for cand in found.values():
            street = normalize_text(cand.street or "")
            if _street_score(parsed, street, parsed.normalized,
                             aliases=self._alias_norm_set(parsed)) >= self.street_match_min:
                return True
        return False

    def _exact_house_usable(self, parsed: ParsedAddress, cand: Candidate) -> bool:
        """Altura exacta que cuenta para saltear nearby.

        Un homónimo provincial con 1800 no debe bloquear interpolar 1799/1801
        en Avenida Caseros CABA cuando la query pide Ciudad Autónoma.
        """
        if _house_number_match(parsed, cand) < 1.0:
            return False
        # Homónimo con altura exacta en otra ciudad no bloquea interpolar
        # la altura cercana en la localidad pedida (Caseros 1800 PBA vs 1799 CABA).
        return not place_conflict(parsed, cand)

    def _add_nearby(self, found: dict[int, Candidate], parsed: ParsedAddress,
                    bbox: tuple[float, float, float, float] | None) -> None:
        if not parsed.house_number:
            return
        if any(self._exact_house_usable(parsed, cand) for cand in found.values()):
            return
        for cand in self._nearby_houses(parsed, bbox):
            found.setdefault(cand.id, cand)

    def _candidates(self, parsed: ParsedAddress,
                    bbox: tuple[float, float, float, float] | None) -> list[Candidate]:
        """AND de la calle primero; OR solo si no aparecio esa calle.

        AND discrimina 'Fragata Sarmiento' de 'Sarmiento'. OR recupera cuando
        OSM no tiene todos los tokens (localidad pegada al road, Jr, etc.).
        Si AND ya trajo la calle, no se afloja: ahi el OR reintroduciria la
        homonima y el depot la elegiria. Grilla US / fecha nunca pasan a OR.
        """
        found: dict[int, Candidate] = {}
        self._add_fts(found, parsed, bbox, strict=True)
        self._add_nearby(found, parsed, bbox)
        self._add_interpolated(found, parsed, bbox)
        if parsed.road and not self._street_name_hit(parsed, found):
            self._add_fts(found, parsed, bbox, strict=False)
            self._add_nearby(found, parsed, bbox)
            self._add_interpolated(found, parsed, bbox)
        return list(found.values())

    def _street_name_variants(self, parsed: ParsedAddress) -> list[str]:
        """Prefijos locales + grafias OSM bilingues del mapa de alias."""
        out = self._prefix_street_variants(parsed)
        seen = {v.casefold() for v in out}
        for alias in self._alias_variants(parsed):
            if alias.casefold() not in seen:
                seen.add(alias.casefold())
                out.append(alias)
        return out

    def _known_streets(self, names: list[str]) -> list[str]:
        found: list[str] = []
        for name in names:
            row = self._conn.execute(
                "SELECT street FROM places WHERE street = ? LIMIT 1", (name,)
            ).fetchone()
            if row:
                found.append(row[0])
        return found

    def _fts_streets(self, query: str) -> list[str]:
        """Calles distintas que matchean FTS, no el top-N de bm25.

        bm25 de `"11" AND "septiembre"` se queda en '11 de Septiembre' (Ramos
        Mejia) y nunca ve '11 de Septiembre de 1888' (CABA).
        """
        if not query:
            return []
        sql = ("SELECT DISTINCT p.street FROM places_fts f"
               " JOIN places p ON p.id = f.rowid"
               " WHERE places_fts MATCH ? AND p.street IS NOT NULL"
               " LIMIT 30")
        return [row[0] for row in self._conn.execute(sql, (query,)) if row[0]]

    def _nearby_houses(self, parsed: ParsedAddress,
                       bbox: tuple[float, float, float, float] | None) -> list[Candidate]:
        """Alturas mas cercanas en calles que ya matchean el nombre.

        bm25 de `"corrientes"` devuelve 1-200; Corrientes 919 vive en 900-930.
        """
        want = _house_sort_int(parsed.house_number or "")
        words = self._search_words(parsed)
        if want is None or not words:
            return []
        streets: list[str] = []
        seen: set[str] = set()
        for name in self._known_streets(self._street_name_variants(parsed)):
            if name not in seen:
                seen.add(name)
                streets.append(name)
        # Si OSM ya tiene esa calle por nombre, interpolar AHI. No saltar a
        # 'Sarmiento' porque tiene un 1502 y 'Fragata Sarmiento' no tiene 1572.
        if streets:
            out: list[Candidate] = []
            for street in streets[:NEAR_HOUSE_STREETS]:
                out.extend(self._houses_on_street(
                    street, want, bbox, NEAR_HOUSE_PER_STREET,
                    exact=parsed.house_number))
            return out
        extras: list[tuple[int, str]] = []
        fts_queries = [self._street_fts(words, parsed=parsed, strict=True)]
        # Si AND no listo ninguna calle, OR (mismo filtro de score).
        fts_queries.append(self._street_fts(words, parsed=parsed, strict=False))
        for query in fts_queries:
            batch: list[tuple[int, str]] = []
            for name in self._fts_streets(query):
                if name in seen:
                    continue
                if _street_score(parsed, normalize_text(name),
                                 parsed.normalized,
                                 aliases=self._alias_norm_set(parsed)) < self.street_match_min:
                    continue
                hits = sum(1 for w in words if w.casefold() in name.casefold())
                batch.append((hits, name))
                seen.add(name)
            extras.extend(batch)
            if extras:
                break
        extras.sort(key=lambda item: item[0], reverse=True)
        for _, name in extras:
            if len(streets) >= NEAR_HOUSE_STREETS:
                break
            streets.append(name)
        out: list[Candidate] = []
        for street in streets[:NEAR_HOUSE_STREETS]:
            out.extend(self._houses_on_street(
                street, want, bbox, NEAR_HOUSE_PER_STREET,
                exact=parsed.house_number))
        return out

    def _houses_on_street(self, street: str, want: int,
                          bbox: tuple[float, float, float, float] | None,
                          limit: int, *, exact: str | None = None) -> list[Candidate]:
        found: list[Candidate] = []
        seen: set[int] = set()

        def _bbox_sql(sql: str, params: list) -> tuple[str, list]:
            if not bbox:
                return sql, params
            north, south, east, west = bbox
            sql += (" AND p.id IN (SELECT id FROM places_rtree WHERE"
                    " max_lat >= ? AND min_lat <= ? AND max_lon >= ? AND min_lon <= ?)")
            params += [south, north, west, east]
            return sql, params

        if exact and exact.strip():
            sql = (f"SELECT {SELECT_COLUMNS} FROM places p"
                   " WHERE p.street = ? AND lower(p.house_number) = lower(?)")
            sql, params = _bbox_sql(sql, [street, exact.strip()])
            for row in self._conn.execute(sql, params):
                cand = Candidate(*row)
                seen.add(cand.id)
                found.append(cand)
                if len(found) >= limit:
                    return found

        sql = (f"SELECT {SELECT_COLUMNS} FROM places p"
               " WHERE p.street = ? AND p.house_number IS NOT NULL")
        sql, params = _bbox_sql(sql, [street])
        sql += " ORDER BY ABS(CAST(p.house_number AS INTEGER) - ?) LIMIT ?"
        params += [want, limit]
        for row in self._conn.execute(sql, params):
            cand = Candidate(*row)
            if cand.id in seen:
                continue
            found.append(cand)
            if len(found) >= limit:
                break
        return found

    def _add_interpolated(self, found: dict[int, Candidate], parsed: ParsedAddress,
                          bbox: tuple[float, float, float, float] | None) -> None:
        if not parsed.house_number:
            return
        if any(self._exact_house_usable(parsed, cand) for cand in found.values()):
            return
        cand = self._interpolated_candidate(parsed, bbox)
        if cand is not None:
            found[cand.id] = cand

    def _interp_streets(self, parsed: ParsedAddress) -> list[str]:
        streets: list[str] = []
        seen: set[str] = set()
        for name in self._known_streets(self._street_name_variants(parsed)):
            if name not in seen:
                seen.add(name)
                streets.append(name)
        return streets[:NEAR_HOUSE_STREETS]

    def _houses_for_interp(
        self, street: str, want: int,
        bbox: tuple[float, float, float, float] | None,
    ) -> list[Candidate]:
        sql = (f"SELECT {SELECT_COLUMNS} FROM places p"
               " WHERE p.street = ? AND p.house_number IS NOT NULL"
               " AND CAST(p.house_number AS INTEGER) > 0"
               " AND ABS(CAST(p.house_number AS INTEGER) - ?) <= ?")
        params: list = [street, want, MAX_SPAN]
        if bbox:
            north, south, east, west = bbox
            sql += (" AND p.id IN (SELECT id FROM places_rtree WHERE"
                    " max_lat >= ? AND min_lat <= ? AND max_lon >= ? AND min_lon <= ?)")
            params += [south, north, west, east]
        sql += " LIMIT 80"
        return [Candidate(*row) for row in self._conn.execute(sql, params)]

    def _street_polyline(self, street: str) -> list[tuple[float, float]]:
        try:
            rows = self._conn.execute(
                "SELECT geom FROM places WHERE geom IS NOT NULL AND geom != ''"
                " AND (street = ? OR name = ?) LIMIT 8",
                (street, street),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        polys = [parse_polyline(raw) for (raw,) in rows if raw]
        polys = [p for p in polys if len(p) >= 2]
        if not polys:
            return []
        return max(polys, key=len)

    def _interpolated_candidate(
        self, parsed: ParsedAddress,
        bbox: tuple[float, float, float, float] | None,
    ) -> Candidate | None:
        want = _house_sort_int(parsed.house_number or "")
        if want is None:
            return None
        streets = self._interp_streets(parsed)
        if not streets:
            words = self._search_words(parsed)
            query = self._street_fts(words, parsed=parsed, strict=True) if words else ""
            for name in self._fts_streets(query) if query else []:
                if _street_score(parsed, normalize_text(name),
                                 parsed.normalized,
                                 aliases=self._alias_norm_set(parsed)) < self.street_match_min:
                    continue
                streets.append(name)
                if len(streets) >= NEAR_HOUSE_STREETS:
                    break
        for street in streets:
            rows = self._houses_for_interp(street, want, bbox)
            points: list[HousePoint] = []
            for row in rows:
                n = _house_sort_int(row.house_number or "")
                if n is None:
                    continue
                points.append(HousePoint(n, row.lat, row.lon))
            if len(points) < 2:
                continue
            poly = self._street_polyline(street)
            pt = interpolate_house(want, points, poly or None)
            if pt is None:
                continue
            template = rows[0]
            return Candidate(
                id=-abs(template.id) or -1,
                lat=pt[0], lon=pt[1],
                kind="interpolated",
                name=template.name,
                house_number=parsed.house_number,
                street=template.street,
                city=template.city,
                district=template.district,
                state=template.state,
                postcode=template.postcode,
                country=template.country,
                normalized_text=template.normalized_text,
            )
        return None

    def geocode(self, address: str, origin: tuple[float, float] | None = None,
                bbox: tuple[float, float, float, float] | None = None,
                parsed: ParsedAddress | None = None) -> GeocodeResult:
        parsed = parsed or parse(address)
        if not parsed.normalized:
            return GeocodeResult(status=STATUS_NOT_FOUND, source=self.name,
                                 detail={"reason": "direccion vacia"})
        try:
            candidates = self._candidates(parsed, bbox)
        except sqlite3.OperationalError as exc:
            return GeocodeResult(status=STATUS_ERROR, source=self.name,
                                 normalized_address=parsed.normalized,
                                 detail={"error": str(exc)})

        if not candidates:
            return GeocodeResult(status=STATUS_NOT_FOUND, source=self.name,
                                 normalized_address=parsed.normalized,
                                 detail={"candidates": 0})

        scored = [(score(parsed, c, origin, aliases=self._alias_norm_set(parsed)), c)
                  for c in candidates]
        compatible = [item for item in scored if not item[0][1].get("place_mismatch")]

        def _rank_key(sc: tuple[tuple[float, dict], Candidate]) -> tuple:
            br = sc[0][1]
            return (
                br.get("street", 0.0),
                br.get("locality", 0.0),
                br.get("house_number", 0.0),
                br.get("proximity", 0.0),
                sc[0][0],
            )

        # Entre CP/ciudad compatibles: localidad antes que altura exacta
        # (Caseros 1800 PBA vs 1799/1801 CABA). Si TODOS son otra ciudad, sin pin.
        if not compatible:
            (best_score, breakdown), best = max(scored, key=_rank_key)
            return GeocodeResult(
                status=STATUS_NOT_FOUND, source=self.name,
                confidence=best_score, normalized_address=parsed.normalized,
                detail={"candidates": len(candidates), "reason": "place_mismatch",
                        "best_score": best_score, "raw_score": best_score,
                        "breakdown": breakdown,
                        "osm": f"{best.kind or ''}:{best.id}"},
            )
        scored = compatible
        (best_score, breakdown), best = max(scored, key=_rank_key)

        # Si la consulta traia altura y el candidato no la resolvio, esto es un
        # match A NIVEL CALLE. Por mas alto que puntue no se puede declarar
        # `matched`: seria afirmar una precision que no tenemos y el operador lo
        # cargaria sin revisar. Techo duro en low_confidence.
        street_match = breakdown.get("street", 0.0)
        interpolated = best.kind == "interpolated"
        resolved_house = (
            breakdown.get("house_number", 0.0) >= 1.0 and not interpolated
        )
        # Pediste altura y OSM no la tiene, O no pediste altura: el candidato
        # puede ser un edificio con numero cualquiera. Eso no es puerta resuelta.
        street_only = not parsed.house_number or not resolved_house

        def descartar(reason: str) -> GeocodeResult:
            """Sin pin. Conserva el score REAL para diagnosticar en CSV/UI."""
            return GeocodeResult(status=STATUS_NOT_FOUND, source=self.name,
                                 confidence=best_score,
                                 normalized_address=parsed.normalized,
                                 detail={"candidates": len(candidates), "reason": reason,
                                         "best_score": best_score, "raw_score": best_score,
                                         "breakdown": breakdown,
                                         "osm": f"{best.kind or ''}:{best.id}"})

        def revisar(reason: str, *, precision: str) -> GeocodeResult:
            """Soft-reject: pin del mejor candidato + Review (score REAL)."""
            return GeocodeResult(
                status=STATUS_LOW, lat=best.lat, lon=best.lon,
                confidence=best_score, precision=precision, source=self.name,
                normalized_address=parsed.normalized,
                matched_text=best.normalized_text,
                detail={"candidates": len(candidates), "reason": reason,
                        "best_score": best_score, "raw_score": best_score,
                        "breakdown": breakdown, "soft_reject": True,
                        "osm": f"{best.kind or ''}:{best.id}"},
            )

        def soft_or_drop(reason: str, *, precision: str = "suspect") -> GeocodeResult:
            if self.soft_reject and best_score >= self.soft_reject_min:
                return revisar(reason, precision=precision)
            return descartar(reason)

        # La CALLE tiene que matchear. Coincidir solo en la altura es el falso
        # positivo mas caro que produce este indice: 'Av. Pueyrredon 359' resuelve
        # a 'Venezuela 359' y el pin queda a kilometros.
        if parsed.road and street_match < self.street_match_min:
            return soft_or_drop("la calle del candidato no coincide con la buscada",
                                precision="street_mismatch")

        # Altura 350 en '71st' no puede ser Valid: el operador no revisa un verde.
        if parsed.road and resolved_house and street_match < 1.0:
            return revisar("altura exacta pero la calle no es la pedida",
                           precision="street_mismatch")

        if street_only:
            # Nivel calle o altura interpolada: pin util, no puerta OSM.
            # Interpolado siempre Review (soft_reject): no afirmar verde.
            if best_score < self.street_level_floor:
                return soft_or_drop("match a nivel calle demasiado debil",
                                    precision="street_weak")
            reason = "interpolated_house" if interpolated else "street_level_match"
            return GeocodeResult(
                status=STATUS_LOW, lat=best.lat, lon=best.lon,
                confidence=best_score, precision="street", source=self.name,
                normalized_address=parsed.normalized,
                matched_text=best.normalized_text,
                detail={"candidates": len(candidates), "breakdown": breakdown,
                        "raw_score": best_score, "reason": reason,
                        "soft_reject": interpolated,
                        "osm": f"{best.kind or ''}:{best.id}"},
            )

        if best_score >= self.match_threshold and best_score >= self.valid_band:
            status = STATUS_MATCHED
        elif best_score >= self.low_threshold:
            status = STATUS_LOW
        else:
            return soft_or_drop("por debajo del umbral de confianza",
                                precision="below_threshold")

        return GeocodeResult(
            status=status, lat=best.lat, lon=best.lon, confidence=best_score,
            precision=best.precision, source=self.name,
            normalized_address=parsed.normalized,
            matched_text=best.normalized_text,
            detail={"candidates": len(candidates), "breakdown": breakdown,
                    "raw_score": best_score,
                    "osm": f"{best.kind or ''}:{best.id}"},
        )
