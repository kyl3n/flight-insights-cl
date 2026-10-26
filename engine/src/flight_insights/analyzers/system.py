"""System health (spec: Analyzers common to all vehicles, "System health"; Processor load,
timing and latency).

Reads the performance monitor (PM), the coded errors (ERR) and events (EV):

- main-loop timing: the share of long loops (PM.NLon over PM.NL; ArduPilot counts a
  loop as long when it runs over 120% of its period) and the longest loop (PM.MaxT),
  leaving out the record that spans arming, when loops are always long;
- internal errors the firmware latched (PM.InE, PM.ErC);
- failsafes, crash and thrust-loss checks, and other errors logged while armed;
- the motors emergency-stopped in flight.

In the simulator, loop timing is the host computer's, not a flight controller's, so
timing is measured but not judged there. Error, event and internal-error names are
the release's own, from the knowledge pack.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import (
    SEVERITY_OF_ZONE,
    Analyzer,
    Evidence,
    Finding,
    MetricValue,
    Result,
    Segment,
)
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si

LONG_LOOP = 1.2  # ArduPilot's long-loop threshold, as a share of the loop period
CRASH_SUBSYSTEMS = ("CRASH_CHECK", "THRUST_LOSS_CHECK")
# Copter's CPU failsafe (ArduCopter/failsafe.cpp) logs as subsystem CPU: the main loop
# stalled and the motors were stopped.
FAILSAFE_SUBSYSTEMS = ("CPU",)
INTERNAL_SUBSYSTEM = "INTERNAL_ERROR"


def _words(name: str) -> str:
    return name.lower().replace("_", " ")


def _ints(table: Any, name: str) -> np.ndarray[Any, Any]:
    return np.asarray(table.column(name).to_numpy(), np.int64)


def _timing(flight: FlightData, reg: Registry, out: Result) -> bool:
    pm = flight.table("PM")
    if pm is None or pm.num_rows == 0 or not {"NLon", "NL", "MaxT", "LR"} <= set(pm.column_names):
        return False
    t = _ints(pm, "TimeUS")
    armed = np.flatnonzero((t > flight.arm_us) & (t <= flight.disarm_us))
    # Each record covers the time since the one before; the first after arming spans it.
    records = armed[1:]
    if not len(records):
        return True
    long_loops, loops = _ints(pm, "NLon")[records], _ints(pm, "NL")[records]
    max_us, rate = _ints(pm, "MaxT")[records], _ints(pm, "LR")[records]
    period_us = 1e6 / np.where(rate > 0, rate, np.nan)
    share = 100.0 * float(long_loops.sum()) / float(loops.sum()) if loops.sum() else float("nan")
    longest = 100.0 * float(np.nanmax(max_us / period_us))
    over = int((max_us > LONG_LOOP * period_us).sum())
    load = si.values(pm, "Load")[records] if "Load" in pm.column_names else np.empty(0)
    mem = _ints(pm, "Mem")[records] if "Mem" in pm.column_names else np.empty(0, np.int64)
    segment = Segment("flight", int(t[records[0]]), int(t[records[-1]]))
    judged = not flight.simulated
    ev = Evidence(
        id="sys:timing",
        kind="metric",
        summary=(
            f"{int(long_loops.sum())} long loops in {int(loops.sum())} over {len(records)} "
            f"performance records; longest loop {int(max_us.max())} us at {int(rate[0])} Hz"
            + ("" if judged else "; simulator timing, not judged")
        ),
        messages=("PM",),
        fields=("NLon", "NL", "MaxT", "LR", "Load", "Mem"),
        segment=segment,
        values={
            "records": len(records),
            "loops": int(loops.sum()),
            "long_loops": int(long_loops.sum()),
            "max_loop_us": int(max_us.max()),
            "loop_rate_hz": int(rate[0]),
            "records_over_120pct": over,
            "cpu_load_pct_p95": round(float(np.percentile(load, 95)), 1) if len(load) else None,
            "cpu_load_pct_max": round(float(load.max()), 1) if len(load) else None,
            "free_memory_min_bytes": int(mem.min()) if len(mem) else None,
            "simulated": not judged,
        },
    )
    out.evidence.append(ev)
    slow_zone = reg.metrics["system.slow_loops"].zone(share) if judged else None
    long_zone = reg.metrics["system.max_loop_time"].zone(longest) if judged else None
    out.metrics.append(MetricValue("system.slow_loops", "flight", share, slow_zone, ev.id))
    out.metrics.append(MetricValue("system.max_loop_time", "flight", longest, long_zone, ev.id))
    if slow_zone in ("caution", "out"):
        load_p95 = ev.values["cpu_load_pct_p95"]
        out.findings.append(
            Finding(
                code="system.slow_loops",
                severity=SEVERITY_OF_ZONE[slow_zone],
                confidence="high",
                evidence=(ev,),
                params={
                    "percent": round(share, 1),
                    "load": load_p95 if load_p95 is not None else "?",
                },
            )
        )
    if long_zone in ("caution", "out"):
        out.findings.append(
            Finding(
                code="system.long_loop",
                severity=SEVERITY_OF_ZONE[long_zone],
                confidence="high" if over > 1 else "medium",
                evidence=(ev,),
                params={"percent": round(longest), "over": over, "records": len(records)},
            )
        )
    return True


def _internal_errors(flight: FlightData, names: Mapping[str, Any], out: Result) -> bool:
    """Internal errors are latched from boot, so every record up to disarming counts."""
    pm = flight.table("PM")
    if pm is None or pm.num_rows == 0 or "InE" not in pm.column_names:
        return False
    t = _ints(pm, "TimeUS")
    upto = t <= flight.disarm_us
    if not upto.any():
        return True
    mask = int(np.bitwise_or.reduce(_ints(pm, "InE")[upto]))
    if not mask:
        return True
    bits: Mapping[str, str] = names.get("internal_errors", {})
    errors = [_words(bits.get(str(b), f"bit {b}")) for b in range(32) if mask >> b & 1]
    count = int(_ints(pm, "ErC")[upto].max()) if "ErC" in pm.column_names else None
    line = int(_ints(pm, "ErrL")[upto][-1]) if "ErrL" in pm.column_names else None
    ev = Evidence(
        id="sys:internal",
        kind="fact",
        summary=f"internal error mask 0x{mask:x}: {', '.join(errors)}",
        messages=("PM",),
        fields=("InE", "ErC", "ErrL"),
        segment=Segment("flight", int(t[upto][0]), flight.disarm_us),
        values={"mask": mask, "errors": errors, "count": count, "last_line": line},
    )
    out.findings.append(
        Finding(
            code="system.internal_error",
            severity="out",
            confidence="high",
            evidence=(ev,),
            params={"errors": ", ".join(errors)},
        )
    )
    return True


def _code_name(subsystem: str, code: int, codes: Mapping[str, list[str]]) -> str | None:
    """ArduPilot reuses error-code numbers across subsystems: the name that belongs to
    this subsystem, or None."""
    head = subsystem.split("_")[0].rstrip("S")
    for name in codes.get(str(code), []):
        if name.startswith(head):
            return _words(name)
    return None


def _general_name(code: int, codes: Mapping[str, list[str]]) -> str:
    """The general meaning of a code (LogErrorCode lists it first, for 1 and 4), or the
    number."""
    candidates = codes.get(str(code), [])
    return _words(candidates[0]) if code in (1, 4) and candidates else f"code {code}"


def error_name(subsystem: str, code: int, codes: Mapping[str, list[str]]) -> str:
    """What an error code means in its subsystem, in words."""
    return _code_name(subsystem, code, codes) or _general_name(code, codes)


def _after_arming(flight: FlightData, times: list[int]) -> list[float]:
    """Log times as seconds after arming: a log's own clock is no use to a reader, and a
    raw microsecond count is in the form of a scaled coordinate, which the AI packet's
    location check refuses."""
    return [round((t - flight.arm_us) / 1e6, 3) for t in times]


def _errors(flight: FlightData, names: Mapping[str, Any], pm_errors: bool, out: Result) -> bool:
    err = flight.table("ERR")
    if err is None or err.num_rows == 0:
        return False
    t, sub, code = _ints(err, "TimeUS"), _ints(err, "Subsys"), _ints(err, "ECode")
    armed = (t >= flight.arm_us) & (t <= flight.disarm_us) & (code != 0)  # 0: resolved
    subsystems: Mapping[str, str] = names.get("error_subsystems", {})
    groups: dict[tuple[int, int], list[int]] = {}
    for k in np.flatnonzero(armed):
        groups.setdefault((int(sub[k]), int(code[k])), []).append(int(t[k]))
    for (s, c), times in sorted(groups.items()):
        subsystem = subsystems.get(str(s), f"subsystem {s}")
        if subsystem == INTERNAL_SUBSYSTEM and pm_errors:
            continue  # PM names which internal errors they were
        codes: Mapping[str, list[str]] = names.get("error_codes", {})
        own = _code_name(subsystem, c, codes)
        what = own or _general_name(c, codes)
        first_s = round((times[0] - flight.arm_us) / 1e6, 1)
        ev = Evidence(
            id=f"sys:err:{s}:{c}",
            kind="fact",
            summary=f"ERR {subsystem} code {c} ({what}) {len(times)} times while armed",
            messages=("ERR",),
            fields=("Subsys", "ECode"),
            segment=Segment("flight", times[0], times[-1]),
            values={"subsystem": s, "code": c, "at_s": _after_arming(flight, times)},
        )
        if subsystem.startswith("FAILSAFE_") or subsystem in FAILSAFE_SUBSYSTEMS:
            finding = Finding(
                code="system.failsafe",
                severity="out",
                confidence="high",
                evidence=(ev,),
                params={
                    "subsystem": _words(subsystem.removeprefix("FAILSAFE_")),
                    "count": len(times),
                    "first_s": first_s,
                },
            )
        elif subsystem in CRASH_SUBSYSTEMS:
            finding = Finding(
                code="system.crash_check",
                severity="safety",
                confidence="high",
                evidence=(ev,),
                params={"what": own or _words(subsystem), "first_s": first_s},
            )
        else:
            finding = Finding(
                code="system.error",
                severity="caution",
                confidence="high",
                evidence=(ev,),
                params={
                    "subsystem": _words(subsystem),
                    "code": what,
                    "count": len(times),
                    "first_s": first_s,
                },
            )
        out.findings.append(finding)
    return True


def _events(flight: FlightData, names: Mapping[str, Any], out: Result) -> bool:
    ev_table = flight.table("EV")
    if ev_table is None or ev_table.num_rows == 0:
        return False
    events: Mapping[str, str] = names.get("events", {})
    stop = [int(n) for n, name in events.items() if name == "MOTORS_EMERGENCY_STOPPED"]
    t, ids = _ints(ev_table, "TimeUS"), _ints(ev_table, "Id")
    hit = np.flatnonzero((t >= flight.arm_us) & (t <= flight.disarm_us) & np.isin(ids, stop))
    if len(hit):
        first = int(t[hit[0]])
        ev = Evidence(
            id="sys:estop",
            kind="fact",
            summary=f"EV MOTORS_EMERGENCY_STOPPED {len(hit)} times while armed",
            messages=("EV",),
            fields=("Id",),
            segment=Segment("flight", first, int(t[hit[-1]])),
            values={"at_s": _after_arming(flight, [int(t[k]) for k in hit])},
        )
        out.findings.append(
            Finding(
                code="system.emergency_stop",
                severity="safety",
                confidence="high",
                evidence=(ev,),
                params={"first_s": round((first - flight.arm_us) / 1e6, 1)},
            )
        )
    return True


def _system(flight: FlightData, reg: Registry) -> Result:
    out = Result()
    names = reg.names(flight.firmware)
    timed = _timing(flight, reg, out)
    pm_errors = _internal_errors(flight, names, out)
    coded = _errors(flight, names, pm_errors, out)
    events = _events(flight, names, out)
    if not (timed or pm_errors or coded or events):
        out.skipped = "the log has no PM, ERR or EV messages"
    return out


SYSTEM = Analyzer(
    name="system",
    version=2,
    run=_system,
    metrics=("system.slow_loops", "system.max_loop_time"),
    findings=(
        "system.slow_loops",
        "system.long_loop",
        "system.internal_error",
        "system.failsafe",
        "system.crash_check",
        "system.error",
        "system.emergency_stop",
    ),
)
