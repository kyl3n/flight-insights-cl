"""Steady-hover windows (spec: Data ingestion, pipeline step 5 "Segment").

A window is a stretch of a flight where the aircraft holds still in the air:
slow horizontally and vertically, nearly level, high enough to be clear of
ground effect, for long enough. Filter analysis reads full-rate IMU data inside
these windows, and the fingerprint reads hover throttle and propulsion peaks
from them. The conditions come from knowledge/rules/hover.json.

Every condition is read from the primary EKF3 core (XKF1 rows whose core is
XKF4.PI at that moment): velocity north, east and down, attitude, and height
from the down position relative to where the flight armed. A QuadPlane hovers
only in its VTOL modes; a fixed-wing aircraft never does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.dataflash import si
from flight_insights.dataflash.log import DataflashLog
from flight_insights.knowledge import Pack, Vehicle
from flight_insights.segment.flights import Flight
from flight_insights.segment.validate import Boot, boot_index

MAX_SAMPLE_GAP_US = 1_000_000  # missing EKF samples for longer than this end a window
Bools = np.ndarray[tuple[int], np.dtype[np.bool_]]
Ints = np.ndarray[tuple[int], np.dtype[np.int64]]
Floats = np.ndarray[tuple[int], np.dtype[np.float64]]


@dataclass(frozen=True)
class HoverRules:
    min_duration_s: float
    min_height_m: float
    max_horizontal_speed_m_s: float
    max_vertical_speed_m_s: float
    max_tilt_deg: float
    plane_vtol_modes: frozenset[str]
    calibrated: bool

    @classmethod
    def load(cls, pack: Pack) -> HoverRules:
        r: dict[str, Any] = pack.json("rules/hover.json")
        return cls(
            float(r["min_duration_s"]),
            float(r["min_height_m"]),
            float(r["max_horizontal_speed_m_s"]),
            float(r["max_vertical_speed_m_s"]),
            float(r["max_tilt_deg"]),
            frozenset(r["plane_vtol_modes"]),
            bool(r["calibrated"]),
        )


@dataclass(frozen=True)
class HoverWindow:
    boot: int
    flight: int  # Flight.number
    start_us: int
    end_us: int
    mode: str  # the mode at the window's start
    samples: int  # EKF samples that met every condition

    @property
    def duration_s(self) -> float:
        return (self.end_us - self.start_us) / 1e6


@dataclass(frozen=True)
class EkfTrack:
    """The primary EKF3 core's samples, in time order."""

    boot: Ints
    time_us: Ints
    vn: Floats
    ve: Floats
    vd: Floats
    pd: Floats
    roll: Floats
    pitch: Floats


def ekf_track(df: DataflashLog, boots: list[Boot]) -> EkfTrack | None:
    if "XKF1" not in df.message_types:
        return None
    x = df.table("XKF1")
    # table() rows are in file order, so the sorted offsets line up with them
    offsets = np.sort(np.concatenate([df.offsets(f) for f in df.formats_named("XKF1")]))
    time_us = np.asarray(x.column("TimeUS").to_numpy(), dtype=np.int64)
    core = np.asarray(x.column("C").to_numpy(), dtype=np.int64)
    primary = _primary_core(df, time_us)
    keep = core == primary
    cols = {n: si.values(x, n)[keep] for n in ("VN", "VE", "VD", "PD", "Roll", "Pitch")}
    return EkfTrack(
        boot_index(boots, offsets)[keep],
        time_us[keep],
        cols["VN"],
        cols["VE"],
        cols["VD"],
        cols["PD"],
        cols["Roll"],
        cols["Pitch"],
    )


def _primary_core(df: DataflashLog, time_us: Ints) -> Ints:
    """Which core was primary at each time: XKF4.PI as last reported, else core 0."""
    if "XKF4" not in df.message_types or "PI" not in df.table("XKF4").column_names:
        return np.zeros(len(time_us), np.int64)
    x4 = df.table("XKF4")
    t4 = np.asarray(x4.column("TimeUS").to_numpy(), dtype=np.int64)
    pi = np.asarray(x4.column("PI").to_numpy(), dtype=np.int64)
    order = np.argsort(t4, kind="stable")
    t4, pi = t4[order], pi[order]
    idx = np.searchsorted(t4, time_us, side="right") - 1
    return np.where(idx >= 0, pi[np.maximum(idx, 0)], 0).astype(np.int64)


def hover_windows(
    df: DataflashLog,
    boots: list[Boot],
    flights: list[Flight],
    vehicle: Vehicle,
    rules: HoverRules,
) -> list[HoverWindow]:
    track = ekf_track(df, boots)
    if track is None:
        return []
    windows: list[HoverWindow] = []
    for f in flights:
        sel = np.flatnonzero(
            (track.boot == f.boot) & (track.time_us >= f.arm_us) & (track.time_us <= f.disarm_us)
        )
        if len(sel) < 2:
            continue
        t = track.time_us[sel]
        mode = _mode_at(f, t)
        height = -(track.pd[sel] - track.pd[sel[0]])  # above where the flight armed
        ok: Bools = (
            (np.hypot(track.vn[sel], track.ve[sel]) <= rules.max_horizontal_speed_m_s)
            & (np.abs(track.vd[sel]) <= rules.max_vertical_speed_m_s)
            & (np.abs(track.roll[sel]) <= rules.max_tilt_deg)
            & (np.abs(track.pitch[sel]) <= rules.max_tilt_deg)
            & (height >= rules.min_height_m)
        )
        if vehicle == "plane":
            ok &= np.isin(mode, list(rules.plane_vtol_modes))
        # A hole in the EKF record (a logging dropout) ends a window: nothing says
        # the aircraft stayed steady through it.
        ok[1:] &= np.diff(t) <= MAX_SAMPLE_GAP_US
        for a, b in _runs(ok):
            if (t[b] - t[a]) / 1e6 >= rules.min_duration_s:
                windows.append(
                    HoverWindow(f.boot, f.number, int(t[a]), int(t[b]), str(mode[a]), b - a + 1)
                )
    return windows


def _mode_at(f: Flight, t: Ints) -> np.ndarray[tuple[int], np.dtype[np.str_]]:
    names = np.full(len(t), "", dtype=object)
    for m in f.modes:
        names[(t >= m.start_us) & (t < m.end_us)] = m.name
    return names.astype(str)


def _runs(ok: Bools) -> list[tuple[int, int]]:
    """(first, last) index of each run of True."""
    padded = np.concatenate([[False], ok, [False]]).astype(np.int8)
    edges = np.flatnonzero(np.diff(padded))
    return [(int(a), int(b) - 1) for a, b in zip(edges[::2], edges[1::2], strict=True)]
