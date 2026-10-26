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

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.local_track import LocalTrack, Series, tracks
from flight_insights.analyzers.model import Segment
from flight_insights.dataflash import si
from flight_insights.history.persist import load_snapshot
from flight_insights.knowledge import Pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store
from flight_insights.tlog import sessions as tlog_sessions


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
    # The flight's tlog sessions (tlog/sessions.py): each tlog that recorded its boot,
    # with the station's clock fitted against the vehicle's.
    tlogs: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
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
            tlogs=tlog_sessions.sessions(store, flight_id),
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

    def local_track(self) -> LocalTrack | None:
        """Where the aircraft was relative to its first fix, in metres (local_track.py),
        from the estimator's position (POS), else the first GNSS receiver's. This and
        `local_tracks` are the one place analysis reads positions, through the cache's
        positions key; what they return holds no coordinates and refuses to leave the
        analysis."""
        found = self.local_tracks()
        return found.get("POS") or found.get("GPS0")

    def local_tracks(self) -> dict[str, LocalTrack]:
        """The estimator's track ("POS") and each GNSS receiver's ("GPS0", "GPS1" ...),
        all from one origin so they can be compared: the estimator's first fix, else the
        first receiver's. Every sample a receiver logged is kept, whatever its fix; the
        GPS table's `Status` at the same `TimeUS` says which to trust."""
        if self.cache is None:
            return {}
        series: dict[str, Series] = {}
        if self.has("POS"):
            pos = self.cache.read(self.key, "POS", ["TimeUS", "Lat", "Lng", "Alt"], positions=True)
            if pos.num_rows and {"Lat", "Lng", "Alt"} <= set(pos.column_names):
                series["POS"] = (_times(pos), si.degrees(pos, "Lat"), si.degrees(pos, "Lng"),
                                 si.values(pos, "Alt"))  # fmt: skip
        if self.has("GPS"):
            gps = self.cache.read(self.key, "GPS", ["TimeUS", "I", "Lat", "Lng", "Alt"],
                                  positions=True)  # fmt: skip
            if gps.num_rows and {"Lat", "Lng", "Alt"} <= set(gps.column_names):
                instance = np.zeros(gps.num_rows, np.int64)
                if "I" in gps.column_names:
                    instance = np.asarray(gps.column("I").to_numpy(zero_copy_only=False), np.int64)
                t, lat, lon = _times(gps), si.degrees(gps, "Lat"), si.degrees(gps, "Lng")
                alt = si.values(gps, "Alt")
                for i in np.unique(instance):
                    rows = instance == i
                    series[f"GPS{int(i)}"] = (t[rows], lat[rows], lon[rows], alt[rows])
        return tracks(series)

    def tlog(
        self, name: str, columns: list[str] | None = None
    ) -> list[tuple[Mapping[str, Any], pa.Table]]:
        """A message type as each tlog session of the flight recorded it, with `TimeUS`
        on the flight's own clock; sessions with no clock or no such packets are left
        out. Positions are never returned."""
        if self.cache is None:
            return []
        out: list[tuple[Mapping[str, Any], pa.Table]] = []
        for s in self.tlogs:
            table = tlog_sessions.session_table(self.cache, s, name, columns)
            if table is not None and table.num_rows:
                out.append((s, table))
        return out

    def tlog_packets(self) -> list[tuple[Mapping[str, Any], pa.Table]]:
        """Every packet each tlog session recorded, in the order the station recorded them,
        with sender, sequence and `TimeUS` on the flight's own clock (no message fields)."""
        if self.cache is None:
            return []
        out: list[tuple[Mapping[str, Any], pa.Table]] = []
        for s in self.tlogs:
            table = tlog_sessions.session_packets(self.cache, s)
            if table is not None and table.num_rows:
                out.append((s, table))
        return out

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


def _times(table: pa.Table) -> np.ndarray[Any, np.dtype[np.int64]]:
    return np.asarray(table.column("TimeUS").to_numpy(zero_copy_only=False), np.int64)
