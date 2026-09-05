"""Geocoder local contra el indice SQLite construido desde un .osm.pbf."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .address import ParsedAddress, parse
from .base import (
    STATUS_ERROR, STATUS_LOW, STATUS_MATCHED, STATUS_NOT_FOUND, GeocodeResult,
)
from .scoring import Candidate, score

CANDIDATE_LIMIT = 40
_FTS_UNSAFE = re.compile(r"[^\w\s]", re.UNICODE)

SELECT_COLUMNS = ("p.id, p.lat, p.lon, p.kind, p.name, p.house_number, p.street,"
                  " p.city, p.district, p.state, p.postcode, p.country, p.normalized_text")


class LocalOSMGeocoder:
    """Un indice = una region. Se abre una vez y se reutiliza para todo el archivo."""

    name = "osm"

    def __init__(self, index_path: str | Path, match_threshold: float = 0.90,
                 low_threshold: float = 0.70):
        self.index_path = Path(index_path).expanduser()
        if not self.index_path.exists():
            raise FileNotFoundError(f"no existe el indice {self.index_path}. "
                                    f"Generalo con 'build-geocoder-index'.")
        self.match_threshold = match_threshold
        self.low_threshold = low_threshold
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

    def _precise_query(self, parsed: ParsedAddress) -> str | None:
        """Altura AND calle. Es la consulta que realmente encuentra direcciones.

        Sin la altura, `"florida" OR "buenos" OR "aires"` ordenado por bm25 devuelve
        40 POIs llamados "Florida" (paradas, comercios) y NINGUNA fila de la calle
        Florida: los documentos de direccion son mas largos y bm25 los castiga.
        La altura esta en normalized_text, asi que incluirla discrimina de una.
        """
        words, numbers = self._clean_tokens(parsed)
        number = parsed.house_number or (numbers[0] if numbers else None)
        if not number or not words:
            return None
        number = _FTS_UNSAFE.sub("", str(number))
        if not number:
            return None
        calle = " OR ".join(f'"{w}"' for w in words)
        return f'"{number}" AND ({calle})'

    def _fts_query(self, parsed: ParsedAddress) -> str:
        """Consulta amplia: tokens en OR. Se usa como red de contencion cuando la
        consulta precisa no aplica o no devuelve nada."""
        words, _ = self._clean_tokens(parsed)
        return " OR ".join(f'"{w}"' for w in words)

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

    def _candidates(self, parsed: ParsedAddress,
                    bbox: tuple[float, float, float, float] | None) -> list[Candidate]:
        """Dos pasadas: primero la precisa (altura AND calle), despues la amplia.

        Se juntan las dos porque la precisa da los aciertos exactos y la amplia
        cubre el caso en que OSM no tiene esa altura pero si la calle.
        """
        found: dict[int, Candidate] = {}

        if precise := self._precise_query(parsed):
            for cand in self._run(precise, bbox, CANDIDATE_LIMIT):
                found[cand.id] = cand

        broad = self._fts_query(parsed)
        if broad:
            for cand in self._run(broad, bbox, CANDIDATE_LIMIT):
                found.setdefault(cand.id, cand)

        return list(found.values())

    def geocode(self, address: str, origin: tuple[float, float] | None = None,
                bbox: tuple[float, float, float, float] | None = None) -> GeocodeResult:
        parsed = parse(address)
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
        (best_score, breakdown), best = max(scored, key=lambda sc: sc[0][0])

        # Si la consulta traia altura y el candidato no la resolvio, esto es un
        # match A NIVEL CALLE. Por mas alto que puntue no se puede declarar
        # `matched`: seria afirmar una precision que no tenemos y el operador lo
        # cargaria sin revisar. Techo duro en low_confidence.
        resolved_house = breakdown.get("house_number", 0.0) >= 1.0
        street_only = bool(parsed.house_number) and not resolved_house

        if best_score >= self.match_threshold and not street_only:
            status = STATUS_MATCHED
        elif best_score >= self.low_threshold:
            status = STATUS_LOW
        else:
            # nunca se devuelven coordenadas de un match debil
            return GeocodeResult(status=STATUS_NOT_FOUND, source=self.name,
                                 confidence=best_score,
                                 normalized_address=parsed.normalized,
                                 detail={"candidates": len(candidates),
                                         "best_score": best_score, "breakdown": breakdown})

        precision = "street" if street_only else best.precision

        return GeocodeResult(
            status=status, lat=best.lat, lon=best.lon, confidence=best_score,
            precision=precision, source=self.name,
            normalized_address=parsed.normalized,
            matched_text=best.normalized_text,
            detail={"candidates": len(candidates), "breakdown": breakdown,
                    "osm": f"{best.kind or ''}:{best.id}"},
        )
