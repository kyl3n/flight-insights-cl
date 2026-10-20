"""Parameter snapshots: the exact set a flight flew with (spec: Parameter history,
"Linking every flight to its exact parameters").

The onboard log is the primary source: ArduPilot writes every parameter at the
start of each log and every change made while logging, with its time. When a
flight has no log, the full download a ground station recorded in a tlog, and
then the latest parameter file saved before the flight, stand in, marked as
fallbacks.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from flight_insights.dataflash.formats import text
from flight_insights.dataflash.log import DataflashLog
from flight_insights.knowledge import Pack, Vehicle
from flight_insights.params.dialect import Dialect, ParamClass
from flight_insights.params.files import ParamFile, format_value
from flight_insights.tlog.params import ParamDownload

Origin = Literal["log start", "tlog download", "file"]
Confidence = Literal["primary", "fallback"]

# The classes a parameter-set ID covers. Counters and learned values change on
# every flight and ground stations rewrite stream rates on connection, so
# including them would give every flight a new ID even with nothing re-tuned.
SET_ID_CLASSES: frozenset[ParamClass] = frozenset({"tuning", "calibration", "hardware"})
SET_ID_VERSION = "ps1"
FIRMWARE = re.compile(r"\bArdu(Copter|Plane)\b")
ARMED_EVENT = 10  # EV Id: armed


class SnapshotError(ValueError):
    """A source does not hold what a snapshot needs."""


@dataclass(frozen=True)
class ParamChange:
    """A value changed while the log was recording: tuning knob, script or ground station."""

    time_us: int  # log time (microseconds since boot)
    name: str
    old: float
    new: float


@dataclass(frozen=True)
class Snapshot:
    values: dict[str, float]  # in the firmware's own names: the dialect it flew with
    vehicle: Vehicle
    firmware: str  # "4.7.2"
    origin: Origin
    confidence: Confidence
    time_us: int | None = None  # log time the set applies from, for log snapshots
    # Changes after the snapshot, while the same log was recording.
    changes: tuple[ParamChange, ...] = field(default=())
    # The firmware's default for each parameter, where the log records it (PARM.Default).
    # Not part of the snapshot's identity: it describes the firmware, not the vehicle.
    defaults: dict[str, float] = field(default_factory=dict[str, float], compare=False)

    def is_default(self, name: str) -> bool:
        """True when the value is the firmware's default: nobody chose it for this vehicle."""
        d = self.defaults.get(name)
        return d is not None and name in self.values and same_value(self.values[name], d)

    def dialect(self, pack: Pack) -> Dialect:
        return Dialect(pack, self.vehicle, self.firmware)

    def final_values(self) -> dict[str, float]:
        """The set as it stood at the end of the log, after the in-flight changes."""
        out = dict(self.values)
        for c in self.changes:
            out[c.name] = c.new
        return out


def same_value(a: float, b: float) -> bool:
    """Parameters are float32 on the autopilot; compare them as it would."""
    x, y = np.float32(a), np.float32(b)
    return bool(x == y or (np.isnan(x) and np.isnan(y)))


# --- sources ------------------------------------------------------------------------


def log_firmware(df: DataflashLog) -> tuple[Vehicle, str]:
    """Vehicle type and version from the log's VER message."""
    ver = df.table("VER").to_pylist() if "VER" in df.message_types else []
    if not ver:
        raise SnapshotError("the log has no VER message, so its firmware is unknown")
    v = ver[0]
    m = FIRMWARE.search(text(v["FWS"]))
    if not m:
        raise SnapshotError("the log's firmware is neither ArduCopter nor ArduPlane")
    return m[1].lower(), f"{v['Maj']}.{v['Min']}.{v['Pat']}"


def first_arm_us(df: DataflashLog) -> int | None:
    """When the vehicle first armed in this log, if the log saw it happen.

    A log that starts on arming (the default without LOG_DISARMED) holds no arming
    record; its start-of-log parameters are then the set it armed with.
    """
    times: list[int] = []
    if "ARM" in df.message_types:
        times += [r["TimeUS"] for r in df.table("ARM").to_pylist() if r["ArmState"] == 1]
    if "EV" in df.message_types:
        times += [r["TimeUS"] for r in df.table("EV").to_pylist() if r["Id"] == ARMED_EVENT]
    return min(times) if times else None


