"""The guided setups' steps and fields, from the knowledge pack
(knowledge/setup/platform.json and station.json), and checking values against them."""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from flight_insights.knowledge import Pack

TEXT_LIMIT = 500
FIELD_TYPES = ("text", "number", "integer", "boolean", "choice", "source")


class SetupError(ValueError):
    """A value does not fit its field, or a record does not exist."""


@dataclass(frozen=True)
class Field:
    id: str
    step: str
    type: str
    required: bool
    label: str  # English, for messages; the UI shows the schema's own languages
    prevents: str | None
    detect: str | None
    choices: tuple[str, ...]
    volatile: bool = False  # changes minute to minute; never a reason for a new version


@dataclass(frozen=True)
class Schema:
    version: int
    steps: tuple[str, ...]
    fields: dict[str, Field]
    raw: dict[str, Any]  # as the UI renders it
    repeating: frozenset[str] = frozenset()  # steps that hold a list of entries

    def field(self, field_id: str) -> Field:
        if field_id not in self.fields:
            raise SetupError(f"no setup field {field_id!r}")
        return self.fields[field_id]

    def in_step(self, step: str) -> list[Field]:
        return [f for f in self.fields.values() if f.step == step]


def load_schema(pack: Pack, name: str = "platform") -> Schema:
    return _load(pack.root, name)


@cache
def _load(root: Path, name: str) -> Schema:
    import json

    raw: dict[str, Any] = json.loads((root / "setup" / f"{name}.json").read_text(encoding="utf-8"))
    fields: dict[str, Field] = {}
    for step in raw["steps"]:
        for f in step["fields"]:
            if f["id"] in fields:
                raise SetupError(f"setup field {f['id']} is defined twice")
            fields[f["id"]] = Field(
                f["id"],
                step["id"],
                f["type"],
                bool(f.get("required")),
                f["label"]["en"],
                _english(f.get("prevents")),
                f.get("detect"),
                tuple(f.get("choices", ())),
                bool(f.get("volatile")),
            )
    steps = tuple(s["id"] for s in raw["steps"])
    repeating = frozenset(s["id"] for s in raw["steps"] if s.get("repeat"))
    return Schema(raw["version"], steps, fields, raw, repeating)


def _english(text: dict[str, str] | None) -> str | None:
    return text.get("en") if text else None


def check_value(field: Field, value: Any) -> Any:
    """The value as stored, or SetupError. None clears a field."""
    if value is None:
        return None
    match field.type:
        case "text" | "source":
            if not isinstance(value, str) or len(value) > TEXT_LIMIT:
                raise SetupError(f"{field.label}: text up to {TEXT_LIMIT} characters")
            return value.strip() or None
        case "number" | "integer":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SetupError(f"{field.label}: a number")
            if not math.isfinite(value) or (field.type == "integer" and value != int(value)):
                raise SetupError(f"{field.label}: a whole number" if field.type == "integer" else
                                 f"{field.label}: a finite number")  # fmt: skip
            return int(value) if field.type == "integer" else float(value)
        case "boolean":
            if not isinstance(value, bool):
                raise SetupError(f"{field.label}: yes or no")
            return value
        case "choice":
            if value not in field.choices:
                raise SetupError(f"{field.label}: one of {', '.join(field.choices)}")
            return value
        case _:
            raise SetupError(f"{field.label}: unknown field type {field.type}")
