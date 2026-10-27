"""When each failsafe's loss began, and losses no failsafe answered (spec: Failsafe
causes and RF early warning, "Did the failsafe work?").

Each failsafe waits a set time after its loss begins, checked at its scheduler's rate
(ArduCopter 4.7.0; rules/failsafes.json, `timing`). The onboard log shows when the
loss began for three of them, exactly or to its logging rate:

- **Ground station:** `MAV.mgs`, the time the GCS's system ID was last seen on each
  link (heartbeat or manual control), logged about once a second. The failsafe fires
  when no link has seen it for `FS_GCS_TIMEOUT`.
- **Battery:** `BAT`, logged in the same loop that checks the failsafe, so its samples
  are what the firmware judged: the voltage it uses (`Volt`, or `VoltR` with
  `BATT_FS_VOLTSRC` 1) under a threshold for `BATT_LOW_TIMER`, or the charge used past
  the pack's capacity less a reserve, at once.
- **Radio:** the receiver's own failsafe flag (`RCI2.Flags` bit 2), after which frames
  stop counting as input and `RC_FS_TIMEOUT` runs out; or the throttle channel (`RCIN`)
  under `FS_THR_VALUE`, which trips within three frames. A receiver that simply stops
  leaves no trace before the failsafe (`ERR RADIO` late frame marks it).

The EKF failsafe's onset needs the estimator's filtered variances and comes with the
GNSS work. Positions are never read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.flight import FlightData

Array = np.ndarray[Any, Any]
OFF_TIMER_US = 10_000_000  # BATT_LOW_TIMER's default
MONITORS = ("BATT", *(f"BATT{n}" for n in "23456789ABCDEFG"))  # battery instances 0..15
PROTOCOL_FAILSAFE = 1 << 2  # AP_Logger::RCLoggingFlags::RC_PROTOCOL_FAILSAFE
IGNORE_RECEIVER, IGNORE_RECEIVER_FAILSAFE = 1 << 0, 1 << 2  # RC_OPTIONS bits


@dataclass(frozen=True)
class Onset:
    """When a failsafe's loss began, and when the firmware was due to act on it."""

    start_us: int  # the loss began (the last time the GCS was seen, the first low sample)
    due_us: int  # the firmware's wait ran out
    resolution_s: float  # how finely the log places the start
    period_s: float  # how often the firmware checks
    source: str  # what the start was read from, in words
    messages: tuple[str, ...]
    fields: tuple[str, ...]

    def late_s(self, t: int, margin_s: float) -> float:
        """How much later than due it fired, beyond what the check rate and the log's
        resolution allow; 0 when on time."""
        slack = self.period_s + self.resolution_s + margin_s
        return max(0.0, round((t - self.due_us) / 1e6 - slack, 3))


@dataclass(frozen=True)
class Loss:
    """A loss the firmware should have acted on, with no failsafe of its kind."""

    kind: str  # FAILSAFE_GCS ...
    start_us: int
    due_us: int
    end_us: int  # when it ended, or the flight did
    off: bool  # the failsafe is switched off
    setting: str  # what switches it off, or the setting it was due by
    what: str  # the loss, in words
    messages: tuple[str, ...]
    fields: tuple[str, ...]


def _ints(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.int64)


def _floats(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.float64)


def _param(flight: FlightData, name: str, fallback: float = 0.0) -> float:
    return float(flight.params.get(name, flight.defaults.get(name, fallback)))


def _runs(mask: Array) -> list[tuple[int, int]]:
    """(first, last) index of each run of True."""
    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.astype(np.int8), [0]))))
    return [(int(a), int(b) - 1) for a, b in zip(edges[::2], edges[1::2], strict=True)]


def _resolution(times: Array, k: int) -> float:
    """The log's spacing around sample k, in seconds: how finely it places an onset."""
    if len(times) < 2:
        return 0.0
    k = min(max(k, 1), len(times) - 1)
    return round(float(times[k] - times[k - 1]) / 1e6, 3)


# --- the ground station -----------------------------------------------------------------


def _mav(flight: FlightData) -> pa.Table | None:
    mav = flight.table("MAV")
    if mav is None or mav.num_rows == 0 or "mgs" not in mav.column_names:
        return None
    return mav


