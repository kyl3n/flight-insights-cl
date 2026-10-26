"""The tune check: how closely each axis follows its target through the pilot's stick
inputs (spec: Copter analyzers, "Rate-loop tune").

Reads RATE (the rate targets and the measured rates), with the modes, the rate PIDs'
saturation flags, the height (CTUN) and the sticks (RCIN) to decide which inputs
measure the loop and nothing else (segment/excitation.py). On each counted input two
errors are measured, and each is averaged per axis with a seeded bootstrap interval
over the inputs, so a later flight of the same profile can be compared with it:

- **attitude tracking error** (roll and pitch): the attitude against its target (ANG),
  what the pilot and the payload feel. It is the tune's measure: AutoTune raises the
  angle P and the acceleration limits as well as the rate gains, and only this error
  shows all of it.
- **rate tracking error**: the rate against its target (RATE). The rate target carries
  the attitude loop's corrections, so a stiffer attitude loop makes it busier; the rate
  error reads the inner loop, not the whole tune.

The rates must be logged every loop (LOG_BITMASK bit 0); at the 10 Hz of bit 1 a
one-second input is ten samples, and the logging analyzer says how to log them
(logging.tune_evidence).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import (
    Analyzer,
    CandidateChange,
    Evidence,
    Finding,
    MetricValue,
    Result,
    Segment,
)
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si
from flight_insights.segment.excitation import (
    AXES,
    PID_MESSAGES,
    RATE_FIELDS,
    REASONS,
    Event,
    Signals,
    events,
)
from flight_insights.stats import bootstrap_mean

Array = np.ndarray[Any, Any]
TO_DEG_S = {"deg/s": 1.0, "rad/s": 180.0 / math.pi}
TO_DEG = {"deg": 1.0, "rad": 180.0 / math.pi}
ATTITUDES = {"roll": ("DesRoll", "Roll"), "pitch": ("DesPitch", "Pitch")}


def logged_hz(flight: FlightData, message: str) -> float:
    """How often a message was logged while armed; 0 when it was not."""
    table = flight.table(message, ["TimeUS"])
    if table is None or table.num_rows < 3:
        return 0.0
    t = np.asarray(table.column("TimeUS").to_numpy(), np.int64)
    t = t[(t >= flight.arm_us) & (t <= flight.disarm_us)]
    return 1e6 / float(np.median(np.diff(t))) if len(t) > 2 else 0.0


def rate_hz(flight: FlightData) -> float:
    """How often RATE was logged while armed; 0 when it was not."""
    return logged_hz(flight, "RATE")


def needed_hz(flight: FlightData, reg: Registry) -> float:
    """The rate the tune check needs: its minimum, or nearly the loop rate on a slower loop."""
    rules: Mapping[str, Any] = reg.rules["tune"]["tracking"]
    loop = float(flight.params.get("SCHED_LOOP_RATE", 400))
    return min(float(rules["min_rate_hz"]), float(rules["loop_share"]) * loop)


def modes(flight: FlightData, reg: Registry) -> list[tuple[int, int, str]]:
    table = flight.table("MODE", ["TimeUS", "ModeNum"])
    if table is None or table.num_rows == 0:
        return []
    names: Mapping[str, str] = reg.names(flight.firmware)["modes"].get(flight.vehicle, {})
    t = np.asarray(table.column("TimeUS").to_numpy(), np.int64)
    n = np.asarray(table.column("ModeNum").to_numpy(), np.int64)
    order = np.argsort(t, kind="stable")
    rows = [(int(t[i]), names.get(str(int(n[i])), f"MODE_{int(n[i])}")) for i in order]
    rows = [r for k, r in enumerate(rows) if not k or rows[k - 1][1] != r[1]]  # asked again
    ends = [r[0] for r in rows[1:]] + [max(flight.disarm_us, rows[-1][0]) + 1]
    return [(start, end, name) for (start, name), end in zip(rows, ends, strict=True)]


def limited(flight: FlightData, axis: str, bit: int) -> Array | None:
    table = flight.table(PID_MESSAGES[axis], ["TimeUS", "Flags"])
    if table is None or table.num_rows == 0:
        return None
    t = np.asarray(table.column("TimeUS").to_numpy(), np.int64)
    flags = np.asarray(table.column("Flags").to_numpy(), np.int64)
    return t[(flags >> bit & 1).astype(bool)]


def height(flight: FlightData) -> tuple[Array, Array] | None:
    table = flight.table("CTUN", ["TimeUS", "Alt"])
    if table is None or table.num_rows == 0 or si.unit(table, "Alt") != "m":
        return None
    t = np.asarray(table.column("TimeUS").to_numpy(), np.int64)
    alt = si.values(table, "Alt")
    after = np.flatnonzero(t >= flight.arm_us)
    if not len(after):
        return None
    return t, alt - alt[after[0]]


def rates(rate: pa.Table, axis: str) -> tuple[Array, Array] | None:
    """An axis's rate target and measured rate in deg/s, in the unit the log names."""
    target_name, actual_name = RATE_FIELDS[axis]
    unit = si.unit(rate, target_name)
    if unit not in TO_DEG_S or si.unit(rate, actual_name) != unit:
        return None
    k = TO_DEG_S[unit]
    return si.values(rate, target_name) * k, si.values(rate, actual_name) * k


