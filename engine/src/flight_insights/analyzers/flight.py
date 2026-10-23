"""One flight as analyzers see it: its cached columns, parameters, fingerprint and log
checks.

Columns come from the encrypted cache, where position columns stay under their own
key: an analyzer cannot read a position even by asking for it. Everything is full
rate; nothing here decimates.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from flight_insights.analyzers.model import Segment
from flight_insights.history.persist import load_snapshot
from flight_insights.knowledge import Pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store


def build_answers(store: Store, build_id: str | None) -> dict[str, Any]:
    """The answers the build holds: its platform's reference answers, then the
    airframe's own changes on top (spec: Platforms, airframes and build configuration)."""
    if build_id is None:
        return {}
    build = store.get("build_version", build_id)
    if build is None:
        return {}
    spec: dict[str, Any] = json.loads(build["spec_json"])
    if build["airframe_id"]:
        airframe = store.get("airframe", build["airframe_id"])
        platform = store.get("platform", airframe["platform_id"]) if airframe else None
        reference = store.get("build_version", platform["reference_build_id"]) if platform else None
        if reference is not None:
            spec = json.loads(reference["spec_json"]) | spec
    return {k: v["value"] for k, v in spec.items() if isinstance(v, dict) and "value" in v}


class FlightError(RuntimeError):
    """The flight cannot be analysed: unknown, or imported before analysis existed."""


@dataclass
class FlightData:
    id: str
    key: str
    airframe_id: str | None
    firmware: str | None
    arm_us: int
    disarm_us: int
    params: Mapping[str, float]  # canonical names, as the flight started
    fingerprint: Mapping[str, Any]
    cache: ColumnCache | None  # None: a parameter file, with no log behind it
    # The log checks overlapping the flight, from import; None for a flight imported
    # before they were kept.
    validation: Mapping[str, Any] | None = None
    # The setup answers of the build the flight flew (platform reference, then the
    # airframe's own), field id -> value; empty for a flight not yet given an airframe.
    answers: Mapping[str, Any] = field(default_factory=dict[str, Any])
    # The firmware's default for each parameter, canonical, where the log recorded it.
    defaults: Mapping[str, float] = field(default_factory=dict[str, float])
    _tables: dict[tuple[str, tuple[str, ...] | None], pa.Table | None] = field(
        default_factory=dict[tuple[str, tuple[str, ...] | None], pa.Table | None]
    )

    @staticmethod
    def load(store: Store, cache: ColumnCache, pack: Pack, flight_id: str) -> FlightData:
        row = store.get("flight", flight_id)
        if row is None:
            raise FlightError(f"no flight {flight_id}")
        if not row["fingerprint_json"]:
            raise FlightError(f"flight {flight_id} was imported before analysis: import it again")
        snapshot = load_snapshot(store, row["param_snapshot_id"])
        dialect = snapshot.dialect(pack)
        params = dialect.to_canonical(snapshot.values).values
        defaults = dialect.to_canonical(snapshot.defaults).values
        return FlightData(
            id=flight_id,
            key=row["flight_key"],
            airframe_id=row["airframe_id"],
            firmware=row["firmware"],
            arm_us=int(row["arm_us"]),
            disarm_us=int(row["disarm_us"]),
            params=params,
            fingerprint=json.loads(row["fingerprint_json"]),
            cache=cache,
            validation=json.loads(row["validation_json"]) if row["validation_json"] else None,
            answers=build_answers(store, row["build_version_id"]),
            defaults=defaults,
        )

    @property
    def vehicle(self) -> str:
        return str(self.fingerprint.get("vehicle", ""))

    @property
    def simulated(self) -> bool:
        """Flown in ArduPilot's simulator, where timing is the host computer's."""
        return self.fingerprint.get("board", {}).get("hal") == "SITL"

    @property
    def logged(self) -> bool:
        """Whether there is a log behind these parameters (a parameter file has none)."""
        return self.cache is not None

    def has(self, name: str) -> bool:
        return self.cache is not None and name in self.cache.names(self.key)

    def table(self, name: str, columns: list[str] | None = None) -> pa.Table | None:
        """A message type's cached columns for this flight, or None if the log has none.
        Positions are never returned."""
        key = (name, tuple(columns) if columns is not None else None)
        if key not in self._tables:
            self._tables[key] = (
                self.cache.read(self.key, name, columns)
                if self.cache is not None and self.has(name)
                else None
            )
        return self._tables[key]

    @property
    def armed(self) -> Segment:
        return Segment("flight", self.arm_us, self.disarm_us)

    def hover(self) -> list[Segment]:
        """This flight's steady-hover windows, from its fingerprint."""
        return [
            Segment("hover", int(w["start_us"]), int(w["end_us"]))
            for w in self.fingerprint.get("hover_windows", [])
            if self.arm_us <= int(w["start_us"]) and int(w["end_us"]) <= self.disarm_us
        ]
