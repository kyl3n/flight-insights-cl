"""A flight's abnormal events on one clock, and the first fault that explains the rest
(spec: Links, ground station and whole-system faults, "Whole-system incident timeline").

The events: telemetry link outages (no packet from the vehicle in any tlog for more than
2 s), the ground station unheard (from 0.4 of FS_GCS_TIMEOUT, timed from when it was last
heard), the receiver's own failsafe flag and failsafe throttle, the battery under its low
voltage, GNSS fix lost, parameters changed in flight, commands refused, errors (ERR) and
the mode changes failsafes make.

The main event is the flight's first failsafe or crash check while armed, else its
first error. Walking back from it, each step takes the nearest earlier event the rule
table (rules/incident.json) names as a possible cause within its window, so the chain
keeps every step; the first fault is where it starts. Walking
forward, the events the rules tie to the main event are the ones it explains. Order and
timing are evidence, not proof, so the finding's confidence is medium. Positions are
never read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.failsafe_onset import (
    MONITORS,
    last_seen,
    low_throttle,
    receiver_flagged,
)
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import Analyzer, Evidence, Finding, Result
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash.formats import text
from flight_insights.knowledge import KnowledgeError
from flight_insights.params.dialect import Dialect

Array = np.ndarray[Any, Any]
REFUSED = (1, 2, 4, 9)  # MAV_RESULT: temporarily rejected, denied, failed, frame
# the main event: the first failsafe or crash check, else the first error
MAIN = ("error:FAILSAFE_", "error:CRASH_CHECK")


@dataclass(frozen=True)
class Event:
    t_us: int
    kind: str  # rules/incident.json's names: "link_outage", "error:FAILSAFE_GCS", ...
    end_us: int | None = None  # for one that lasts
    detail: Mapping[str, Any] = field(default_factory=dict[str, Any])  # stays local
    messages: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()


def _ints(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.int64)


def _floats(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.float64)


def _param(flight: FlightData, name: str, fallback: float = 0.0) -> float:
    return float(flight.params.get(name, flight.defaults.get(name, fallback)))


def _runs(times: Array, mask: Array) -> list[tuple[int, int]]:
    """(first, last) time of each run of True."""
    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.astype(np.int8), [0]))))
    return [(int(times[a]), int(times[b - 1])) for a, b in zip(edges[::2], edges[1::2],
                                                                 strict=True)]  # fmt: skip


# --- the events --------------------------------------------------------------------------


def link_outages(flight: FlightData, over_s: float) -> list[Event]:
    """Gaps of more than `over_s` with no packet from the vehicle in any tlog."""
    times: list[Array] = []
    for session, packets in flight.tlog_packets():
        t = np.asarray(packets.column("TimeUS").to_numpy(zero_copy_only=False), np.float64)
        mine = (
            (_ints(packets, "src_sysid") == session["sysid"])
            & (_ints(packets, "src_compid") == session["compid"])
            & np.isfinite(t)
        )  # fmt: skip
        times.append(t[mine].astype(np.int64))
    if not times:
        return []
    t = np.unique(np.concatenate(times))
    if len(t) < 2:
        return []
    gaps = np.flatnonzero(np.diff(t) > over_s * 1e6)
    return [Event(int(t[k]), "link_outage", int(t[k + 1]),
                  {"seconds": round(float(t[k + 1] - t[k]) / 1e6, 1)}) for k in gaps]  # fmt: skip


def gcs_unheard(flight: FlightData, share: float) -> list[Event]:
    """Each time the ground station went unheard for `share` of FS_GCS_TIMEOUT or more,
    timed from when it was last heard."""
    mav = flight.table("MAV")
    if mav is None or mav.num_rows == 0 or "mgs" not in mav.column_names:
        return []
    t = _ints(mav, "TimeUS")
    order = np.argsort(t, kind="stable")
    t = t[order]
    seen = last_seen(t, _ints(mav, "chan")[order], _ints(mav, "mgs")[order])
    timeout = _param(flight, "FS_GCS_TIMEOUT", 5.0) * 1e6
    out: list[Event] = []
    for a, b in _runs(t, (seen > 0) & (t - seen >= share * timeout)):
        k = int(np.searchsorted(t, a))
        start = int(seen[k])
        if out and out[-1].t_us == start:
            continue
        out.append(Event(start, "gcs_unheard", b, {"seconds": round((b - start) / 1e6, 1)},
                         ("MAV",), ("mgs",)))  # fmt: skip
    return out


def _receiver(flight: FlightData) -> list[Event]:
    out: list[Event] = []
    flagged = receiver_flagged(flight)
    if flagged is not None:
        t, flag = flagged
        out += [Event(a, "receiver_flag", b, {}, ("RCI2",), ("Flags",)) for a, b in _runs(t, flag)]
    low = low_throttle(flight)
    if low is not None:
        t, under, what = low
        out += [Event(a, "receiver_throttle", b, {"what": what}, ("RCIN",), ())
                for a, b in _runs(t, under)]  # fmt: skip
    return out


def battery_low(flight: FlightData) -> list[Event]:
    """Each spell the voltage the battery failsafe uses was under BATT_LOW_VOLT."""
    bat = flight.table("BAT")
    if bat is None or bat.num_rows == 0 or "Inst" not in bat.column_names:
        return []
    out: list[Event] = []
    for inst in np.unique(_ints(bat, "Inst")):
        if not 0 <= inst < len(MONITORS):
            continue
        prefix = MONITORS[int(inst)]
        limit = _param(flight, f"{prefix}_LOW_VOLT")
        field_ = "VoltR" if _param(flight, f"{prefix}_FS_VOLTSRC") == 1 else "Volt"
        if limit <= 0 or field_ not in bat.column_names or _param(flight, f"{prefix}_MONITOR") == 0:
            continue
        rows = _ints(bat, "Inst") == inst
        t, v = _ints(bat, "TimeUS")[rows], _floats(bat, field_)[rows]
        out += [Event(a, "battery_low", b, {"monitor": prefix, "under": limit}, ("BAT",), (field_,))
                for a, b in _runs(t, (v > 0) & (v < limit))]  # fmt: skip
    return out


def gps_lost(flight: FlightData) -> list[Event]:
    """The first receiver's fix falling below 3D after it had one."""
    gps = flight.table("GPS", ["TimeUS", "Status", "I"])
    if gps is None or gps.num_rows == 0 or "Status" not in gps.column_names:
        return []
    rows = _ints(gps, "I") == 0 if "I" in gps.column_names else np.ones(gps.num_rows, bool)
    t, status = _ints(gps, "TimeUS")[rows], _ints(gps, "Status")[rows]
    had = np.maximum.accumulate(status >= 3) if len(status) else status
    return [Event(a, "gps_lost", b, {}, ("GPS",), ("Status",))
            for a, b in _runs(t, had & (status < 3))]  # fmt: skip