def last_seen(times: Array, chan: Array, mgs: Array) -> Array:
    """The GCS's last-seen time (µs) as each row was logged: the newest of every link's."""
    latest: dict[int, int] = {}
    out = np.zeros(len(times), np.int64)
    for k in range(len(times)):
        latest[int(chan[k])] = int(mgs[k])
        out[k] = max(latest.values()) * 1000
    return out


def gcs_onset(flight: FlightData, timing: Mapping[str, Any], t: int) -> Onset | None:
    """The GCS was last seen at the newest last-seen time logged before the failsafe. A
    later sighting would have kept it from firing, and a link logs once a second."""
    mav = _mav(flight)
    if mav is None:
        return None
    times = _ints(mav, "TimeUS")
    order = np.argsort(times, kind="stable")
    seen = last_seen(times[order], _ints(mav, "chan")[order], _ints(mav, "mgs")[order])
    before = np.flatnonzero(times[order] <= t)
    if not len(before) or seen[before[-1]] <= 0:
        return None
    start = int(seen[before[-1]])
    timeout = _param(flight, "FS_GCS_TIMEOUT", 5.0)
    return Onset(
        start, start + round(timeout * 1e6), 0.0, float(timing["period_s"]),
        "the ground station last seen (MAV.mgs)", ("MAV",), ("mgs",),
    )  # fmt: skip


def gcs_losses(
    flight: FlightData, timing: Mapping[str, Any], fired: Sequence[tuple[int, int | None]]
) -> list[Loss]:
    """Each time no link saw the GCS for longer than the failsafe waits, while armed, with
    no GCS failsafe started or in force. `fired`: every GCS failsafe's (start, end)."""
    mav = _mav(flight)
    if mav is None:
        return []
    times = _ints(mav, "TimeUS")
    order = np.argsort(times, kind="stable")
    times = times[order]
    seen = last_seen(times, _ints(mav, "chan")[order], _ints(mav, "mgs")[order])
    timeout = round(_param(flight, "FS_GCS_TIMEOUT", 5.0) * 1e6)
    slack = round((float(timing["period_s"]) + float(timing["margin_s"])) * 1e6)
    gone = (seen > 0) & (times - seen > timeout + slack)
    out: list[Loss] = []
    for a, b in _runs(gone):
        start = int(seen[a])
        due = start + timeout
        back = min(int(times[b + 1]) if b + 1 < len(times) else flight.disarm_us, flight.disarm_us)
        if not flight.arm_us <= due <= flight.disarm_us or _answered(fired, due, slack, back):
            continue
        off = _param(flight, "FS_GCS_ENABLE") == 0
        out.append(Loss(
            "FAILSAFE_GCS", start, due, back, off,
            "FS_GCS_ENABLE 0" if off else f"FS_GCS_TIMEOUT {timeout / 1e6:g}",
            "no ground station heard", ("MAV",), ("mgs",),
        ))  # fmt: skip
    return out


def _answered(fired: Sequence[tuple[int, int | None]], due: int, slack: int, until: int) -> bool:
    """A failsafe of this kind started from about when it was due until the loss ended,
    however late (that is the timing check's to judge), or was already in force."""
    return any(
        due - slack <= start <= until or (start < due and (end is None or end > due))
        for start, end in fired
    )


# --- the battery ------------------------------------------------------------------------


def _bat(flight: FlightData) -> pa.Table | None:
    bat = flight.table("BAT")
    if bat is None or bat.num_rows == 0 or "Inst" not in bat.column_names:
        return None
    return bat


@dataclass(frozen=True)
class _Stage:
    """One way a monitor's failsafe can trip: its series, threshold and wait."""

    what: str  # in words
    times: Array
    below: Array  # the condition held at each sample
    wait_us: int
    field: str
    setting: str  # the setting it is due by
    off: bool = False  # the voltage failsafe is switched off (a timer of 0)


