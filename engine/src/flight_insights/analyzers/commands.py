"""Commands the aircraft received and what it answered, and how the operator answered
critical warnings (spec: Links, ground station and whole-system faults, "Ground station
and operator").

- **Commands** come from the onboard MAVC record: ArduPilot logs every COMMAND_LONG and
  COMMAND_INT it handles, with its result and who sent it. Only the command, the result
  and the sender are read; its parameters and coordinates never are (rules/scrubber.json
  denies them on the AI path too).
- **Commands with no reply** need the ground station's own packets in the tlog (Mission
  Planner records them; QGroundControl records only what it receives). MAVC then says
  whether each one reached the aircraft and only its reply was lost.
- **Warnings** are the tlog's STATUSTEXT at warning severity or worse, while armed, which
  is where ArduPilot announces its failsafes. Their text stays on the laptop: only their
  times are used. The operator's next action is a
  command from the ground station, or a mode change the pilot or ground station asked for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import Analyzer, Evidence, Finding, MetricValue, Result
from flight_insights.analyzers.registry import Registry
from flight_insights.tlog.names import command_name

Array = np.ndarray[Any, Any]


def _ints(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.int64)


def _param(flight: FlightData, name: str, fallback: float) -> float:
    return float(flight.params.get(name, flight.defaults.get(name, fallback)))


@dataclass(frozen=True)
class Commands:
    """The MAVC record in time order."""

    times: Array
    cmd: Array
    result: Array
    sender: Array  # the source system ID


def _mavc(flight: FlightData) -> Commands | None:
    table = flight.table("MAVC", ["TimeUS", "Cmd", "Res", "SS"])
    if table is None or table.num_rows == 0:
        return None
    t = _ints(table, "TimeUS")
    order = np.argsort(t, kind="stable")
    return Commands(t[order], _ints(table, "Cmd")[order], _ints(table, "Res")[order],
                    _ints(table, "SS")[order])  # fmt: skip


def _from_station(flight: FlightData, sender: Array) -> Array:
    """Whether each sender is the ground station, by MAV_GCS_SYSID (to MAV_GCS_SYSID_HI)."""
    low = int(_param(flight, "MAV_GCS_SYSID", 255))
    high = max(low, int(_param(flight, "MAV_GCS_SYSID_HI", low)))
    return (sender >= low) & (sender <= high)


def _who(flight: FlightData, sender: int) -> str:
    if bool(_from_station(flight, np.array([sender]))[0]):
        return "the ground station"
    if sender == int(_param(flight, "MAV_SYSID", 1)):
        return "the aircraft's own components"
    return "another system"


def _after_arming(flight: FlightData, t: int) -> float:
    return round((t - flight.arm_us) / 1e6, 1)


def _refused(
    flight: FlightData, reg: Registry, rules: Mapping[str, Any], c: Commands, out: Result
) -> None:
    results: Mapping[str, str] = rules["results"]
    refused = np.isin(c.result, rules["refused"])
    unsupported = ~refused & (c.result != 0) & np.isin(c.result, [3, 7, 8])
    armed = (c.times >= flight.arm_us) & (c.times <= flight.disarm_us)
    hits = np.flatnonzero(refused & armed)
    values: dict[str, Any] = {
        "refused": len(hits),
        "not_supported": int(np.count_nonzero(unsupported & armed)),
        "commands": int(np.count_nonzero(armed)),
    }
    first: dict[str, Any] = {}
    if len(hits):
        k = int(hits[0])
        first = {
            "command": command_name(int(c.cmd[k])),
            "result": results.get(str(int(c.result[k])), str(int(c.result[k]))),
            "sender": _who(flight, int(c.sender[k])),
            "at_s": _after_arming(flight, int(c.times[k])),
        }
        values |= {"first_at_s": first["at_s"]}
    ev = Evidence(
        id="command:in_flight",
        kind="fact",
        summary=(
            f"MAVC while armed: {values['commands']} commands, {len(hits)} refused,"
            f" {values['not_supported']} not supported"
        ),
        messages=("MAVC",),
        fields=("Cmd", "Res", "SS"),
        segment=flight.armed,
        values=values,
    )
    out.evidence.append(ev)
    metric = "command.refused_in_flight"
    m = MetricValue(metric, "flight", float(len(hits)), reg.metrics[metric].zone(len(hits)), ev.id)
    out.metrics.append(m)
    if first:
        out.findings.append(Finding(metric, "caution", "high", (ev,), {"count": len(hits)} | first))
    _arming_refused(flight, rules, c, refused, out)


def _arming_refused(
    flight: FlightData, rules: Mapping[str, Any], c: Commands, refused: Array, out: Result
) -> None:
    """Arm commands refused before this flight armed: the arm-or-disarm command refused
    while disarmed, which can only have been asked to arm."""
    since = flight.arm_us - round(float(rules["arming_lookback_s"]) * 1e6)
    k = np.flatnonzero(
        refused
        & (c.cmd == int(rules["arm_disarm"]))
        & (c.times >= since)
        & (c.times < flight.arm_us)
    )
    if not len(k):
        return
    waited = round((flight.arm_us - int(c.times[k[0]])) / 1e6, 1)
    ev = Evidence(
        id="command:arming",
        kind="fact",
        summary=f"MAVC: arming refused {len(k)} times, the first {waited} s before it armed",
        messages=("MAVC",),
        fields=("Cmd", "Res"),
        segment=flight.armed,
        values={"refused": len(k), "waited_s": waited},
    )
    out.evidence.append(ev)
    out.findings.append(Finding("command.arming_refused", "info", "high", (ev,), {
        "count": len(k), "waited_s": waited,
    }))  # fmt: skip


# --- commands with no reply --------------------------------------------------------------


def _station_commands(flight: FlightData) -> list[tuple[int, int]]:
    """(time, command) of each command the ground station sent the aircraft, as the tlog
    recorded it, on the flight's clock."""
    out: list[tuple[int, int]] = []
    for name in ("COMMAND_LONG", "COMMAND_INT"):
        for session, table in flight.tlog(name, ["command", "target_system"]):
            if table.num_rows == 0:
                continue
            t = np.asarray(table.column("TimeUS").to_numpy(zero_copy_only=False), np.float64)
            mine = (
                (_ints(table, "src_sysid") != session["sysid"])
                & (_ints(table, "target_system") == session["sysid"])
                & np.isfinite(t)
            )  # fmt: skip
            out += [(int(a), int(b)) for a, b in zip(t[mine], _ints(table, "command")[mine],
                                                       strict=True)]  # fmt: skip
    return sorted(out)