def attitudes(flight: FlightData, axis: str) -> tuple[Array, Array, Array] | None:
    """An axis's attitude target and attitude in degrees, from ANG (every loop with
    LOG_BITMASK bit 0; ATT is only 10 Hz in 4.7), at ANG's times."""
    names = ATTITUDES.get(axis)
    if names is None:
        return None
    ang = flight.table("ANG", ["TimeUS", *names])
    if ang is None or ang.num_rows < 3:
        return None
    unit = si.unit(ang, names[0])
    if unit not in TO_DEG or si.unit(ang, names[1]) != unit:
        return None
    t = np.asarray(ang.column("TimeUS").to_numpy(), np.int64)
    k = TO_DEG[unit]
    return t, si.values(ang, names[0]) * k, si.values(ang, names[1]) * k


def attitude_error(
    att: tuple[Array, Array, Array], e: Event, rules: Mapping[str, Any]
) -> float | None:
    """RMS(target - attitude) over an input, as a share of how far the input moved the
    target from where it started: in wind the target holds a trim angle, which is not
    the pilot's command. None with too few samples or too small a move to compare."""
    t, target, actual = att
    inside = (t >= e.start_us) & (t <= e.end_us)
    if int(inside.sum()) < int(rules["min_samples"]):
        return None
    d: Array = target[inside]
    a: Array = actual[inside]
    moved = math.sqrt(float(np.mean((d - float(d[0])) ** 2)))
    if moved < float(rules["min_move_deg"]):
        return None
    return math.sqrt(float(np.mean((d - a) ** 2))) / moved


def _stick(flight: FlightData, axis: str, rules: Mapping[str, Any]) -> tuple[Array, Array] | None:
    """The axis's stick as a fraction of its throw beyond the dead zone, signed."""
    defaults: Mapping[str, float] = rules["stick_defaults"]
    p = flight.params
    mapping = str(rules["sticks"][axis])
    channel = int(p.get(mapping, defaults[mapping]))
    table = flight.table("RCIN")
    column = f"C{channel}"
    if table is None or table.num_rows == 0 or column not in table.column_names:
        return None
    t = np.asarray(table.column("TimeUS").to_numpy(), np.int64)
    pwm = np.asarray(table.column(column).to_numpy(), np.float64)
    low, trim, high, dz = (
        float(p.get(f"RC{channel}_{k}", defaults[k])) for k in ("MIN", "TRIM", "MAX", "DZ")
    )
    off = pwm - trim
    throw = np.where(off >= 0, high - trim, trim - low) - dz
    moved = np.maximum(np.abs(off) - dz, 0.0) / np.maximum(throw, 1.0)
    return t, np.sign(off) * moved


def _interval(errors: Sequence[float], seed: int, rules: Mapping[str, Any]) -> tuple[float, float]:
    return bootstrap_mean(errors, seed, float(rules["level"]), int(rules["resamples"]))


def _counts(found: Sequence[Event]) -> dict[str, int]:
    return {r: sum(e.rejected == r for e in found) for r in REASONS}


@dataclass(frozen=True)
class Inputs:
    """An axis's stick inputs, each counted or left out with why, and whether the log
    let saturation and the stick be checked."""

    events: list[Event]
    saturation_checked: bool
    sticks_checked: bool


