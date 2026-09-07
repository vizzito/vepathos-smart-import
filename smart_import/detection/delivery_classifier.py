"""¿Este segmento es una entrega?

Se pregunta ANTES de extraer, y es la respuesta a un error grave: convertir
"Salutos" en `address="Salutos"` y mandarlo al geocoder. Un texto solo es una
entrega si acumula evidencia logistica; sin evidencia se ignora, y `ignored` no
es lo mismo que `invalid` — el pie de un mensaje no es una entrega fallida.

Devuelve tambien las razones: sin ellas no se pueden ajustar las reglas despues.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..addresses.scoring import AddressCandidateScorer
from ..extraction.packages import has_package_signal
from ..extraction.phone_extract import find_phones
from ..extraction.time_window import find_time_expression
from ..resources import fold, label_set

DELIVERY = "delivery"
NEEDS_REVIEW = "needs_review"
IGNORE = "ignore"

#: pesos de cada señal
W_COORDINATES = 0.45
W_VALID_PHONE = 0.35
W_ADDRESS = 0.45           # se multiplica por el score de la direccion
W_EXPLICIT_LABEL = 0.20
W_TIME_WINDOW = 0.10
W_PACKAGES = 0.10

#: por debajo de esto no hay ni con que empezar
MIN_CHARS = 8
MIN_WORDS = 2


@dataclass(frozen=True)
class Verdict:
    classification: str
    score: float
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_delivery(self) -> bool:
        return self.classification in (DELIVERY, NEEDS_REVIEW)

    def as_dict(self) -> dict:
        return {"classification": self.classification, "score": round(self.score, 3),
                "reasons": list(self.reasons)}


class DeliveryCandidateClassifier:
    """Thresholds desde `Config`; señales desde las mismas librerias que extraen."""

    def __init__(self, config=None, context=None,
                 scorer: AddressCandidateScorer | None = None):
        self.config = config
        self.context = context
        locales = getattr(context, "locales", None)
        self.scorer = scorer or AddressCandidateScorer(locales)
        self.locales = locales
        self.accept = getattr(config, "delivery_accept_threshold", 0.55)
        self.review = getattr(config, "delivery_review_threshold", 0.30)

    def classify(self, text: str) -> Verdict:
        raw = (text or "").strip()
        if len(raw) < MIN_CHARS or len(raw.split()) < MIN_WORDS:
            return Verdict(IGNORE, 0.0, ("texto demasiado corto para ser una entrega",))
        if self._is_courtesy(raw):
            return Verdict(IGNORE, 0.0, ("saludo o despedida, no una entrega",))

        score = 0.0
        reasons: list[str] = []
        regions = getattr(self.context, "regions", ()) if self.context else ()

        phones = find_phones(raw, regions)
        if any(p.valid for p in phones):
            score += W_VALID_PHONE
            reasons.append("telefono valido")
        elif any(len(p.digits) >= 8 for p in phones):
            # posible pero no "valid" en libphonenumber (planes AR incompletos)
            score += W_VALID_PHONE * 0.55
            reasons.append("telefono posible (digitos suficientes)")

        address = self.scorer.score(raw)
        if address.score > 0:
            score += W_ADDRESS * address.score
            reasons.append(f"señal de direccion {address.score:.2f}: {address.evidence[0]}")
            # calle+altura clara sin tel (listas WhatsApp) → al menos needs_review
            if address.score >= 0.50:
                score += 0.06
                reasons.append("boost calle+altura")

        if self._has_label(raw):
            score += W_EXPLICIT_LABEL
            reasons.append("etiqueta explicita de entrega")

        if find_time_expression(raw):
            score += W_TIME_WINDOW
            reasons.append("restriccion horaria")

        if has_package_signal(raw):
            score += W_PACKAGES
            reasons.append("informacion de bultos/peso/medidas")

        score = min(score, 0.99)
        if score >= self.accept:
            return Verdict(DELIVERY, score, tuple(reasons))
        if score >= self.review:
            return Verdict(NEEDS_REVIEW, score,
                           (*reasons, "evidencia parcial: conviene revisar a mano"))
        return Verdict(IGNORE, score, tuple(reasons) or ("sin evidencia logistica",))

    # ---------- señales ----------

    def _is_courtesy(self, text: str) -> bool:
        """El segmento ENTERO es un saludo o una despedida."""
        words = [fold(w.strip(".,;:!¡¿?")) for w in text.split() if w.strip(".,;:!¡¿?")]
        if not words or len(words) > 4:
            return False
        courtesy = label_set("greetings", self.locales) | label_set("closings", self.locales)
        return all(w in courtesy or len(w) <= 2 for w in words)

    def _has_label(self, text: str) -> bool:
        from ..extraction.labels import find_labels
        return bool(find_labels(text, self.locales))
