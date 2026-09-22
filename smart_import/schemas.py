"""Carga del target schema desde JSON.

El schema NO esta hardcodeado: agregar un campo es editar el JSON, no el mapper.
"""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


def normalize_key(text: str) -> str:
    """'Dir. Entrega ' -> 'dir entrega'. Sin acentos, sin puntuacion, colapsa espacios."""
    if text is None:
        return ""
    s = str(text).strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    out = []
    for ch in s:
        out.append(ch if (ch.isalnum() or ch.isspace()) else " ")
    return " ".join("".join(out).split())


@dataclass(frozen=True)
class SchemaField:
    name: str
    type: str
    level: str              # delivery | package | timewindow
    aliases: tuple[str, ...] = ()
    range: tuple[float, float] | None = None
    derivable: bool = False
    input_only: bool = False

    @property
    def normalized_aliases(self) -> tuple[str, ...]:
        return tuple(normalize_key(a) for a in self.aliases)


@dataclass(frozen=True)
class TargetSchema:
    name: str
    version: int
    sheet_name: str
    group_by: str
    column_order: tuple[str, ...]
    fields: dict[str, SchemaField] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "TargetSchema":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        fields = {}
        for name, spec in raw["fields"].items():
            rng = spec.get("range")
            fields[name] = SchemaField(
                name=name,
                type=spec["type"],
                level=spec.get("level", "delivery"),
                aliases=tuple(spec.get("aliases", ())),
                range=(float(rng[0]), float(rng[1])) if rng else None,
                derivable=bool(spec.get("derivable", False)),
                input_only=bool(spec.get("input_only", False)),
            )
        return cls(
            name=raw["name"],
            version=int(raw.get("version", 1)),
            sheet_name=raw.get("sheet_name", "deliveries"),
            group_by=raw.get("group_by", "delivery_id"),
            column_order=tuple(raw.get("column_order", list(fields))),
            fields=fields,
        )

    def fields_at(self, level: str) -> list[SchemaField]:
        return [f for f in self.fields.values() if f.level == level]

    def alias_index(self) -> dict[str, str]:
        """alias normalizado -> nombre del campo. El nombre del campo tambien es alias."""
        idx: dict[str, str] = {}
        for f in self.fields.values():
            for alias in (normalize_key(f.name), *f.normalized_aliases):
                idx.setdefault(alias, f.name)
        return idx


class SchemaNotFound(LookupError):
    """El schema pedido no existe en el directorio de schemas.

    Es un error de dominio, no de transporte: la API lo traduce a 404 y el worker
    a una falla permanente. Sin esto, resolver un schema obligaria a importar
    fastapi desde el worker solo para levantar una HTTPException.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"schema '{name}' inexistente")
        self.name = name


def resolve_schema_dir(schema_dir: str | Path) -> Path:
    """El directorio efectivo de schemas.

    En el container vive en /app/schemas, en desarrollo en ./schemas, y un
    cliente puede montar los suyos. Una ruta relativa que no existe desde el cwd
    se resuelve contra la raiz del repo, que es de donde salen los del paquete.
    """
    path = Path(schema_dir)
    if not path.is_absolute() and not path.exists():
        path = Path(__file__).resolve().parent.parent / schema_dir
    return path


def resolve_schema_path(schema_dir: str | Path, name: str) -> Path:
    """Ruta del JSON de un schema por nombre. `SchemaNotFound` si no esta."""
    path = resolve_schema_dir(schema_dir) / f"{name}.json"
    if not path.exists():
        raise SchemaNotFound(name)
    return path
