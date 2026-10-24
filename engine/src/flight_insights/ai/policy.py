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
from collections.abc import Mapping
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
        self._removed = [re.compile(p) for p in rules["removed_parameters"]]

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
        held = rules["position_messages"].get(message)
        if held is not None:
            if field in held["allow"]:
                return Decision("allow", "cleared in a message that holds a position")
            return Decision("deny", held["reason"])
        if any(
            self._position(u, t)
            for u, t in ((f["units"], f["type"]) for f in definition["fields"].values())
        ):
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

    def value(self, decision: Decision, value: float) -> float:
        """A readable field's value as the AI path may see it."""
        if decision.verdict == "deny":
            raise ValueError("a denied field has no value on the AI path")
        if decision.verdict == "coarsen" and decision.step and math.isfinite(value):
            return round(value / decision.step) * decision.step
        return value

    def parameter_removed(self, name: str) -> bool:
        return any(p.search(name) for p in self._removed)

    def parameters(self, params: Mapping[str, float]) -> dict[str, float]:
        """The parameters with every location-revealing one removed."""
        return {n: v for n, v in params.items() if not self.parameter_removed(n)}