def _stages(flight: FlightData, bat: pa.Table, inst: int) -> list[_Stage]:
    prefix = MONITORS[inst]
    rows = _ints(bat, "Inst") == inst
    times = _ints(bat, "TimeUS")[rows]
    out: list[_Stage] = []
    timer = round(_param(flight, f"{prefix}_LOW_TIMER", 10.0) * 1e6)
    field = "VoltR" if _param(flight, f"{prefix}_FS_VOLTSRC") == 1 else "Volt"
    if field in bat.column_names:
        volts = _floats(bat, field)[rows]
        for stage in ("CRT_VOLT", "LOW_VOLT"):
            limit = _param(flight, f"{prefix}_{stage}")
            if limit > 0:
                below = (volts > 0) & (volts < limit)
                setting = f"{prefix}_LOW_TIMER {timer / 1e6:g}"
                # a timer of 0 switches the voltage failsafe off; the default wait is the
                # yardstick for how long the voltage stayed low
                out.append(_Stage(f"{field} under {prefix}_{stage} {limit:g} V", times, below,
                                  timer or OFF_TIMER_US, field, setting, timer == 0))  # fmt: skip
    capacity = _param(flight, f"{prefix}_CAPACITY")
    if "CurrTot" in bat.column_names and capacity > 0:
        used = _floats(bat, "CurrTot")[rows]
        for stage in ("CRT_MAH", "LOW_MAH"):
            reserve = _param(flight, f"{prefix}_{stage}")
            if reserve > 0 and np.any(used > 0):
                left = capacity - used
                out.append(_Stage(f"over {capacity - reserve:g} mAh used", times, left < reserve,
                                  0, "CurrTot", f"{prefix}_{stage} {reserve:g}"))  # fmt: skip
    return out


def _enabled(flight: FlightData, bat: pa.Table) -> list[int]:
    seen = np.unique(_ints(bat, "Inst"))
    return [
        int(i)
        for i in seen
        if 0 <= i < len(MONITORS) and _param(flight, f"{MONITORS[i]}_MONITOR") != 0
    ]


def battery_onset(flight: FlightData, timing: Mapping[str, Any], t: int) -> Onset | None:
    """The stage that explains a battery failsafe at `t`: the condition holding at its
    last check, begun long enough before. The firmware counts its wait from its first
    armed check, and logs BAT in that same loop, just before checking."""
    bat = _bat(flight)
    if bat is None:
        return None
    best: Onset | None = None
    for inst in _enabled(flight, bat):
        for s in _stages(flight, bat, inst):
            if s.off:
                continue  # it cannot have fired
            checked = np.flatnonzero((s.times <= t + 1000) & (s.times >= flight.arm_us))
            if not len(checked) or not s.below[checked[-1]]:
                continue
            first = checked[-1]
            while first > checked[0] and s.below[first - 1]:
                first -= 1
            start = int(s.times[first])
            onset = Onset(
                start, start + s.wait_us, _resolution(s.times, first),
                float(timing["period_s"]), s.what, ("BAT",), (s.field,),
            )  # fmt: skip
            if onset.due_us <= t + round(float(timing["margin_s"]) * 1e6) and (
                best is None or onset.due_us > best.due_us
            ):
                best = onset  # the most recent explanation that was due
    return best


def battery_losses(
    flight: FlightData, timing: Mapping[str, Any], fired: Sequence[tuple[int, int | None]]
) -> list[Loss]:
    """A stage's condition held, while armed, past its wait with no battery failsafe at
    all by then. Any battery failsafe before counts as an answer: which monitor and stage
    one was raised for is not logged, so only a flight with none can have missed one."""
    bat = _bat(flight)
    if bat is None:
        return []
    out: list[Loss] = []
    margin = round(float(timing["margin_s"]) * 1e6)
    for inst in _enabled(flight, bat):
        for s in _stages(flight, bat, inst):
            armed = (s.times >= flight.arm_us) & (s.times <= flight.disarm_us)
            for a, b in _runs(s.below & armed):
                start = int(s.times[a])
                due = start + s.wait_us
                slack = round(_resolution(s.times, a) * 1e6) + margin
                if int(s.times[b]) - due <= slack:
                    continue  # it did not last past the wait
                if any(f_start <= int(s.times[b]) for f_start, _ in fired):
                    continue  # one fired by the end, for this stage or one before it
                out.append(Loss(
                    "FAILSAFE_BATT", start, due, int(s.times[b]), s.off, s.setting, s.what,
                    ("BAT",), (s.field,),
                ))  # fmt: skip
                break  # the first is the one that mattered
    if out:
        return [min(out, key=lambda x: x.due_us)]
    return out


# --- the radio --------------------------------------------------------------------------


