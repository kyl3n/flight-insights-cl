"""Which log fields and parameters the AI path may read (spec: AI layer, "Location
scrubber: deny by default").

Every field an AI packet or AI tool reads goes through `Policy.field`, which reads the
rules in knowledge/rules/scrubber.json against the release's own message definitions
(knowledge/logs) and the log's own units. Deny by default: a message or field the
release does not define, or one the rules do not clear, is not read. Parameters go
through `Policy.parameters`, which drops every location-revealing one.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from flight_insights.knowledge import Pack, version_key

Verdict = Literal["allow", "coarsen", "deny"]


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    step: float | None = None  # for "coarsen": round to this step, in the field's unit

    @property
    def readable(self) -> bool:
        return self.verdict != "deny"


class Policy:
    """The scrubber rules for one release."""

    def __init__(self, rules: Mapping[str, Any], messages: Mapping[str, Mapping[str, Any]]) -> None:
        self.rules = rules
        self.messages = messages
        self._patterns = [re.compile(p) for p in rules["location_field_patterns"]]
        self._named = re.compile(rules["position_names"])
        self._unitless: Mapping[str, Mapping[str, str]] = rules["unitless_positions"]
        self._removed = [re.compile(p) for p in rules["removed_parameters"]]
        self._derived: Mapping[str, float] = rules["position_derived"]

    @staticmethod
    def load(pack: Pack, firmware: str) -> Policy:
        """The rules read against the release's definitions, or the nearest earlier
        release's when the pack does not have this one."""
        versions = sorted(pack.manifest.get("logs", {}), key=version_key)
        earlier = [v for v in versions if version_key(v) <= version_key(firmware)]
        version = earlier[-1] if earlier else versions[0]
        return Policy(pack.json("rules/scrubber.json"), pack.log_messages(version))

    def _position(self, units: str, type_: str) -> bool:
        return units in self.rules["position_units"] or any(
            t in type_ for t in self.rules["position_types"]
        )

    def _holds(self, message: str, field: str, spec: Mapping[str, str]) -> bool:
        """A position by its units, its type, its name, or the rules' list of fields that
        hold one with neither."""
        return (
            self._position(spec["units"], spec["type"])
            or bool(self._named.match(field))
            or field in self._unitless.get(message, {})
        )

    def field(
        self, message: str, field: str, units: str | None = None, is_position: bool = False
    ) -> Decision:
        """Whether the AI path may read one field. `units` and `is_position` are what the
        log itself says (FMTU, the decoder's position tag); either can only deny."""
        rules = self.rules
        if is_position:
            return Decision("deny", "the log marks this field as a position")
        if message in rules["blocked_messages"]:
            return Decision("deny", rules["blocked_messages"][message])
        definition = self.messages.get(message)
        if definition is None:
            return Decision("deny", "not a message this release defines")
        spec: Mapping[str, str] | None = definition["fields"].get(field)
        if spec is None:
            return Decision("deny", "not a field this release defines")
        for u, t in ((spec["units"], spec["type"]), (units or "", "")):
            if self._position(u, t):
                return Decision("deny", "a latitude or longitude")
        if self._named.match(field):
            return Decision("deny", "a latitude or longitude, by its name")
        if field in self._unitless.get(message, {}):
            return Decision("deny", self._unitless[message][field])
        held = rules["position_messages"].get(message)
        if held is not None:
            if field in held["allow"]:
                return Decision("allow", "cleared in a message that holds a position")
            return Decision("deny", held["reason"])
        if any(self._holds(message, name, f) for name, f in definition["fields"].items()):
            return Decision("deny", "the message holds a position and is not classified")
        if field in rules["location_fields"].get(message, {}):
            return Decision("deny", rules["location_fields"][message][field])
        if any(p.search(field) for p in self._patterns):
            return Decision("deny", "a location-revealing field")
        if spec["units"] in rules["location_units"] or (units or "") in rules["location_units"]:
            return Decision("deny", "the magnetic field varies with place")
        step = rules["coarsen"].get(message, {}).get(field)
        if step is not None:
            return Decision("coarsen", "ambient pressure or temperature, rounded", float(step))
        return Decision("allow", "not location-revealing")

    def defines(self, message: str, field: str) -> bool:
        definition = self.messages.get(message)
        return definition is not None and field in definition["fields"]

    def evidence_clear(self, kind: str, messages: Sequence[str], fields: Sequence[str]) -> bool:
        """Whether what one piece of analyzer evidence read may reach the AI path whole.
        Parameter evidence names parameters, not log fields: none may be a removed one.
        Otherwise each field must be defined by a message it cites, and allowed in every
        one that defines it; a coarsened field is not clear, since which values come from
        it is not known. Evidence that names no fields is judged by its messages: every
        field of each must be allowed. Deny by default: anything unknown is not clear."""
        if kind == "parameter":
            return not any(self.parameter_removed(f) for f in fields)
        if fields:
            for f in fields:
                verdicts = [self.field(m, f).verdict for m in messages if self.defines(m, f)]
                if not verdicts or any(v != "allow" for v in verdicts):
                    return False
            return True
        for m in messages:
            definition = self.messages.get(m)
            if definition is None or any(
                self.field(m, f).verdict != "allow" for f in definition["fields"]
            ):
                return False
        return True

    def value(self, decision: Decision, value: float) -> float:
        """A readable field's value as the AI path may see it."""
        if decision.verdict == "deny":
            raise ValueError("a denied field has no value on the AI path")
        if decision.verdict == "coarsen" and decision.step and math.isfinite(value):
            return round(value / decision.step) * decision.step
        return value

    def derived(self, values: Mapping[str, Any]) -> dict[str, float | int]:
        """From evidence the policy withholds, the distances worked out from positions on
        this laptop that may still reach the AI path: single numbers on the allowlist,
        rounded to their step (whole metres). A list never passes: a series of distances
        traces the path."""
        out: dict[str, float | int] = {}
        for key, step in self._derived.items():
            v = values.get(key)
            if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
                continue
            rounded = round(float(v) / step) * step
            out[key] = int(rounded) if float(step).is_integer() else round(rounded, 6)
        return out

    def parameter_removed(self, name: str) -> bool:
        return any(p.search(name) for p in self._removed)

    def parameters(self, params: Mapping[str, float]) -> dict[str, float]:
        """The parameters with every location-revealing one removed."""
        return {n: v for n, v in params.items() if not self.parameter_removed(n)}
