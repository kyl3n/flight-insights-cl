"""What a build still lacks before its first flight, and what each gap prevents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flight_insights.platforms.schema import Schema


@dataclass(frozen=True)
class Gap:
    step: str
    field: str
    label: str
    prevents: str | None


@dataclass(frozen=True)
class Completeness:
    filled: int
    required: int
    gaps: tuple[Gap, ...]
    steps: dict[str, str]  # step -> "done", "started", "not started" or "optional"

    @property
    def percent(self) -> int:
        return 100 if not self.required else round(100 * self.filled / self.required)

    @property
    def ready(self) -> bool:
        """Ready for a first flight: every required answer given."""
        return not self.gaps


def completeness(schema: Schema, spec: dict[str, Any]) -> Completeness:
    gaps: list[Gap] = []
    filled = required = 0
    steps: dict[str, str] = {}
    for step in schema.steps:
        fields = schema.in_step(step)
        given = [f for f in fields if _value(spec, f.id) is not None]
        missing = [f for f in fields if f.required and f not in given]
        required += sum(f.required for f in fields)
        filled += sum(f.required for f in given)
        gaps += [Gap(step, f.id, f.label, f.prevents) for f in missing]
        if not fields:  # the review step: done once nothing before it is missing
            steps[step] = "done" if not gaps else "not started"
        elif missing:
            steps[step] = "started" if given else "not started"
        elif given or any(f.required for f in fields):
            steps[step] = "done"
        else:
            steps[step] = "optional"  # nothing required here, nothing entered yet
    return Completeness(filled, required, tuple(gaps), steps)


def _value(spec: dict[str, Any], field_id: str) -> Any:
    entry: dict[str, Any] = spec.get(field_id) or {}
    return entry.get("value")
