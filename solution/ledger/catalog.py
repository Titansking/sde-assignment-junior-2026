"""Loading of workflow lead schemas and the disposition catalogue."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalize_header

SUPPORTED_TYPES = {"string", "phone", "date", "decimal", "enum"}


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    default: str | None = None


@dataclass
class WorkflowSchema:
    workflow_id: str
    fields: list[Field]
    header_lookup: dict[str, str] = field(default_factory=dict)  # normalised header -> field name

    def __post_init__(self) -> None:
        phones = [f for f in self.fields if f.type == "phone"]
        refs = [f for f in self.fields if f.reference]
        if len(phones) != 1:
            raise ValueError(f"{self.workflow_id}: schema needs exactly one phone field")
        if len(refs) != 1:
            raise ValueError(f"{self.workflow_id}: schema needs exactly one reference field")
        for f in self.fields:
            if f.type not in SUPPORTED_TYPES:
                raise ValueError(f"{self.workflow_id}.{f.name}: unsupported type {f.type!r}")
            for key in (f.name, *f.aliases):
                self.header_lookup.setdefault(normalize_header(key), f.name)

    @property
    def phone_field(self) -> Field:
        return next(f for f in self.fields if f.type == "phone")

    @property
    def reference_field(self) -> Field:
        return next(f for f in self.fields if f.reference)


@dataclass(frozen=True)
class Disposition:
    code: str
    is_terminal: bool
    is_success: bool


Catalog = dict[str, dict[str, Disposition]]  # workflow_id -> code -> Disposition


def load_schemas(schema_dir: Path) -> dict[str, WorkflowSchema]:
    schemas: dict[str, WorkflowSchema] = {}
    for path in sorted(schema_dir.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        fields = [
            Field(
                name=f["name"],
                type=f["type"],
                required=bool(f.get("required", False)),
                pii=bool(f.get("pii", False)),
                reference=bool(f.get("reference", False)),
                aliases=tuple(f.get("aliases", [])),
                values=tuple(f.get("values", [])),
                default=f.get("default"),
            )
            for f in doc["fields"]
        ]
        schemas[doc["workflow_id"]] = WorkflowSchema(doc["workflow_id"], fields)
    return schemas


def _bool(s: str) -> bool:
    return s.strip().lower() == "true"


def load_catalog(schema_dir: Path) -> Catalog:
    catalog: Catalog = {}
    with open(schema_dir / "dispositions.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            catalog.setdefault(row["workflow_id"].strip(), {})[row["code"].strip()] = Disposition(
                code=row["code"].strip(),
                is_terminal=_bool(row["is_terminal"]),
                is_success=_bool(row["is_success"]),
            )
    return catalog
