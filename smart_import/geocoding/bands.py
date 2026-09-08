"""La banda de confianza se calcula UNA sola vez, y aca.

Contrato del producto (env):
  score >= GEOCODE_VALID_BAND   → valid (verde)   # default 0.80
  score >= GEOCODE_REVIEW_BAND  → review (ambar)  # default 0.70
  score <  GEOCODE_REVIEW_BAND  → sin pin (needs_geocoding)

`geocode_confidence` es el score textual REAL. El color sigue ese numero
cuando hay pin (already/manual siempre verdes).

Precision (`street`, `street_mismatch`, …) es diagnostico en la UI: NO redefine
la banda. Soft-reject (`force_review`) si mantiene el techo ambar.
"""
from __future__ import annotations

from .base import STATUS_ALREADY, STATUS_ERROR, STATUS_NOT_FOUND

#: verde: la coordenada se puede usar tal cual
BAND_VALID = "valid"
#: ambar: hay coordenada, pero conviene que un humano la mire
BAND_REVIEW = "review"
#: sin pin: hay que ubicarla a mano
BAND_NEEDS_GEOCODING = "needs_geocoding"

#: cortes por defecto; el despliegue los mueve por Config / .env
DEFAULT_VALID_AT = 0.80
DEFAULT_REVIEW_AT = 0.70

_HARD_MISS = frozenset({
    STATUS_NOT_FOUND, STATUS_ERROR, "a_geocodificar", "needs_geocode", "failed",
})
_TRUSTED = frozenset({STATUS_ALREADY, "manual"})


def band_for(status: str | None, confidence: float | None, *, has_coords: bool = True,
             valid_at: float = DEFAULT_VALID_AT,
             review_at: float = DEFAULT_REVIEW_AT,
             precision: str | None = None,
             force_review: bool = False) -> str:
    """Banda de una fila. Sin coordenadas → needs_geocoding.

    Con pin: already/manual → valid (coordenadas del archivo / operador).
    El resto usa el score contra `valid_at` / `review_at`
    (GEOCODE_VALID_BAND / GEOCODE_REVIEW_BAND).

    Soft-reject (`force_review`) puede bajar un verde a ambar. La precision OSM
    queda como diagnostico y no cambia el color (el % en la UI y la banda deben
    coincidir: 97% no puede ser ambar solo por ser `street`).
    """
    if not has_coords:
        return BAND_NEEDS_GEOCODING

    st = (status or "").strip().lower()
    if st in _HARD_MISS:
        return BAND_NEEDS_GEOCODING
    if st in _TRUSTED:
        return BAND_VALID

    value = float(confidence or 0.0)
    if value >= valid_at:
        banda = BAND_VALID
    elif value >= review_at:
        banda = BAND_REVIEW
    else:
        return BAND_NEEDS_GEOCODING

    # Soft-reject: pin de respaldo — no afirmar "usalo tal cual".
    if banda == BAND_VALID and force_review:
        return BAND_REVIEW
    return banda


#: precisiones aproximadas (diagnostico UI / logs; ya no redefinen la banda)
APPROXIMATE_PRECISIONS = frozenset({
    "street", "street_mismatch", "street_weak", "suspect", "below_threshold",
    "locality", "poi",
})


def _is_approximate(precision: str | None) -> bool:
    return (precision or "").strip().lower() in APPROXIMATE_PRECISIONS


def band_from_row(row: dict, *, valid_at: float = DEFAULT_VALID_AT,
                  review_at: float = DEFAULT_REVIEW_AT) -> str:
    """Deriva la banda desde status+score+coords (ignora geocode_band stale)."""
    try:
        lat, lng = float(row.get("lat") or ""), float(row.get("lng") or "")
        has_coords = -90 <= lat <= 90 and -180 <= lng <= 180
    except (TypeError, ValueError):
        has_coords = False
    try:
        confidence = float(row.get("geocode_confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return band_for(
        row.get("geocode_status"), confidence, has_coords=has_coords,
        valid_at=valid_at, review_at=review_at,
        precision=row.get("geocode_precision"),
    )


def percent(confidence: float | None) -> int | None:
    """El % que muestra la UI, redondeado igual en todos lados."""
    if confidence in (None, ""):
        return None
    return round(float(confidence) * 100)


def parse_band_env(raw: str | float | None, default: float) -> float:
    """Acepta 0.75 o 75 desde env."""
    if raw is None or raw == "":
        return default
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return default
    if n > 1.0 and n <= 100.0:
        return n / 100.0
    return n
