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
    cache: ColumnCache
    # The log checks overlapping the flight, from import; None for a flight imported
    # before they were kept.
    validation: Mapping[str, Any] | None = None
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
        params = snapshot.dialect(pack).to_canonical(snapshot.values).values
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
        )

    @property
    def vehicle(self) -> str:
        return str(self.fingerprint.get("vehicle", ""))

    @property
    def simulated(self) -> bool:
        """Flown in ArduPilot's simulator, where timing is the host computer's."""
        return self.fingerprint.get("board", {}).get("hal") == "SITL"

    def has(self, name: str) -> bool:
        return name in self.cache.names(self.key)

    def table(self, name: str, columns: list[str] | None = None) -> pa.Table | None:
        """A message type's cached columns for this flight, or None if the log has none.
        Positions are never returned."""
        key = (name, tuple(columns) if columns is not None else None)
        if key not in self._tables:
            self._tables[key] = self.cache.read(self.key, name, columns) if self.has(name) else None
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