def found_inputs(flight: FlightData, reg: Registry) -> dict[str, Inputs] | str:
    """Every stick input on each axis the log measures (segment/excitation.py), or why
    the log cannot show them."""
    if not flight.logged:
        return "a parameter file has no flight to measure"
    if flight.vehicle != "copter":  # Plane's and QuadPlane's rate loops: Phase 3
        return "the tune check is for Copter"
    rate = flight.table("RATE")
    if rate is None or rate.num_rows == 0:
        return "the log has no RATE messages"
    hz, needed = rate_hz(flight), needed_hz(flight, reg)
    if hz < needed:
        return f"RATE is logged at {hz:.0f} Hz; the tune check needs {needed:.0f}"
    found_rules: Mapping[str, Any] = reg.rules["tune"]["excitation"]
    t = np.asarray(rate.column("TimeUS").to_numpy(), np.int64)
    armed = (t >= flight.arm_us) & (t <= flight.disarm_us)
    flown, heights = modes(flight, reg), height(flight)
    out: dict[str, Inputs] = {}
    for axis in AXES:
        both = rates(rate, axis)
        if both is None:
            continue
        target, actual = both
        signals = Signals(
            modes=flown,
            limited=limited(flight, axis, int(found_rules["limit_flag"])),
            height=heights,
            stick=_stick(flight, axis, found_rules),
        )
        found = events(axis, t[armed], target[armed], actual[armed], signals, found_rules)
        out[axis] = Inputs(found, signals.limited is not None, signals.stick is not None)
    return out


def _tune(flight: FlightData, reg: Registry) -> Result:
    inputs = found_inputs(flight, reg)
    if isinstance(inputs, str):
        return Result(skipped=inputs)
    rules: Mapping[str, Any] = reg.rules["tune"]
    tracking: Mapping[str, Any] = rules["tracking"]
    attitude_rules: Mapping[str, Any] = rules["attitude"]
    ang_ok = logged_hz(flight, "ANG") >= needed_hz(flight, reg)
    out = Result()
    for axis, found_on in inputs.items():
        found = found_on.events
        counted = [e for e in found if e.rejected is None]
        if len(counted) < int(tracking["min_events"]):
            continue
        errors = [100.0 * e.error for e in counted]
        low, high = _interval(errors, int(tracking["seed"][axis]), tracking)
        mean = float(np.mean(errors))
        ev = Evidence(
            id=f"tune:{axis}",
            kind="metric",
            summary=(
                f"{axis} rate tracking error {mean:.1f}% (95% interval {low:.1f}-{high:.1f}%) "
                f"over {len(counted)} stick inputs of {len(found)} found"
            ),
            messages=("RATE", PID_MESSAGES[axis], "MODE", "CTUN", "RCIN"),
            fields=RATE_FIELDS[axis],
            segment=Segment("inputs", counted[0].start_us, counted[-1].end_us),
            values={
                "counted": len(counted),
                "found": len(found),
                "left_out": {r: n for r, n in _counts(found).items() if n},
                "errors_percent": [round(x, 1) for x in errors][:64],
                "peak_target_dps": [round(e.peak_dps, 1) for e in counted][:64],
                "stick": [None if e.stick is None else round(e.stick, 2) for e in counted][:64],
                "saturation_checked": found_on.saturation_checked,
                "sticks_checked": found_on.sticks_checked,
            },
        )
        out.evidence.append(ev)
        out.metrics.append(
            MetricValue(
                f"tune.tracking_error_{axis}", "inputs", mean, None, ev.id,
                spread={"low": low, "high": high, "events": float(len(counted))},
            )
        )  # fmt: skip
        att = attitudes(flight, axis) if ang_ok else None
        if att is not None:
            _attitude(axis, att, counted, attitude_rules, tracking, out)
    _few({a: i.events for a, i in inputs.items()}, tracking, out)
    _offer(flight, reg, modes(flight, reg), out)
    return out


def _attitude(
    axis: str,
    att: tuple[Array, Array, Array],
    counted: Sequence[Event],
    rules: Mapping[str, Any],
    tracking: Mapping[str, Any],
    out: Result,
) -> None:
    """The attitude tracking error over the same counted inputs as the rate's."""
    measured = [(e, attitude_error(att, e, rules)) for e in counted]
    kept = [(e, x) for e, x in measured if x is not None]
    if len(kept) < int(tracking["min_events"]):
        return
    errors = [100.0 * x for _, x in kept]
    low, high = _interval(errors, int(rules["seed"][axis]), tracking)
    mean = float(np.mean(errors))
    ev = Evidence(
        id=f"tune:attitude:{axis}",
        kind="metric",
        summary=(
            f"{axis} attitude tracking error {mean:.1f}% (95% interval {low:.1f}-{high:.1f}%) "
            f"over {len(kept)} stick inputs"
        ),
        messages=("ANG", "RATE", PID_MESSAGES[axis], "MODE", "CTUN", "RCIN"),
        fields=ATTITUDES[axis],
        segment=Segment("inputs", kept[0][0].start_us, kept[-1][0].end_us),
        values={
            "counted": len(kept),
            "too_small": len(measured) - len(kept),
            "errors_percent": [round(x, 1) for x in errors][:64],
        },
    )
    out.evidence.append(ev)
    out.metrics.append(
        MetricValue(
            f"tune.attitude_error_{axis}", "inputs", mean, None, ev.id,
            spread={"low": low, "high": high, "events": float(len(kept))},
        )
    )  # fmt: skip


