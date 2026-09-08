"""Capa de localidad: de que ciudad habla el archivo que subio el usuario."""
from .scan import (
    ACCEPT_CONFIDENCE, SUGGEST_CONFIDENCE, LocalityCandidate, LocalityEvidence,
    detect_locality,
)

__all__ = [
    "ACCEPT_CONFIDENCE", "SUGGEST_CONFIDENCE",
    "LocalityCandidate", "LocalityEvidence", "detect_locality",
]
