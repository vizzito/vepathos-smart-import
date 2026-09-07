"""Geocoder local contra el indice SQLite construido desde un .osm.pbf."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .address import ParsedAddress, normalize_text, parse
from .base import (
    STATUS_ERROR, STATUS_LOW, STATUS_MATCHED, STATUS_NOT_FOUND, GeocodeResult,
)
from .scoring import (
    Candidate, _house_int, _house_number_match, _street_score, _strip_way_type,
    compass_key, ordinal_key, score,
)

CANDIDATE_LIMIT = 40
#: Si OSM no tiene la altura exacta, se piden mas filas de ESA calle (no el
#: top-N de bm25, que en avenidas largas son las alturas 1-200).
NEAR_HOUSE_STREETS = 12
NEAR_HOUSE_PER_STREET = 12
_STREET_PREFIXES = (
    "", "Avenida ", "Av. ", "Av ", "Calle ", "Paseo ", "Pasaje ",
    "Boulevard ", "Diagonal ",
)
_FTS_UNSAFE = re.compile(r"[^\w\s]", re.UNICODE)
#: NE/NW se expanden a 'noreste' en normalize_text; OSM US indexa 'northeast'.
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
}
_US_WAY_TITLES = (
    "Avenue", "Street", "Drive", "Boulevard", "Court", "Terrace",
    "Ave", "St", "Dr", "Blvd",
)

SELECT_COLUMNS = ("p.id, p.lat, p.lon, p.kind, p.name, p.house_number, p.street,"
                  " p.city, p.district, p.state, p.postcode, p.country, p.normalized_text")


class LocalOSMGeocoder:
    """Un indice = una region. Se abre una vez y se reutiliza para todo el archivo."""

    name = "osm"

    def __init__(self, index_path: str | Path, match_threshold: float = 0.90,
                 low_threshold: float = 0.70, street_level_floor: float = 0.60,
                 street_match_min: float = 0.80, review_band: float = 0.70,
                 valid_band: float = 0.80, soft_reject: bool = True,
                 soft_reject_min: float = 0.50):
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
        self._conn = sqlite3.connect(f"file:{self.index_path}?mode=ro", uri=True)
        self._conn.execute("PRAGMA query_only = ON")

    def close(self) -> None:
        self._conn.close()

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
        """
        from ..resources import fold, label_set, name_glue_words

        skip = label_set("street_tokens") | name_glue_words()
        if parsed.road:
            raw = normalize_text(parsed.road)
            words = []
            for token in raw.split():
                clean = _FTS_UNSAFE.sub("", token)
                if not clean or len(clean) <= 1:
                    continue
                # '11 de septiembre' tiene que buscar el 11; la altura de la
                # puerta no vive en road, no se cuela aca.
                if clean.isdigit():
                    words.append(clean)
                    continue
                if fold(clean) in skip:
                    continue
                words.append(clean)
            if words:
                expanded: list[str] = []
                seen: set[str] = set()
                for word in words:
                    key = word.casefold()
                    if key not in seen:
                        seen.add(key)
                        expanded.append(word)
                    for extra in _COMPASS_FTS.get(key, ()):
                        if extra not in seen:
                            seen.add(extra)
                            expanded.append(extra)
                    for extra in _ORDINAL_FTS.get(key, ()):
                        if extra not in seen:
                            seen.add(extra)
                            expanded.append(extra)
                return expanded[:12]
        words, _ = self._clean_tokens(parsed)
        return [w for w in words if fold(w) not in skip][:8]

    @staticmethod
    def _house_fts_variants(number: str) -> list[str]:
        raw = _FTS_UNSAFE.sub("", str(number))
        if not raw:
            return []
        variants = [raw]
        stripped = raw.lstrip("0")
        if stripped and stripped != raw:
            variants.append(stripped)
        return variants

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

    def _street_fts(self, words: list[str], *, strict: bool = True) -> str:
        """Nombre de calle para FTS.

        strict=True (default): AND de los tokens de nombre. 'fragata' AND
        'sarmiento' no mete la homonima corta. Grilla US y calles-fecha
        siempre AND, tambien en el fallback.
        strict=False: OR de esos tokens. Solo cuando AND no encontro calle.
        """
        if not words:
            return ""
        if any(w.isdigit() for w in words) and len(words) >= 2:
            return " AND ".join(f'"{w}"' for w in words)
        compass, ordinals, rest = [], [], []
        for word in words:
            if compass_key(word):
                compass.append(word)
            elif ordinal_key(word):
                ordinals.append(word)
            else:
                rest.append(word)
        if compass and ordinals:
            parts = [self._or_group(compass), self._or_group(ordinals)]
            if rest:
                parts.append(self._or_group(rest))
            return " AND ".join(parts)
        if len(rest) >= 2:
            joiner = " AND " if strict else " OR "
            return joiner.join(f'"{w}"' for w in rest)
        return " OR ".join(f'"{w}"' for w in words)

    def _precise_query(self, parsed: ParsedAddress, *, strict: bool = True) -> str | None:
        """Altura AND nombre de calle. Es la consulta que encuentra direcciones."""
        words = self._search_words(parsed)
        _, numbers = self._clean_tokens(parsed)
        number = parsed.house_number or (numbers[0] if numbers else None)
        if not number or not words:
            return None
        variants = self._house_fts_variants(number)
        if not variants:
            return None
        num = " OR ".join(f'"{n}"' for n in variants)
        if len(variants) > 1:
            num = f"({num})"
        else:
            num = f'"{variants[0]}"'
        calle = self._street_fts(words, strict=strict)
        if " AND " in calle or calle.startswith("("):
            return f"{num} AND {calle}"
        return f"{num} AND ({calle})"

    def _fts_query(self, parsed: ParsedAddress, *, strict: bool = True) -> str:
        """Consulta amplia: tokens de calle (sin país / avenida)."""
        words = self._search_words(parsed)
        return self._street_fts(words, strict=strict) if words else ""

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
            if _street_score(parsed, street, parsed.normalized) >= self.street_match_min:
                return True
        return False

    def _add_nearby(self, found: dict[int, Candidate], parsed: ParsedAddress,
                    bbox: tuple[float, float, float, float] | None) -> None:
        if not parsed.house_number:
            return
        has_exact = any(
            _house_number_match(parsed, cand) >= 1.0 for cand in found.values()
        )
        if has_exact:
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
        if parsed.road and not self._street_name_hit(parsed, found):
            self._add_fts(found, parsed, bbox, strict=False)
            self._add_nearby(found, parsed, bbox)
        return list(found.values())

    def _street_name_variants(self, parsed: ParsedAddress) -> list[str]:
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
        # OSM US: 'Northeast 1st Avenue', no 'Noreste 1st' ni 'Avenida NE'.
        compass = next((compass_key(w) for w in nombre.split() if compass_key(w)), None)
        ordinal = next((w for w in nombre.split() if ordinal_key(w)), None)
        if compass and ordinal:
            title = _US_COMPASS_TITLE[compass]
            short = compass.upper()
            for way in _US_WAY_TITLES:
                for head in (title, short):
                    cand = f"{head} {ordinal} {way}"
                    if cand not in seen:
                        seen.add(cand)
                        out.append(cand)
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
        want = _house_int(parsed.house_number)
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
                out.extend(self._houses_on_street(street, want, bbox,
                                                  NEAR_HOUSE_PER_STREET))
            return out
        extras: list[tuple[int, str]] = []
        fts_queries = [self._street_fts(words, strict=True)]
        # Si AND no listo ninguna calle, OR (mismo filtro de score).
        fts_queries.append(self._street_fts(words, strict=False))
        for query in fts_queries:
            batch: list[tuple[int, str]] = []
            for name in self._fts_streets(query):
                if name in seen:
                    continue
                if _street_score(parsed, normalize_text(name),
                                 parsed.normalized) < self.street_match_min:
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
            out.extend(self._houses_on_street(street, want, bbox,
                                              NEAR_HOUSE_PER_STREET))
        return out

    def _houses_on_street(self, street: str, want: int,
                          bbox: tuple[float, float, float, float] | None,
                          limit: int) -> list[Candidate]:
        sql = (f"SELECT {SELECT_COLUMNS} FROM places p"
               " WHERE p.street = ? AND p.house_number IS NOT NULL")
        params: list = [street]
        if bbox:
            north, south, east, west = bbox
            sql += (" AND p.id IN (SELECT id FROM places_rtree WHERE"
                    " max_lat >= ? AND min_lat <= ? AND max_lon >= ? AND min_lon <= ?)")
            params += [south, north, west, east]
        sql += " ORDER BY ABS(CAST(p.house_number AS INTEGER) - ?) LIMIT ?"
        params += [want, limit]
        return [Candidate(*row) for row in self._conn.execute(sql, params)]

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

        scored = [(score(parsed, c, origin), c) for c in candidates]
        # Se ajusta a la CALLE del pedido, no al depot. 350 NE 71st tenia
        # altura exacta y 0.80 de calle; 300 NE 1st tenia la calle correcta.
        # El depot (proximity) solo desempataba al final y perdia.
        (best_score, breakdown), best = max(
            scored,
            key=lambda sc: (
                sc[0][1].get("street", 0.0),
                sc[0][1].get("house_number", 0.0),
                sc[0][0],
            ),
        )

        # Si la consulta traia altura y el candidato no la resolvio, esto es un
        # match A NIVEL CALLE. Por mas alto que puntue no se puede declarar
        # `matched`: seria afirmar una precision que no tenemos y el operador lo
        # cargaria sin revisar. Techo duro en low_confidence.
        street_match = breakdown.get("street", 0.0)
        resolved_house = breakdown.get("house_number", 0.0) >= 1.0
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
            # Nivel calle: hay centroide util, pero sin altura OSM → siempre Review.
            # confidence = score textual REAL (no se reescala a la banda).
            if best_score < self.street_level_floor:
                return soft_or_drop("match a nivel calle demasiado debil",
                                    precision="street_weak")
            return GeocodeResult(
                status=STATUS_LOW, lat=best.lat, lon=best.lon,
                confidence=best_score, precision="street", source=self.name,
                normalized_address=parsed.normalized,
                matched_text=best.normalized_text,
                detail={"candidates": len(candidates), "breakdown": breakdown,
                        "raw_score": best_score, "reason": "street_level_match",
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