def _offer(
    flight: FlightData, reg: Registry, flown: Sequence[tuple[int, int, str]], out: Result
) -> None:
    """A tune check measured on roll or pitch, with no AutoTune in the flight: AutoTune is
    ArduPilot's next step once the filters are set, and the next tune check measures it.
    On large propellers the rate filters go to 10 Hz first; with a payload that varies, the
    gain back-off is raised (rules/tune.json autotune_offer). One axis a pack is the
    procedure's to say: AUTOTUNE_AXES changes from pack to pack, so it is no change to keep."""
    measured = [m.metric.removeprefix("tune.tracking_error_") for m in out.metrics]
    axes = [a for a in ("roll", "pitch") if a in measured]
    if not axes or any(name == "AUTOTUNE" for _, _, name in flown):
        return
    rules: Mapping[str, Any] = reg.rules["tune"]["autotune_offer"]
    start: Mapping[str, Any] = reg.rules["audit"]["starting_tune"]
    p = flight.params
    wanted: dict[str, float] = {}
    prop = flight.answers.get("prop_diameter_in")
    large = isinstance(prop, int | float) and prop >= float(start["large_prop_in"])
    if large:
        rate = float(start["large_rate_filter_hz"])
        wanted |= {f"ATC_RAT_{a}_{f}": rate for a in ("RLL", "PIT") for f in ("FLTT", "FLTD")}
    if flight.answers.get("payload") == "variable":
        wanted["AUTOTUNE_GMBK"] = max(float(rules["gmbk_variable_payload"]),
                                      float(p.get("AUTOTUNE_GMBK", 0.25)))  # fmt: skip
    changes = tuple(
        CandidateChange(n, p.get(n), v, "autotune")
        for n, v in wanted.items()
        if n in p and abs(float(p[n]) - v) > 1e-6
    )
    ids = {f"tune:{a}" for a in axes} | {f"tune:attitude:{a}" for a in axes}
    cited = tuple(e for e in out.evidence if e.id in ids)
    out.findings.append(
        Finding(
            code="tune.autotune_offer",
            severity="info",
            confidence="medium",
            evidence=cited,
            params={"axes": ", ".join(axes), "large_props": bool(large)},
            changes=changes,
        )
    )


def _few(per_axis: Mapping[str, Sequence[Event]], rules: Mapping[str, Any], out: Result) -> None:
    """Inputs flown in a stick mode, enough on some axis for a tune check, but too few of
    them counted on roll or pitch: something the pilot can change on the next flight."""
    needed = int(rules["min_events"])
    tried = {
        a: [e for e in found if e.rejected not in ("mode", "mode change")]
        for a, found in per_axis.items()
    }
    counted = {a: sum(e.rejected is None for e in found) for a, found in per_axis.items()}
    attempted = any(len(found) >= needed for found in tried.values())
    short = [a for a in ("roll", "pitch") if a in counted and counted[a] < needed]
    if not attempted or not short:
        return
    every = [e for found in per_axis.values() for e in found]
    left_out = _counts(every)
    ev = Evidence(
        id="tune:inputs",
        kind="fact",
        summary=(
            "stick inputs counted: "
            + ", ".join(f"{a} {counted[a]} of {len(tried[a])}" for a in per_axis)
            + f"; {needed} needed on each axis"
        ),
        messages=("RATE", "PIDR", "PIDP", "PIDY", "MODE", "CTUN", "RCIN"),
        segment=Segment("inputs", min(e.start_us for e in every), max(e.end_us for e in every)),
        values={"counted": counted, "tried": {a: len(f) for a, f in tried.items()},
                "left_out": left_out},
    )  # fmt: skip
    out.findings.append(
        Finding(
            code="tune.few_inputs",
            severity="info",
            confidence="high",
            evidence=(ev,),
            params={
                "roll": counted.get("roll", 0),
                "pitch": counted.get("pitch", 0),
                "yaw": counted.get("yaw", 0),
                "needed": needed,
                "saturated": left_out["saturated"],
                "low": left_out["low"],
                "mode_change": left_out["mode change"],
                "gaps": left_out["log gap"],
                "no_stick": left_out["no stick"],
            },
        )
    )


TUNE = Analyzer(
    name="tune",
    version=3,
    run=_tune,
    metrics=(
        "tune.tracking_error_roll",
        "tune.tracking_error_pitch",
        "tune.tracking_error_yaw",
        "tune.attitude_error_roll",
        "tune.attitude_error_pitch",
    ),
    findings=("tune.few_inputs", "tune.autotune_offer"),
)
