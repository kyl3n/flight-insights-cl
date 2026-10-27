"""AutoTune review (spec: Copter analyzers, "Autotune review"): what each AutoTune session
in a flight did and how it ended.

- **Sessions** are the spans in AutoTune mode (`MODE`), to the next mode change or disarm.
- **Events** (`EV`, named from the release's enum table): AUTOTUNE_INITIALISED, OFF (left
  before it finished: the original gains come back), SUCCESS (every axis in
  AUTOTUNE_AXES done), FAILED, REACHED_LIMIT, PILOT_TESTING and SAVEDGAINS (logged at
  disarm, once the finished axes' gains are kept).
- **Axes and steps** (`ATUN`): each row is one gain update, with its axis, step and the
  rate P and D being tried. An axis is finished when a later axis starts, or when the
  session succeeds. Its last row holds the tuned rate P and D (on yaw the RD column is
  the yaw rate filter, not D).

Findings: the gains saved, or finished but not saved; a session stopped part-way; a
failure; a gain limit reached during the rate steps (a limit in the angle steps is
ordinary); rate D left at AUTOTUNE_MIN_D at the end of the D search; and an ESC losing
sync during a session, or, on motors under 500 KV, no ESC RPM to tell.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import Analyzer, Evidence, Finding, Result, Segment
from flight_insights.analyzers.motors_health import desync_events, motors
from flight_insights.analyzers.registry import Registry

Array = np.ndarray[Any, Any]
EVENTS = ("AUTOTUNE_INITIALISED", "AUTOTUNE_OFF", "AUTOTUNE_SUCCESS", "AUTOTUNE_FAILED",
          "AUTOTUNE_REACHED_LIMIT", "AUTOTUNE_PILOT_TESTING", "AUTOTUNE_SAVEDGAINS")  # fmt: skip


@dataclass
class Axis:
    name: str
    rows: int = 0
    steps: list[str] = field(default_factory=list[str])
    rate_p: float | None = None  # the last tried
    rate_d: float | None = None  # rate D, or on yaw the rate filter
    d_search_end: float | None = None  # rate D at the end of the D-down step
    finished: bool = False


@dataclass
class Session:
    start_us: int
    end_us: int
    events: dict[str, list[int]] = field(default_factory=dict[str, list[int]])
    axes: dict[str, Axis] = field(default_factory=dict[str, Axis])
    limits: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])  # axis, step

    def happened(self, event: str) -> bool:
        return bool(self.events.get(event))


def _times(table: Any) -> Array:
    return np.asarray(table.column("TimeUS").to_numpy(), np.int64)


def _sessions(flight: FlightData, mode_number: int) -> list[Session]:
    table = flight.table("MODE", ["TimeUS", "ModeNum"])
    if table is None or table.num_rows == 0:
        return []
    t = _times(table)
    n = np.asarray(table.column("ModeNum").to_numpy(), np.int64)
    out: list[Session] = []
    for i in np.flatnonzero(n == mode_number):
        if i > 0 and n[i - 1] == mode_number:
            continue  # the same mode logged again
        later = np.flatnonzero((t > t[i]) & (n != mode_number))
        end = int(t[later[0]]) if len(later) else flight.disarm_us
        out.append(Session(int(t[i]), min(end, flight.disarm_us)))
    return out


def _events(flight: FlightData, names: Mapping[str, int]) -> list[tuple[int, str]]:
    table = flight.table("EV", ["TimeUS", "Id"])
    if table is None or table.num_rows == 0:
        return []
    by_id = {v: k for k, v in names.items()}
    ids = np.asarray(table.column("Id").to_numpy(), np.int64)
    return [
        (int(t), by_id[int(i)]) for t, i in zip(_times(table), ids, strict=True) if int(i) in by_id
    ]


def _read(flight: FlightData, s: Session, rules: Mapping[str, Any]) -> None:
    atun = flight.table("ATUN")
    if atun is not None and atun.num_rows:
        t = _times(atun)
        inside = (t >= s.start_us) & (t <= s.end_us)
        axis = np.asarray(atun.column("Axis").to_numpy(), np.int64)[inside]
        step = np.asarray(atun.column("TuneStep").to_numpy(), np.int64)[inside]
        rp = np.asarray(atun.column("RP").to_numpy(), np.float64)[inside]
        rd = np.asarray(atun.column("RD").to_numpy(), np.float64)[inside]
        times = t[inside]
        order: list[str] = []
        for k in range(len(times)):
            name = rules["axes"].get(str(int(axis[k])), f"axis {int(axis[k])}")
            a = s.axes.setdefault(name, Axis(name))
            if name not in order:
                order.append(name)
            a.rows += 1
            label = rules["steps"].get(str(int(step[k])), f"step {int(step[k])}")
            if label not in a.steps:
                a.steps.append(label)
            a.rate_p, a.rate_d = float(rp[k]), float(rd[k])
            if int(step[k]) == int(rules["d_down_step"]):
                a.d_search_end = float(rd[k])
        for earlier in order[:-1]:
            s.axes[earlier].finished = True  # a later axis started
        for when in s.events.get("AUTOTUNE_REACHED_LIMIT", []):
            before = np.flatnonzero(times <= when)
            if len(before):
                k = int(before[-1])
                s.limits.append(
                    (rules["axes"].get(str(int(axis[k])), "?"),
                     rules["steps"].get(str(int(step[k])), "?"))
                )  # fmt: skip
    if s.happened("AUTOTUNE_SUCCESS"):
        for a in s.axes.values():
            a.finished = True


def _gains(flight: FlightData, s: Session, rules: Mapping[str, Any]) -> dict[str, Any]:
    """Each finished axis's rate P and D before (the flight's parameters) and as tuned."""
    out: dict[str, Any] = {}
    for a in s.axes.values():
        if not a.finished or a.rate_p is None:
            continue
        prefix = f"ATC_RAT_{rules['axis_params'].get(a.name, '?')}"
        p_before = flight.params.get(f"{prefix}_P")
        row: dict[str, Any] = {"p_before": p_before, "p_after": round(a.rate_p, 5)}
        if p_before:
            row["p_change_pct"] = round(100.0 * (a.rate_p / float(p_before) - 1.0), 1)
        if a.name != "yaw":  # yaw's RD is its rate filter
            row["d_before"] = flight.params.get(f"{prefix}_D")
            row["d_after"] = None if a.rate_d is None else round(a.rate_d, 6)
        out[a.name] = row
    return out


def _autotune(flight: FlightData, reg: Registry) -> Result:
    if not flight.logged:
        return Result(skipped="a parameter file has no flight to review")
    if flight.vehicle != "copter":  # Plane's and QuadPlane's AutoTune: Phase 3
        return Result(skipped="the AutoTune review is for Copter")
    rules: Mapping[str, Any] = reg.rules["autotune"]
    enums = reg.pack.enums(flight.firmware or "4.7.0")
    modes: Mapping[str, str] = enums["modes"].get("copter", {})
    mode_number = next((int(k) for k, v in modes.items() if v == "AUTOTUNE"), None)
    names = {v: int(k) for k, v in enums["events"].items() if v in EVENTS}
    if mode_number is None:
        return Result(skipped="the release's mode table has no AUTOTUNE")
    sessions = _sessions(flight, mode_number)
    if not sessions:
        return Result(skipped="no AutoTune in this flight")
    events = _events(flight, names)
    window = int(float(rules["save_window_s"]) * 1e6)
    for i, s in enumerate(sessions):
        until = sessions[i + 1].start_us if i + 1 < len(sessions) else s.end_us + window
        for when, name in events:
            if s.start_us <= when <= min(s.end_us + window, until):
                s.events.setdefault(name, []).append(when)
        _read(flight, s, rules)

    out = Result()
    min_d = float(flight.params.get("AUTOTUNE_MIN_D", 0.0005))
    axes_wanted = int(flight.params.get("AUTOTUNE_AXES", 7))
    for i, s in enumerate(sessions):
        _review(flight, rules, i, s, min_d, axes_wanted, out)
    _desyncs(flight, rules, sessions, out)
    return out


def _review(
    flight: FlightData,
    rules: Mapping[str, Any],
    i: int,
    s: Session,
    min_d: float,
    axes_wanted: int,
    out: Result,
) -> None:
    finished = [a.name for a in s.axes.values() if a.finished]
    gains = _gains(flight, s, rules)
    saved = s.happened("AUTOTUNE_SAVEDGAINS")
    outcome = (
        "failed" if s.happened("AUTOTUNE_FAILED")
        else "finished" if s.happened("AUTOTUNE_SUCCESS")
        else "stopped" if s.happened("AUTOTUNE_OFF")
        else "ended"
    )  # fmt: skip
    wanted = [
        n for bit, n in ((1, "roll"), (2, "pitch"), (4, "yaw"), (8, "yaw_d")) if axes_wanted & bit
    ]
    remaining = [n for n in wanted if n not in finished]
    ev = Evidence(
        id=f"autotune:{i + 1}",
        kind="fact",
        summary=(
            f"AutoTune session {i + 1}: {outcome} after "
            f"{(s.end_us - s.start_us) / 1e6:.0f} s; finished "
            f"{', '.join(finished) or 'no axis'}; gains {'saved' if saved else 'not saved'}"
        ),
        messages=("MODE", "EV", "ATUN"),
        fields=("ModeNum", "Id", "Axis", "TuneStep", "RP", "RD"),
        segment=Segment("autotune", s.start_us, s.end_us),
        values={
            "outcome": outcome,
            "saved": saved,
            "finished": finished,
            "remaining": remaining,
            "tested_by_pilot": s.happened("AUTOTUNE_PILOT_TESTING"),
            "steps": {a.name: a.steps for a in s.axes.values()},
            "gains": gains,
            "limits": [f"{axis}: {step}" for axis, step in s.limits],
            "aggr": flight.params.get("AUTOTUNE_AGGR"),
            "gmbk": flight.params.get("AUTOTUNE_GMBK"),
            "min_d": min_d,
        },
    )
    out.evidence.append(ev)

    def add(code: str, severity: Any, **params: Any) -> None:
        out.findings.append(Finding(code, severity, "high", (ev,), params))

    if finished and saved:
        add("autotune.saved", "info", axes=", ".join(finished))
    elif finished:
        add("autotune.not_saved", "caution", axes=", ".join(finished))
    if outcome in ("stopped", "ended") and remaining:
        add("autotune.incomplete", "info", remaining=", ".join(remaining))
    if outcome == "failed":
        last = next(reversed(s.axes.values()), None)
        add("autotune.failed", "caution", axis=last.name if last else "?",
            step=last.steps[-1] if last and last.steps else "?")  # fmt: skip
    rate_steps = {rules["steps"][str(k)] for k in rules["rate_steps"]}
    for axis, step in s.limits:
        if step in rate_steps:
            add("autotune.rate_limit", "caution", axis=axis, step=step)
            break
    floor = min_d * (1.0 + float(rules["min_d_margin"]))
    at_floor = [
        a.name for a in s.axes.values()
        if a.name != "yaw" and a.d_search_end is not None and a.d_search_end <= floor
    ]  # fmt: skip
    if at_floor:
        add("autotune.d_at_min", "caution", axes=", ".join(at_floor))


def _desyncs(
    flight: FlightData, rules: Mapping[str, Any], sessions: Sequence[Session], out: Result
) -> None:
    ms = motors(flight.params)
    events = desync_events(flight, ms) if ms else None
    kv = flight.answers.get("motor_kv")
    if events is None:
        if isinstance(kv, int | float) and 0 < kv < float(rules["low_kv"]):
            ev = Evidence(
                id="autotune:kv",
                kind="fact",
                summary=f"Motor KV {kv:g} from the platform setup; no ESC RPM in the log",
                values={"motor_kv": kv, "low_kv": rules["low_kv"]},
            )
            out.evidence.append(ev)
            out.findings.append(
                Finding("autotune.low_kv_unchecked", "info", "high", (ev,), {"kv": round(kv)})
            )
        return
    during = [
        e for e in events
        if any(s.start_us <= int(e["at_us"]) <= s.end_us for s in sessions)
    ]  # fmt: skip
    if not during:
        return
    ev = Evidence(
        id="autotune:desync",
        kind="fact",
        summary=(
            "RPM collapsed under a hover-level command during AutoTune on motor(s) "
            + ", ".join(str(e["motor"]) for e in during)
        ),
        messages=("ESC", "RCOU"),
        fields=("RPM",),
        segment=Segment("autotune", sessions[0].start_us, sessions[-1].end_us),
        values={"events": during, "motor_kv": kv},
    )
    out.evidence.append(ev)
    out.findings.append(
        Finding(
            "autotune.desync",
            "out",
            "medium",
            (ev,),
            {"motors": ", ".join(str(e["motor"]) for e in during)},
        )
    )


AUTOTUNE = Analyzer(
    name="autotune",
    version=1,
    run=_autotune,
    metrics=(),
    findings=(
        "autotune.saved",
        "autotune.not_saved",
        "autotune.incomplete",
        "autotune.failed",
        "autotune.rate_limit",
        "autotune.d_at_min",
        "autotune.desync",
        "autotune.low_kv_unchecked",
    ),
)