def _counter(flight: FlightData, reg: Registry) -> Callable[[str], bool]:
    """Whether a parameter is one of the statistics the autopilot counts up on its own
    (STAT_*): it changes in flight with no one setting it, so it is no event."""
    try:
        dialect = Dialect(reg.pack, flight.vehicle, flight.firmware or "0")  # type: ignore[arg-type]
    except (KnowledgeError, KeyError):
        return lambda _name: False
    return lambda name: dialect.param_class(name) == "counter"


def params_changed(flight: FlightData, reg: Registry) -> list[Event]:
    """Parameters set while armed: the firmware logs PARM again when one changes. The
    autopilot's own counters are left out."""
    parm = flight.table("PARM")
    if parm is None or parm.num_rows == 0 or "Name" not in parm.column_names:
        return []
    t = _ints(parm, "TimeUS")
    raw: list[Any] = parm.column("Name").to_pylist()
    names: list[str] = [text(n) if isinstance(n, bytes) else str(n) for n in raw]
    values = _floats(parm, "Value")
    counter = _counter(flight, reg)
    last: dict[str, float] = {}
    out: list[Event] = []
    for k in np.argsort(t, kind="stable"):
        name, value = names[int(k)], float(values[k])
        before = last.get(name)
        last[name] = value
        if (
            flight.arm_us <= t[k] <= flight.disarm_us and before is not None and before != value
            and not counter(name)
        ):  # fmt: skip
            change = {"name": name, "from": before, "to": value}
            out.append(Event(int(t[k]), "param_changed", None, change, ("PARM",), ()))
    return out


