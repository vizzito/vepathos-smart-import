"""Extracción previa estructura-primero (antes de IA / geocode).

Pipeline (CPU barato, sin hardcode de un solo idioma):

  1. Normalizar Unicode (NFKC) y limpiar comillas.
  2. Detectar teléfonos con ``phonenumbers`` (región ISO del job).
  3. Estrategias estructurales (idioma-agnósticas): flecha, paréntesis, KV, guiones.
  4. Estrategias léxicas (datos en ``lexicon.py``): deliver-to, etiquetas.
  5. Gate de confianza: address creíble + (name|phone válido) → OK; si no → None.

No llama al geocoder. Si devuelve dict, el caller geocodifica la calle limpia;
si None, el campo queda vacio y la fila se marca para revision.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from collections.abc import Callable

from .lexicon import CompiledLexicon, fold, get_lexicon

DEFAULT_FIELDS = (
    "delivery_id", "customer_name", "address", "phone",
    "reference", "tw_start", "tw_end", "tw_timezone",
)

_ARROW = re.compile(r"\s*(?:→|->|=>|⇒)\s*")
_DASH_SPLIT = re.compile(r"\s*[-–—]\s*")
_KV_SPLIT = re.compile(r"\s*[|/]\s*")
# 1) / 12. / 3- / 4:  → el número es un delivery_id natural en pastes de despacho
_LIST_PREFIX = re.compile(r"^\s*(?P<id>\d+)[\)\.\-:]\s+")
_ANGLE_PHONE = re.compile(r"<\s*([^>]+?)\s*>")
_TRAILING_PAREN = re.compile(r"^(?P<head>.+?)\s*\(\s*(?P<inner>[^)]+?)\s*\)\s*$")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_DIGIT = re.compile(r"\d")
_PHONE_SHAPE = re.compile(r"\+?\d[\d\s\-().]{5,}\d")


@dataclass(frozen=True, slots=True)
class HeuristicHit:
    values: dict[str, str]
    strategy: str
    confidence: float


def _clean(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", " ", text).strip(" \t\r\n,;.|\"'")


def _normalize_input(text: str) -> str:
    return _clean((text or "").strip().strip('"'))


def _pop_list_id(text: str) -> tuple[str | None, str]:
    """Si la línea empieza con '1) ' / '2. ', devuelve (id, resto)."""
    match = _LIST_PREFIX.match(text or "")
    if not match:
        return None, text
    return match.group("id"), text[match.end():]


def _looks_like_name(name: str) -> bool:
    name = _clean(name)
    if not (2 <= len(name) <= 80):
        return False
    if not _LETTER.search(name):
        return False
    if len(re.findall(r"\d", name)) >= 3:
        return False
    return True


def _looks_like_address(addr: str) -> bool:
    addr = _clean(addr)
    # Límite alto a propósito: barrio + ciudad + país ayudan al geocoder
    if not (5 <= len(addr) <= 400):
        return False
    if not _LETTER.search(addr):
        return False
    if _DIGIT.search(addr):
        return True
    return len(addr) >= 12


def _phone_digits(phone: str) -> int:
    return len(re.sub(r"\D", "", phone))


def find_phone_candidates(text: str, region: str | None = None) -> list[str]:
    """Candidatos vía phonenumbers (+ spans <…> y forma libre)."""
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        cleaned = _clean(raw)
        key = re.sub(r"\D", "", cleaned)
        if len(key) < 6 or key in seen:
            return
        seen.add(key)
        found.append(cleaned)

    for match in _ANGLE_PHONE.finditer(text):
        add(match.group(1))

    try:
        import phonenumbers
    except ImportError:
        for match in _PHONE_SHAPE.finditer(text):
            add(match.group(0))
        return found

    regions: list[str] = []
    if region:
        regions.append(region.upper())
    for extra in ("AR", "BR", "US", "MX", "ES", "PT", "GB"):
        if extra not in regions:
            regions.append(extra)

    for reg in regions:
        try:
            for match in phonenumbers.PhoneNumberMatcher(text, reg):
                add(text[match.start:match.end])
        except Exception:
            continue
        if found:
            break

    if not found:
        for match in _PHONE_SHAPE.finditer(text):
            add(match.group(0))
    return found


def _strip_leading_phone_label(fragment: str, lexicon: CompiledLexicon) -> str:
    folded = fold(fragment).lstrip()
    for label in sorted(lexicon.phone_labels, key=len, reverse=True):
        if folded.startswith(label):
            # Quitar N chars approx: buscar label case-insensitive al inicio
            m = re.match(
                rf"^\s*{re.escape(label)}\s*[:.]?\s*",
                fragment,
                flags=re.IGNORECASE,
            )
            if m:
                return _clean(fragment[m.end():])
            # fold length may differ from original; regex on folded words
            m2 = re.match(r"^\s*\S+\s*[:.]?\s*", fragment)
            if m2:
                return _clean(fragment[m2.end():])
    return _clean(fragment)


def _pick_phone(text: str, region: str | None, lexicon: CompiledLexicon) -> str | None:
    for cand in find_phone_candidates(text, region):
        return cand
    # Etiqueta del léxico + cola numérica
    folded = fold(text)
    for label in sorted(lexicon.phone_labels, key=len, reverse=True):
        idx = folded.find(label)
        if idx < 0:
            continue
        # Aprox: buscar forma de teléfono después del label en el original
        m = _PHONE_SHAPE.search(text)
        if m and _phone_digits(m.group(0)) >= 6:
            return _clean(m.group(0))
    return None


def _confidence(name: str, address: str, phone: str) -> float:
    score = 0.0
    if _looks_like_address(address):
        score += 0.45
        if _DIGIT.search(address):
            score += 0.10
    if phone and 6 <= _phone_digits(phone) <= 15:
        score += 0.25
    if name and _looks_like_name(name):
        score += 0.20
    return min(score, 1.0)


def _accept(name: str, address: str, phone: str, strategy: str,
            min_confidence: float = 0.70) -> HeuristicHit | None:
    name, address, phone = _clean(name), _clean(address), _clean(phone)
    if not _looks_like_address(address):
        return None
    if name and not _looks_like_name(name):
        name = ""
    if phone and not (6 <= _phone_digits(phone) <= 15):
        phone = ""
    if not (name or phone):
        return None
    conf = _confidence(name, address, phone)
    if conf < min_confidence:
        return None
    values = {k: v for k, v in (
        ("customer_name", name), ("address", address), ("phone", phone),
    ) if v}
    return HeuristicHit(values=values, strategy=strategy, confidence=conf)


def _strategy_arrow(text: str, region: str | None,
                    lexicon: CompiledLexicon) -> HeuristicHit | None:
    if not _ARROW.search(text):
        return None
    left, right = _ARROW.split(text, maxsplit=1)
    # El id de lista se saca en try_heuristic; acá solo limpiamos si quedó.
    _, left = _pop_list_id(left)
    phone = ""
    name = left
    angle = _ANGLE_PHONE.search(left)
    if angle:
        phone = angle.group(1)
        name = _ANGLE_PHONE.sub(" ", left)
    else:
        phone = _pick_phone(left, region, lexicon) or ""
        if phone:
            name = left.replace(phone, " ")
    # Notas operativas después de "|" (TW, portero) no van a address;
    # TODO lo geográfico a la izquierda del "|" se conserva intacto (barrio/CABA).
    address = right.split("|", 1)[0] if "|" in right else right
    return _accept(name, address, phone, "arrow")


def _strategy_trailing_paren(text: str, region: str | None,
                             lexicon: CompiledLexicon) -> HeuristicHit | None:
    match = _TRAILING_PAREN.match(text)
    if not match:
        return None
    inner = match.group("inner")
    phone = _pick_phone(inner, region, lexicon) or ""
    name = inner.replace(phone, " ") if phone else inner
    name = _strip_leading_phone_label(name, lexicon).strip(" ,;|-")
    # "Elena Vargas, cel" → si quedó label suelta al final
    for label in lexicon.phone_labels:
        name = re.sub(rf",?\s*{re.escape(label)}\s*$", "", name, flags=re.I)
    return _accept(name, match.group("head"), phone, "paren")


def _strategy_kv(text: str, region: str | None,
                 lexicon: CompiledLexicon) -> HeuristicHit | None:
    if ":" not in text:
        return None
    parts = _KV_SPLIT.split(text) if _KV_SPLIT.search(text) else [text]
    if len(parts) == 1 and parts[0].count(":") >= 2:
        parts = [
            t for t in re.split(
                r"(?=\b[\wÁÉÍÓÚÜÑáéíóúüñ][\wÁÉÍÓÚÜÑáéíóúüñ \-]{0,24}:)",
                parts[0],
            )
            if t.strip()
        ]
    bucket: dict[str, str] = {}
    for part in parts:
        if ":" not in part:
            continue
        label, value = part.split(":", 1)
        field = lexicon.classify_label(label)
        if field and value.strip():
            bucket[field] = _clean(value)
    if "address" not in bucket:
        return None
    phone = bucket.get("phone", "")
    if not phone:
        phone = _pick_phone(text, region, lexicon) or ""
    return _accept(
        bucket.get("customer_name", ""),
        bucket["address"],
        phone,
        "labeled_kv",
    )


def _strategy_dashes(text: str, region: str | None,
                     lexicon: CompiledLexicon) -> HeuristicHit | None:
    parts = [p for p in _DASH_SPLIT.split(text) if p.strip()]
    if len(parts) < 3:
        return None

    phone = ""
    phone_idx = -1
    for i, part in enumerate(parts):
        folded = fold(part)
        labeled = any(
            folded == lbl or folded.startswith(lbl + " ") or folded.startswith(lbl + ":")
            for lbl in lexicon.phone_labels
        )
        cand = _pick_phone(_strip_leading_phone_label(part, lexicon), region, lexicon)
        if not cand:
            cand = _pick_phone(part, region, lexicon)
        if cand and (labeled or _phone_digits(cand) >= 8 or i == len(parts) - 1):
            phone, phone_idx = cand, i
            if labeled or i == len(parts) - 1:
                break

    if phone_idx < 0:
        return None

    rest = [p for i, p in enumerate(parts) if i != phone_idx]
    if len(rest) < 2:
        return None

    def addr_score(p: str) -> int:
        return (3 if _DIGIT.search(p) else 0) + (1 if len(p) > 12 else 0)

    def name_score(p: str) -> int:
        return (3 if not _DIGIT.search(p) else 0) + (1 if " " in p.strip() else 0)

    address = max(rest, key=addr_score)
    name = max((p for p in rest if p != address), key=name_score, default="")
    return _accept(name, address, phone, "dashes")


def _strategy_deliver(text: str, region: str | None,
                      lexicon: CompiledLexicon) -> HeuristicHit | None:
    folded = fold(text)
    matched_prefix = next(
        (p for p in lexicon.deliver_prefixes if folded.startswith(p)),
        None,
    )
    if not matched_prefix:
        return None

    words_needed = len(matched_prefix.split())
    bits = text.split()
    if len(bits) <= words_needed:
        return None
    rest = " ".join(bits[words_needed:])

    phone = _pick_phone(rest, region, lexicon) or ""
    body = rest.replace(phone, " ") if phone else rest
    # Cortar cola "Telefono:/Celular:/Phone:" sin partir "Av. Paulista" en el punto
    label_alt = "|".join(
        re.escape(lbl) for lbl in sorted(lexicon.phone_labels, key=len, reverse=True)
    )
    # Fronteras de palabra: evita que "tel"/"cel" partan "Hotel"/"Broadway"
    body = re.split(
        rf"(?:^|[\s,;.])(?:{label_alt})(?![A-Za-z0-9])\s*[:.]?\s*",
        body,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]

    linker_alt = "|".join(
        re.escape(x) for x in sorted(lexicon.deliver_linkers, key=len, reverse=True)
    )
    m = re.search(
        rf"^(?P<name>.+?)\s+(?:{linker_alt})\s+(?P<address>.+)$",
        _clean(body),
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    return _accept(m.group("name"), m.group("address"), phone, "deliver")


Strategy = Callable[[str, str | None, CompiledLexicon], HeuristicHit | None]

_STRATEGIES: tuple[Strategy, ...] = (
    _strategy_arrow,
    _strategy_trailing_paren,
    _strategy_kv,
    _strategy_dashes,
    _strategy_deliver,
)


def try_heuristic(
    text: str,
    fields: tuple[str, ...] = DEFAULT_FIELDS,
    *,
    region: str | None = None,
    locales: tuple[str, ...] | None = None,
    min_confidence: float = 0.70,
    service_date=None,
    timezone: str | None = None,
) -> dict[str, str] | None:
    """Extrae campos locales o ``None`` → el caller puede caer a IA.

    ``region``: ISO 3166-1 alpha-2 para phonenumbers (``AR``, ``BR``, ``US``).
    ``locales``: packs léxicos (``es``, ``en``, ``pt``). ``None`` = todos.
    También deriva ``tw_*`` / ``reference`` de frases tipo "antes de las 14hs".

    Conserva barrio/ciudad/CABA dentro de ``address`` y *amplía* con provincia/país
    si faltan (señal para el geocoder, no estética). El prefijo ``1)`` → delivery_id.
    """
    from datetime import date as date_cls
    from .geo_address import maximize_address_for_geocode
    from .time_windows import enrich_with_tw_and_reference

    original = (text or "").strip().strip('"')
    raw = _normalize_input(text)
    if len(raw) < 8:
        return None

    list_id, raw = _pop_list_id(raw)

    lexicon = get_lexicon(locales)
    best: HeuristicHit | None = None

    for strategy in _STRATEGIES:
        hit = strategy(raw, region, lexicon)
        if hit is None or hit.confidence < min_confidence:
            continue
        if best is None or hit.confidence > best.confidence:
            best = hit
        if hit.confidence >= 0.90:
            break

    if best is None:
        return None

    values = dict(best.values)
    if list_id and "delivery_id" not in values:
        values["delivery_id"] = list_id

    if values.get("address"):
        values["address"] = maximize_address_for_geocode(
            values["address"], phone_region=region,
        )

    enriched = enrich_with_tw_and_reference(
        original or raw,
        values,
        service_date=service_date if isinstance(service_date, date_cls) else None,
        timezone=timezone,
        fields=fields,
    )
    return {k: v for k, v in enriched.items() if k in fields and v}


def try_heuristic_debug(
    text: str,
    *,
    region: str | None = None,
    locales: tuple[str, ...] | None = None,
) -> HeuristicHit | None:
    """Como ``try_heuristic`` pero con strategy/confidence (tests)."""
    raw = _normalize_input(text)
    if len(raw) < 8:
        return None
    lexicon = get_lexicon(locales)
    best: HeuristicHit | None = None
    for strategy in _STRATEGIES:
        hit = strategy(raw, region, lexicon)
        if hit and (best is None or hit.confidence > best.confidence):
            best = hit
    return best