def _late_frame(flight: FlightData, names: Mapping[str, Any], t: int) -> bool:
    """The firmware logged a late RC frame just before this failsafe: a timeout, not a
    low throttle."""
    err = flight.table("ERR", ["TimeUS", "Subsys", "ECode"])
    if err is None:
        return False
    subsystems: Mapping[str, str] = names.get("error_subsystems", {})
    codes: Mapping[str, list[str]] = names.get("error_codes", {})
    times, sub, code = _ints(err, "TimeUS"), _ints(err, "Subsys"), _ints(err, "ECode")
    for k in np.flatnonzero(np.abs(times - t) <= 2000):
        if subsystems.get(str(int(sub[k]))) == "RADIO" and "RADIO_LATE_FRAME" in codes.get(
            str(int(code[k])), []
        ):
            return True
    return False


def receiver_flagged(flight: FlightData) -> tuple[Array, Array] | None:
    """When the receiver said it was in failsafe (RCI2), unless RC_OPTIONS ignores that."""
    if int(_param(flight, "RC_OPTIONS")) & (IGNORE_RECEIVER | IGNORE_RECEIVER_FAILSAFE):
        return None
    rci2 = flight.table("RCI2")
    if rci2 is None or rci2.num_rows == 0 or "Flags" not in rci2.column_names:
        return None
    times = _ints(rci2, "TimeUS")
    return times, (_ints(rci2, "Flags") & PROTOCOL_FAILSAFE) > 0


def low_throttle(flight: FlightData) -> tuple[Array, Array, str] | None:
    """When the throttle channel read under FS_THR_VALUE: a receiver's failsafe output,
    since FS_THR_VALUE sits under the stick's lowest (by 10 or more: Copter's own check),
    and only while the channel was not overridden over MAVLink."""
    if int(_param(flight, "RC_OPTIONS")) & IGNORE_RECEIVER:
        return None
    channel = int(_param(flight, "RCMAP_THROTTLE", 3))
    value = _param(flight, "FS_THR_VALUE", 975)
    stick_min = _param(flight, f"RC{channel}_MIN", 1100)
    rcin = flight.table("RCIN")
    column = f"C{channel}"
    if rcin is None or rcin.num_rows == 0 or column not in rcin.column_names:
        return None
    if value < 910 or stick_min - value < 10:
        return None  # a stick at its lowest could read under it
    times, pwm = _ints(rcin, "TimeUS"), _ints(rcin, column)
    low = (pwm > 0) & (pwm < value)
    rci2 = flight.table("RCI2")
    if rci2 is not None and rci2.num_rows and "OMask" in rci2.column_names:
        overridden = (_ints(rci2, "OMask") >> (channel - 1)) & 1
        k = np.clip(np.searchsorted(_ints(rci2, "TimeUS"), times, side="right") - 1, 0, None)
        low &= overridden[k] == 0
    return times, low, f"RCIN.{column} under FS_THR_VALUE {value:g}"


def column_of(what: str) -> str:
    """The RCIN field named in a low-throttle description."""
    return what.split(" ")[0].split(".")[1]


def radio_onset(
    flight: FlightData, names: Mapping[str, Any], timing: Mapping[str, Any], t: int
) -> Onset | None:
    period = float(timing["period_s"])
    if _late_frame(flight, names, t):
        flagged = receiver_flagged(flight)
        if flagged is None:
            return None  # the receiver stopped: its last frame is not logged
        times, flag = flagged
        start = _run_start(times, flag, t)
        if start is None:
            return None
        k, at = start
        wait = round(_param(flight, "RC_FS_TIMEOUT", 1.0) * 1e6)
        what = "the receiver's failsafe flag (RCI2.Flags)"
        return Onset(at, at + wait, _resolution(times, k), period, what, ("RCI2",), ("Flags",))
    low = low_throttle(flight)
    if low is None:
        return None
    times, below, what = low
    start = _run_start(times, below, t)
    if start is None:
        return None
    k, at = start
    frames = float(timing["frames"]) * float(timing["frame_s"])
    return Onset(at, at + round(frames * 1e6), _resolution(times, k), period,
                 f"the throttle {what}", ("RCIN",), (column_of(what),))  # fmt: skip