def errors(flight: FlightData, names: Mapping[str, Any]) -> list[Event]:
    """Each error the firmware raised (ERR with a non-zero code), until it was resolved."""
    err = flight.table("ERR", ["TimeUS", "Subsys", "ECode"])
    if err is None or err.num_rows == 0:
        return []
    subsystems: Mapping[str, str] = names.get("error_subsystems", {})
    t, sub, code = _ints(err, "TimeUS"), _ints(err, "Subsys"), _ints(err, "ECode")
    out: list[Event] = []
    open_: dict[int, int] = {}
    for k in np.argsort(t, kind="stable"):
        s = int(sub[k])
        if code[k] != 0:
            open_[len(out)] = s
            kind = f"error:{subsystems.get(str(s), f'SUBSYS_{s}')}"
            detail = {"code": int(code[k])}
            out.append(Event(int(t[k]), kind, None, detail, ("ERR",), ("Subsys", "ECode")))
        else:
            for i in [i for i, ss in open_.items() if ss == s]:
                e = out[i]
                out[i] = Event(e.t_us, e.kind, int(t[k]), e.detail, e.messages, e.fields)
                del open_[i]
    return out


def failsafe_modes(flight: FlightData, names: Mapping[str, Any]) -> list[Event]:
    """Mode changes a failsafe or check made (any reason but the pilot's or the GCS's)."""
    table = flight.table("MODE")
    if table is None or table.num_rows == 0 or "Rsn" not in table.column_names:
        return []
    reasons: Mapping[str, str] = names.get("mode_reasons", {})
    modes: Mapping[str, str] = names.get("modes", {}).get("copter", {})
    t, n, r = _ints(table, "TimeUS"), _ints(table, "ModeNum"), _ints(table, "Rsn")
    out: list[Event] = []
    for k in np.argsort(t, kind="stable"):
        reason = reasons.get(str(int(r[k])), "UNKNOWN")
        if reason in ("RC_COMMAND", "GCS_COMMAND", "UNKNOWN", "INITIALISED"):
            continue
        out.append(Event(int(t[k]), f"mode:{reason}", None,
                         {"mode": modes.get(str(int(n[k])), f"MODE_{int(n[k])}")},
                         ("MODE",), ("ModeNum", "Rsn")))  # fmt: skip
    return out


def commands_refused(flight: FlightData) -> list[Event]:
    mavc = flight.table("MAVC", ["TimeUS", "Cmd", "Res"])
    if mavc is None or mavc.num_rows == 0:
        return []
    t, cmd, res = _ints(mavc, "TimeUS"), _ints(mavc, "Cmd"), _ints(mavc, "Res")
    keep = np.isin(res, REFUSED) & (t >= flight.arm_us) & (t <= flight.disarm_us)
    return [
        Event(int(t[k]), "command_refused", None,
              {"command": int(cmd[k]), "result": int(res[k])}, ("MAVC",), ("Cmd", "Res"))
        for k in np.flatnonzero(keep)
    ]  # fmt: skip


def events(flight: FlightData, reg: Registry) -> list[Event]:
    """Every abnormal event from a minute before arming to disarming, in time order."""
    rules: Mapping[str, Any] = reg.rules["incident"]
    names = reg.names(flight.firmware)
    found = (
        link_outages(flight, float(rules["outage_s"]))
        + gcs_unheard(flight, float(rules["silence_share"]))
        + _receiver(flight) + battery_low(flight) + gps_lost(flight) + params_changed(flight, reg)
        + errors(flight, names) + failsafe_modes(flight, names) + commands_refused(flight)
    )  # fmt: skip
    since = flight.arm_us - round(float(rules["lookback_s"]) * 1e6)
    return sorted((e for e in found if since <= e.t_us <= flight.disarm_us),
                  key=lambda e: (e.t_us, e.kind))  # fmt: skip