def _acks(flight: FlightData) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for session, table in flight.tlog("COMMAND_ACK", ["command"]):
        if table.num_rows == 0:
            continue
        t = np.asarray(table.column("TimeUS").to_numpy(zero_copy_only=False), np.float64)
        mine = (_ints(table, "src_sysid") == session["sysid"]) & np.isfinite(t)
        out += [(int(a), int(b)) for a, b in zip(t[mine], _ints(table, "command")[mine],
                                                   strict=True)]  # fmt: skip
    return sorted(out)


def _groups(sent: Sequence[tuple[int, int]], resend_us: int) -> list[tuple[int, int, int]]:
    """(first, last, command): a command and its resends, each within `resend_us` of the
    one before."""
    out: list[list[int]] = []
    open_: dict[int, list[int]] = {}
    for t, cmd in sent:
        g = open_.get(cmd)
        if g is not None and t - g[1] <= resend_us:
            g[1] = t
        else:
            g = [t, t, cmd]
            open_[cmd] = g
            out.append(g)
    return [(a, b, c) for a, b, c in out]


def _unanswered(
    flight: FlightData, rules: Mapping[str, Any], c: Commands | None, out: Result
) -> None:
    sent = _station_commands(flight)
    if not sent:
        return
    timeout = round(float(rules["ack_timeout_s"]) * 1e6)
    since = flight.arm_us - round(float(rules["arming_lookback_s"]) * 1e6)
    acks = _acks(flight)
    lost: list[tuple[int, int]] = []
    never: list[tuple[int, int]] = []
    groups = [g for g in _groups(sent, round(float(rules["resend_s"]) * 1e6))
              if since <= g[0] <= flight.disarm_us]  # fmt: skip
    for first, last, cmd in groups:
        if any(a_cmd == cmd and first <= t <= last + timeout for t, a_cmd in acks):
            continue
        reached = c is not None and bool(np.any(
            (c.cmd == cmd) & (c.times >= first - timeout) & (c.times <= last + timeout)
        ))  # fmt: skip
        (lost if reached else never).append((first, cmd))
    missing = sorted(lost + never)
    ev = Evidence(
        id="command:replies",
        kind="fact",
        summary=(
            f"tlog: {len(groups)} commands from the ground station, {len(missing)} with no reply"
            f" ({len(lost)} reached the aircraft, {len(never)} did not)"
        ),
        messages=("COMMAND_LONG", "COMMAND_INT", "COMMAND_ACK", "MAVC"),
        fields=("command", "Cmd"),
        segment=flight.armed,
        values={
            "sent": len(groups),
            "unanswered": len(missing),
            "lost_replies": len(lost),
            "never_arrived": len(never),
        },
    )
    out.evidence.append(ev)
    if missing:
        t, cmd = missing[0]
        out.findings.append(Finding("command.unanswered", "caution", "high", (ev,), {
            "count": len(missing), "command": command_name(cmd),
            "at_s": _after_arming(flight, t), "lost": len(lost), "never": len(never),
        }))  # fmt: skip