def _run_start(times: Array, mask: Array, t: int) -> tuple[int, int] | None:
    """Where the run behind a failsafe at `t` began: the run holding at the last sample
    up to `t`, or, when the firmware acted between samples, the one the next sample
    shows. The loss began no later than the failsafe, so the start is capped at `t`."""
    up_to = np.flatnonzero(times <= t + 1000)
    if len(up_to) and mask[up_to[-1]]:
        k = int(up_to[-1])
        while k > 0 and mask[k - 1]:
            k -= 1
        return k, int(times[k])
    k = int(up_to[-1]) + 1 if len(up_to) else 0
    if k == 0 or k >= len(times) or not mask[k]:
        return None
    return k, min(int(times[k]), t)


def radio_losses(
    flight: FlightData, timing: Mapping[str, Any], fired: Sequence[tuple[int, int | None]]
) -> list[Loss]:
    """The receiver said it was in failsafe for longer than RC_FS_TIMEOUT, or sent a
    throttle under FS_THR_VALUE for three frames and a log sample more, while armed, with
    no radio failsafe started or in force."""
    off = _param(flight, "FS_THR_ENABLE") == 0
    margin = float(timing["margin_s"])
    period = float(timing["period_s"])
    found: list[Loss] = []
    flagged = receiver_flagged(flight)
    if flagged is not None:
        times, flag = flagged
        wait = round(_param(flight, "RC_FS_TIMEOUT", 1.0) * 1e6)
        found += _held(flight, times, flag, wait, margin, period, fired, off,
                       "FS_THR_ENABLE 0" if off else f"RC_FS_TIMEOUT {wait / 1e6:g}",
                       "the receiver in failsafe", ("RCI2",), ("Flags",))  # fmt: skip
    low = low_throttle(flight)
    if low is not None:
        times, below, what = low
        wait = round(float(timing["frames"]) * float(timing["frame_s"]) * 1e6)
        value = what.rsplit(" ", 2)
        found += _held(flight, times, below, wait, margin, period, fired, off,
                       "FS_THR_ENABLE 0" if off else " ".join(value[-2:]),
                       f"the throttle under {' '.join(value[-2:])}", ("RCIN",),
                       (column_of(what),))  # fmt: skip
    return sorted(found, key=lambda x: x.due_us)[:1]  # one loss of the link, read two ways


def _held(
    flight: FlightData, times: Array, mask: Array, wait: int, margin: float, period: float,
    fired: Sequence[tuple[int, int | None]], off: bool, setting: str, what: str,
    messages: tuple[str, ...], fields: tuple[str, ...],
) -> list[Loss]:  # fmt: skip
    out: list[Loss] = []
    armed = (times >= flight.arm_us) & (times <= flight.disarm_us)
    for a, b in _runs(mask & armed):
        start = int(times[a])
        due = start + wait
        slack = round((_resolution(times, a) + period + margin) * 1e6)
        if int(times[b]) - due <= slack or _answered(fired, due, slack, int(times[b])):
            continue
        out.append(Loss("FAILSAFE_RADIO", start, due, int(times[b]), off, setting, what,
                        messages, fields))  # fmt: skip
    return out


def onset(
    flight: FlightData, names: Mapping[str, Any], timing: Mapping[str, Any], kind: str, t: int
) -> Onset | None:
    """When the loss behind a failsafe of `kind` at `t` began, where the log shows it."""
    own: Mapping[str, Any] = {**timing, **timing.get(kind, {})}
    if kind == "FAILSAFE_GCS":
        return gcs_onset(flight, own, t)
    if kind == "FAILSAFE_BATT":
        return battery_onset(flight, own, t)
    if kind == "FAILSAFE_RADIO":
        return radio_onset(flight, names, own, t)
    return None  # the EKF's comes with the GNSS work


def losses(
    flight: FlightData,
    timing: Mapping[str, Any],
    fired: Mapping[str, Sequence[tuple[int, int | None]]],
) -> list[Loss]:
    """Every loss no failsafe answered. `fired`: each kind's failsafes, (start, end)."""

    def own(kind: str) -> Mapping[str, Any]:
        return {**timing, **timing.get(kind, {})}

    found = (
        gcs_losses(flight, own("FAILSAFE_GCS"), fired.get("FAILSAFE_GCS", []))
        + battery_losses(flight, own("FAILSAFE_BATT"), fired.get("FAILSAFE_BATT", []))
        + radio_losses(flight, own("FAILSAFE_RADIO"), fired.get("FAILSAFE_RADIO", []))
    )
    return sorted(found, key=lambda x: x.due_us)
