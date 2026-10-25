"""Parameter dialects: one canonical name for every parameter, whatever the firmware.

ArduPilot renames and rescales parameters between releases. Analyzers read
canonical names only: the names of the newest release in the knowledge pack.
Recommendations are written back in the vehicle's own dialect, so a vehicle
never receives a name its firmware does not have (spec: Data ingestion,
"Parameter dialects").
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from flight_insights.knowledge import Pack, Vehicle, VersionChoice, version_key

ParamClass = Literal["tuning", "calibration", "learned", "counter", "hardware", "ground station"]


@dataclass(frozen=True)
class Rename:
    vehicle: Vehicle
    to_version: str
    old: str
    new: str
    scale: float  # new value = old value x scale


@dataclass
class CanonicalParams:
    values: dict[str, float]  # canonical name -> value in canonical units
    unknown: list[str] = field(default_factory=list[str])  # not in this firmware's metadata


@dataclass(frozen=True)
class _Rule:
    cls: ParamClass
    names: tuple[str, ...]
    metadata: str | None
    when: Mapping[str, tuple[float, ...]]


class Dialect:
    """The parameters of one vehicle type at one firmware version."""

    def __init__(self, pack: Pack, vehicle: Vehicle, firmware: str) -> None:
        self.pack, self.vehicle, self.firmware = pack, vehicle, firmware
        self.choice: VersionChoice = pack.choose(firmware)
        self.canonical_version = pack.versions[-1]
        self.meta = pack.metadata(self.choice.version, vehicle)
        renames = [
            Rename(vehicle, r["to_version"], r["old"], r["new"], float(r["scale"]))
            for r in pack.json("params/renames.json")["renames"]
            if r["vehicle"] == vehicle
        ]
        # Renames that happened after this firmware, oldest first.
        self._later = sorted(
            (r for r in renames if version_key(r.to_version) > version_key(self.choice.version)),
            key=lambda r: version_key(r.to_version),
        )
        self._rules = [
            _Rule(
                r["class"],
                tuple(r.get("names", ["*"])),
                r.get("metadata"),
                {k: tuple(v) for k, v in r.get("when", {}).items()},
            )
            for r in pack.json("params/classes.json")["rules"]
        ]

    # --- names and values ---------------------------------------------------------

    def known(self, name: str) -> bool:
        return name in self.meta

    def canonical(self, name: str, value: float | None = None) -> tuple[str, float | None]:
        """This firmware's name (and value) -> the canonical name (and value)."""
        for r in self._later:
            if name == r.old:
                name = r.new
                value = None if value is None else value * r.scale
        return name, value

    def native(self, name: str, value: float | None = None) -> tuple[str, float | None]:
        """A canonical name (and value) -> this firmware's name (and value)."""
        for r in reversed(self._later):
            if name == r.new:
                name = r.old
                value = None if value is None else value / r.scale
        return name, value

    def gate(self, name: str) -> tuple[str, Literal["refresh", "reboot"]] | None:
        """For a canonical parameter, the canonical parameter that switches it into view
        in this firmware, and whether it then appears on a parameter "refresh" or only
        after a "reboot" (knowledge pack, read from the release's source)."""
        native, _ = self.native(name)
        entry = self.pack.gates(self.choice.version, self.vehicle).get(native)
        if entry is None:
            return None
        after: Literal["refresh", "reboot"] = "reboot" if entry["after"] == "reboot" else "refresh"
        return self.canonical(entry["gate"])[0], after

    def to_canonical(self, values: Mapping[str, float]) -> CanonicalParams:
        out = CanonicalParams({})
        for name, value in values.items():
            if not self.known(name):
                out.unknown.append(name)  # unknown, not wrong: custom builds add their own
            canonical, converted = self.canonical(name, value)
            out.values[canonical] = value if converted is None else converted
        return out

    def from_canonical(self, values: Mapping[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for name, value in values.items():
            native, converted = self.native(name, value)
            out[native] = value if converted is None else converted
        return out

    # --- classes --------------------------------------------------------------------

    def param_class(self, name: str, values: Mapping[str, float] | None = None) -> ParamClass:
        """How a parameter changes: only "tuning" changes are candidate causes.

        `values` (the rest of the parameter set) decides rules such as "learned
        while hover learning is on".
        """
        meta: dict[str, Any] = self.meta.get(name, {})
        for rule in self._rules:
            if not any(fnmatch.fnmatchcase(name, p) for p in rule.names):
                continue
            if rule.metadata and not meta.get(rule.metadata):
                continue
            if rule.when and not all(
                values is not None and values.get(k) in allowed for k, allowed in rule.when.items()
            ):
                continue
            return rule.cls
        return "tuning"