# --- the first fault ---------------------------------------------------------------------


@dataclass(frozen=True)
class Incident:
    main: Event
    chain: tuple[Event, ...]  # from the first fault to the main event
    explained: tuple[Event, ...]  # later events the main event leads to


def _window(rules: Sequence[Sequence[Any]], cause: str, effect: str) -> float | None:
    found = [float(w) for c, e, w in rules if c == cause and e == effect]
    return max(found) if found else None


def first_fault(
    found: Sequence[Event], arm_us: int, rules: Sequence[Sequence[Any]]
) -> Incident | None:
    armed = [e for e in found if e.kind.startswith("error:") and e.t_us >= arm_us]
    if not armed:
        return None
    main = next((e for e in armed if e.kind.startswith(MAIN)), armed[0])
    chain = [main]
    while True:
        here = chain[0]
        causes = [
            c for c in found
            if c is not here and c not in chain and c.t_us <= here.t_us
            and (w := _window(rules, c.kind, here.kind)) is not None
            and here.t_us - c.t_us <= w * 1e6
        ]  # fmt: skip
        if not causes:
            break
        chain.insert(0, max(causes, key=lambda c: c.t_us))  # the nearest step back
    explained: list[Event] = []
    reach = [main]
    for e in found:
        if e.t_us < main.t_us or e in chain:
            continue
        if any((w := _window(rules, r.kind, e.kind)) is not None and 0 <= e.t_us - r.t_us <= w * 1e6
               for r in reach):  # fmt: skip
            explained.append(e)
            reach.append(e)
    return Incident(main, tuple(chain), tuple(explained))


def words(rules: Mapping[str, Any], e: Event, lang: str = "en") -> str:
    """The event in words, in `lang` (the rule pack words each in English and Ukrainian);
    a mode change a failsafe made is its mode and reason, and an error with no words of
    its own is its subsystem's name."""
    known: Mapping[str, Mapping[str, str]] = rules["words"]
    if e.kind in known:
        return known[e.kind][lang]
    if e.kind.startswith("mode:"):
        why = e.kind.removeprefix("mode:").lower().replace("_", " ")
        template: str = rules["mode_words"][lang]
        return template.format(mode=e.detail.get("mode", "?"), reason=why)
    return f"ERR {e.kind.removeprefix('error:')}" if e.kind.startswith("error:") else e.kind


def _incident(flight: FlightData, reg: Registry) -> Result:
    if not flight.logged:
        return Result(skipped="a parameter file has no flight to judge")
    rules: Mapping[str, Any] = reg.rules["incident"]
    found = events(flight, reg)
    incident = first_fault(found, flight.arm_us, rules["rules"])
    if incident is None:
        return Result()

    def at(e: Event) -> float:
        return round((e.t_us - flight.arm_us) / 1e6, 1)

    chain = incident.chain
    ev = Evidence(
        id="incident:chain",
        kind="fact",
        summary=" → ".join(f"{words(rules, e)} at {at(e)} s" for e in chain)
        + (f"; then {len(incident.explained)} more it explains" if incident.explained else ""),
        messages=tuple(sorted({m for e in chain for m in e.messages})),
        fields=tuple(sorted({f for e in chain for f in e.fields})),
        segment=flight.armed,
        values={
            "chain": [e.kind for e in chain],
            "at_s": [at(e) for e in chain],
            "explained": len(incident.explained),
        },
    )
    params = {
        "first": words(rules, chain[0]), "first_at_s": at(chain[0]),
        "main": words(rules, incident.main), "main_at_s": at(incident.main),
        "chain": " → ".join(words(rules, e) for e in chain),
        "explained": len(incident.explained),
    }  # fmt: skip
    if len(chain) > 1:
        finding = Finding("incident.first_fault", "caution", "medium", (ev,), params)
    else:
        finding = Finding("incident.unexplained", "info", "medium", (ev,), params)
    return Result(evidence=[ev], findings=[finding])


INCIDENT = Analyzer(
    name="incident",
    version=1,
    run=_incident,
    metrics=(),
    findings=("incident.first_fault", "incident.unexplained"),
)