# --- warnings and the operator's answer --------------------------------------------------


def _warnings(flight: FlightData, rules: Mapping[str, Any]) -> list[int]:
    """When each warning came while armed, repeats of one within a few seconds
    counted once. Only severity and time are read; the text stays where it is."""
    times: list[int] = []
    for session, table in flight.tlog("STATUSTEXT", ["severity"]):
        if table.num_rows == 0:
            continue
        t = np.asarray(table.column("TimeUS").to_numpy(zero_copy_only=False), np.float64)
        keep = (
            (_ints(table, "src_sysid") == session["sysid"])
            & (_ints(table, "severity") <= int(rules["warning_severity"]))
            & np.isfinite(t) & (t >= flight.arm_us) & (t <= flight.disarm_us)
        )  # fmt: skip
        times += [int(x) for x in t[keep]]
    repeat = round(float(rules["warning_repeat_s"]) * 1e6)
    out: list[int] = []
    for t in sorted(times):
        if not out or t - out[-1] > repeat:
            out.append(t)
    return out


def _operator_actions(
    flight: FlightData, reg: Registry, rules: Mapping[str, Any], c: Commands | None
) -> Array:
    times: list[int] = []
    if c is not None:
        times += [int(t) for t in c.times[_from_station(flight, c.sender)]]
    mode = flight.table("MODE", ["TimeUS", "Rsn"])
    if mode is not None and mode.num_rows and "Rsn" in mode.column_names:
        reasons: Mapping[str, str] = reg.names(flight.firmware).get("mode_reasons", {})
        wanted = set(rules["operator_reasons"])
        rsn, t = _ints(mode, "Rsn"), _ints(mode, "TimeUS")
        times += [int(t[k]) for k in range(len(t)) if reasons.get(str(int(rsn[k]))) in wanted]
    return np.array(sorted(times), np.int64)


def _responses(
    flight: FlightData, reg: Registry, rules: Mapping[str, Any], c: Commands | None, out: Result
) -> None:
    warnings = _warnings(flight, rules)
    if not warnings:
        return
    actions = _operator_actions(flight, reg, rules, c)
    within = float(rules["response_s"])
    waits: list[float | None] = []
    for w in warnings:
        later = actions[(actions >= w) & (actions <= flight.disarm_us)]
        waits.append(round((int(later[0]) - w) / 1e6, 1) if len(later) else None)
    slow = [w for w, wait in zip(warnings, waits, strict=True) if wait is None or wait > within]
    answered = [wait for wait in waits if wait is not None]
    ev = Evidence(
        id="operator:warnings",
        kind="fact",
        summary=(
            f"tlog: {len(warnings)} warnings while armed; the operator acted on"
            f" {len(answered)}" + (f", within {max(answered)} s at most" if answered else "")
        ),
        messages=("STATUSTEXT", "MAVC", "MODE"),
        fields=("severity", "SS", "Rsn"),
        segment=flight.armed,
        values={
            "warnings": len(warnings),
            "answered": len(answered),
            "longest_s": max(answered) if answered else None,
            "unanswered": len(slow),
        },
    )
    out.evidence.append(ev)
    if slow:
        out.findings.append(Finding("operator.unanswered_warning", "caution", "medium", (ev,), {
            "count": len(slow), "within_s": f"{within:g}", "at_s": _after_arming(flight, slow[0]),
        }))  # fmt: skip


def _commands(flight: FlightData, reg: Registry) -> Result:
    if not flight.logged:
        return Result(skipped="a parameter file has no flight to judge")
    rules: Mapping[str, Any] = reg.rules["commands"]
    out = Result()
    c = _mavc(flight)
    if c is not None:
        _refused(flight, reg, rules, c, out)
    _unanswered(flight, rules, c, out)
    _responses(flight, reg, rules, c, out)
    return out


COMMANDS = Analyzer(
    name="commands",
    version=1,
    run=_commands,
    metrics=("command.refused_in_flight",),
    findings=(
        "command.refused_in_flight",
        "command.arming_refused",
        "command.unanswered",
        "operator.unanswered_warning",
    ),
)