def from_log(df: DataflashLog, at_us: int | None = None) -> Snapshot:
    """The log's parameters as they stood at `at_us` (default: first arming, or the end
    of the start-of-log dump), with every later change kept as an in-flight change."""
    vehicle, firmware = log_firmware(df)
    if "PARM" not in df.message_types:
        raise SnapshotError("the log has no PARM messages")
    initial: dict[str, float] = {}
    current: dict[str, float] = {}
    defaults: dict[str, float] = {}
    dump_end = 0
    changes: list[ParamChange] = []
    for r in sorted(df.table("PARM").to_pylist(), key=lambda r: r["TimeUS"]):
        name, value = text(r["Name"]), float(r["Value"])
        if name not in initial:
            initial[name] = current[name] = value  # the start-of-log dump
            dump_end = max(dump_end, r["TimeUS"])
            default = r.get("Default")
            if default is not None and not math.isnan(default):
                defaults[name] = float(default)
        elif not same_value(current[name], value):
            changes.append(ParamChange(r["TimeUS"], name, current[name], value))
            current[name] = value
    at = at_us if at_us is not None else first_arm_us(df)
    # A parameter not dumped yet has no value to report, so never before the dump ends.
    at = dump_end if at is None else max(at, dump_end)
    values = dict(initial)
    for c in changes:
        if c.time_us <= at:
            values[c.name] = c.new
    after = tuple(c for c in changes if c.time_us > at)
    return Snapshot(values, vehicle, firmware, "log start", "primary", at, after, defaults)


def from_tlog_download(download: ParamDownload, vehicle: Vehicle, firmware: str) -> Snapshot:
    return Snapshot(
        {k: float(v) for k, v in download.values.items()},
        vehicle,
        firmware,
        "tlog download",
        "fallback",
    )


def from_param_file(params: ParamFile, vehicle: Vehicle, firmware: str) -> Snapshot:
    """A saved file as a snapshot. QGroundControl files keep other components'
    parameters apart; only the autopilot's (component 1) are taken."""
    values = {e.name: e.value for e in params.entries if e.component is None or e.component[1] == 1}
    if not values:
        raise SnapshotError("the file holds no autopilot parameters")
    return Snapshot(values, vehicle, firmware, "file", "fallback")


@dataclass(frozen=True)
class Candidate:
    utc_us: int  # when the source was recorded or saved
    snapshot: Snapshot


def choose_snapshot(
    log: Snapshot | None,
    downloads: Sequence[Candidate],
    files: Sequence[Candidate],
    flight_start_utc_us: int,
) -> Snapshot | None:
    """The flight's own log if it has one; otherwise the latest tlog download before
    the flight; otherwise the latest file saved before it."""
    if log is not None:
        return log
    for pool in (downloads, files):
        earlier = [c for c in pool if c.utc_us <= flight_start_utc_us]
        if earlier:
            return max(earlier, key=lambda c: c.utc_us).snapshot
    return None


# --- identity -----------------------------------------------------------------------


def param_set_id(snapshot: Snapshot, pack: Pack) -> str:
    """A hash of the configuration: flights with the same ID flew identical settings.

    Computed on canonical names and values, so the same settings give the same ID on
    either side of a firmware rename. Covers the classes in SET_ID_CLASSES.
    """
    dialect = snapshot.dialect(pack)
    lines: list[str] = []
    for name, value in snapshot.values.items():
        if dialect.param_class(name, snapshot.values) not in SET_ID_CLASSES:
            continue
        canonical, converted = dialect.canonical(name, value)
        lines.append(f"{canonical}={format_value(value if converted is None else converted)}\n")
    digest = hashlib.sha256("".join(sorted(lines)).encode()).hexdigest()
    return f"{SET_ID_VERSION}-{digest}"
