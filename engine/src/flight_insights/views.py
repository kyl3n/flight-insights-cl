"""What the propulsion and power view and the system and buses view draw for one flight
(spec: User interface, "Propulsion and power", "System and buses").

The analyzers judge each flight from full-rate samples. These views only draw it, so
each trace is reduced for display to at most `BINS` time bins, each keeping its lowest
and highest sample so that no spike drops out of the picture (spec: "Plotting at log
scale"). Units come from the log's own FMTU, never assumed. Nothing here reads a
position or leaves the laptop.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.incident import Event, events, first_fault, words
from flight_insights.analyzers.motors_health import motors
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.system import error_name
from flight_insights.dataflash import si
from flight_insights.knowledge import Pack
from flight_insights.tlog.names import command_name

BINS = 600  # display bins per trace; the screen is narrower than this
MARGIN_US = 5_000_000  # shown either side of the armed span

Floats = np.ndarray[tuple[int], np.dtype[np.float64]]


def envelope(
    t_s: Floats, values: Floats, start_s: float, end_s: float, bins: int = BINS
) -> dict[str, list[float]]:
    """The lowest and highest sample in each of up to `bins` equal time bins between
    start and end, at the bin's centre; empty bins are left out and non-finite samples
    ignored. A trace with no more samples than bins is returned as it is."""
    keep = (t_s >= start_s) & (t_s <= end_s) & np.isfinite(values)
    t, v = t_s[keep], values[keep]
    if len(t) <= bins:
        r = [round(float(x), 4) for x in v]
        return {"t": [round(float(x), 3) for x in t], "lo": r, "hi": r}
    edges = np.linspace(start_s, end_s, bins + 1)
    which = np.clip(np.searchsorted(edges, t, side="right") - 1, 0, bins - 1)
    order = np.argsort(which, kind="stable")
    which, v = which[order], v[order]
    starts = np.flatnonzero(np.r_[True, which[1:] != which[:-1]])
    lo = np.minimum.reduceat(v, starts)
    hi = np.maximum.reduceat(v, starts)
    centres = (edges[which[starts]] + edges[which[starts] + 1]) / 2
    return {
        "t": [round(float(x), 3) for x in centres],
        "lo": [round(float(x), 4) for x in lo],
        "hi": [round(float(x), 4) for x in hi],
    }


def _window(flight: FlightData) -> tuple[float, float]:
    return (flight.arm_us - MARGIN_US) / 1e6, (flight.disarm_us + MARGIN_US) / 1e6


def _shown(flight: FlightData, span: tuple[float, float] | None) -> tuple[float, float]:
    """The stretch drawn: the whole window, or the part of it asked for, so a zoomed
    stretch gets its own bins (and every sample, when it holds fewer than the bins)."""
    start, end = _window(flight)
    if span is None:
        return start, end
    a, b = max(span[0], start), min(span[1], end)
    if not a < b:
        raise ValueError("the window asked for is outside the flight")
    return a, b


def _median(values: Floats, sel: np.ndarray[Any, Any]) -> float | None:
    picked = values[sel & np.isfinite(values)]
    return round(float(np.median(picked)), 3) if len(picked) else None


def _inside(t_us: np.ndarray[Any, Any], spans: list[tuple[int, int]]) -> np.ndarray[Any, Any]:
    sel = np.zeros(len(t_us), bool)
    for a, b in spans:
        sel |= (t_us >= a) & (t_us <= b)
    return sel


def _ints(table: pa.Table, name: str) -> np.ndarray[tuple[int], np.dtype[np.int64]]:
    return np.asarray(table.column(name).to_numpy(), np.int64)


def _times(table: pa.Table) -> np.ndarray[tuple[int], np.dtype[np.int64]]:
    return _ints(table, "TimeUS")


def _context(flight: FlightData, pack: Pack) -> dict[str, Any]:
    """What both views share: the window, the armed span, hover windows and modes."""
    start, end = _window(flight)
    modes: list[dict[str, Any]] = []
    table = flight.table("MODE", ["TimeUS", "ModeNum"])
    if table is not None and table.num_rows and flight.firmware:
        names: Mapping[str, str] = pack.enums(flight.firmware)["modes"].get(flight.vehicle, {})
        for t_us, n in zip(_times(table), _ints(table, "ModeNum"), strict=True):
            name = names.get(str(n), f"MODE_{n}")
            # a repeated request for the same mode is no change
            if start <= t_us / 1e6 <= end and (not modes or modes[-1]["mode"] != name):
                modes.append({"t": round(int(t_us) / 1e6, 3), "mode": name})
    return {
        "window_s": [round(start, 3), round(end, 3)],
        "armed_s": [round(flight.arm_us / 1e6, 3), round(flight.disarm_us / 1e6, 3)],
        "hover_s": [[round(w.start_us / 1e6, 3), round(w.end_us / 1e6, 3)] for w in flight.hover()],
        "modes": modes,
        "simulated": flight.simulated,
    }


def propulsion(
    flight: FlightData, pack: Pack, span: tuple[float, float] | None = None
) -> dict[str, Any]:
    """Per-motor output and, from ESC telemetry, RPM, temperature, current and supply
    voltage; the battery's voltage, current, power and resistance. In the simulator the
    ESC's temperature, current and voltage are placeholders and are left out."""
    out = _context(flight, pack)
    start, end = _shown(flight, span)
    out["shown_s"] = [round(start, 3), round(end, 3)]
    hover = [(w.start_us, w.end_us) for w in flight.hover()]
    ms = motors(flight.params)
    low = float(flight.params.get("MOT_PWM_MIN", 0)) or float(flight.params.get("RC3_MIN", 1000))
    high = float(flight.params.get("MOT_PWM_MAX", 0)) or float(flight.params.get("RC3_MAX", 2000))
    pwm_range = high - low
    out["saturation_pct"] = round(100.0 * float(flight.params.get("MOT_SPIN_MAX", 0.95)), 1)
    rcou, esc = flight.table("RCOU"), flight.table("ESC")
    esc_fields = ("RPM",) if flight.simulated else ("RPM", "Temp", "Curr", "Volt")
    units: dict[str, str] = {"output": "%"}
    rows: list[dict[str, Any]] = []
    for m in ms:
        row: dict[str, Any] = {"number": m.number, "channel": m.channel}
        name = f"C{m.channel}"
        if rcou is not None and name in rcou.column_names and pwm_range > 0:
            t_us = _times(rcou)
            pct = (si.values(rcou, name) - low) / pwm_range * 100.0
            row["output"] = envelope(t_us / 1e6, pct, start, end)
            row["hover_output"] = _median(pct, _inside(t_us, hover))
        if esc is not None and esc.num_rows:
            t_us = _times(esc)
            mine = np.asarray(esc.column("Instance").to_numpy(), np.int64) == m.channel - 1
            for f in esc_fields:
                if f not in esc.column_names or not mine.any():
                    continue
                v = si.values(esc, f)
                row[f.lower()] = envelope(t_us[mine] / 1e6, v[mine], start, end)
                row[f"hover_{f.lower()}"] = _median(v[mine], _inside(t_us[mine], hover))
                units[f.lower()] = si.unit(esc, f)
        if "temp" in row:
            temps = np.asarray(row["temp"]["hi"], np.float64)
            row["peak_temp"] = round(float(temps.max()), 1) if len(temps) else None
        rows.append(row)
    out["motors"] = rows
    out["battery"] = _battery(flight, start, end, units)
    out["units"] = units
    return out


def _battery(
    flight: FlightData, start: float, end: float, units: dict[str, str]
) -> dict[str, Any] | None:
    bat = flight.table("BAT")
    if bat is None or bat.num_rows == 0 or not {"Volt", "Curr"} <= set(bat.column_names):
        return None
    t_us = _times(bat)
    inst = (
        np.asarray(bat.column("Inst").to_numpy(), np.int64)
        if "Inst" in bat.column_names
        else np.zeros(len(t_us), np.int64)
    )
    primary = inst == int(np.min(inst))  # the battery the failsafes watch
    t_s = t_us[primary] / 1e6
    volt, curr = si.values(bat, "Volt")[primary], si.values(bat, "Curr")[primary]
    units |= {"volt": si.unit(bat, "Volt"), "curr": si.unit(bat, "Curr")}
    out: dict[str, Any] = {
        "volt": envelope(t_s, volt, start, end),
        "curr": envelope(t_s, curr, start, end),
    }
    if si.unit(bat, "Volt") == "V" and si.unit(bat, "Curr") == "A":
        out["power"] = envelope(t_s, volt * curr, start, end)
        units["power"] = "W"
    if "Res" in bat.column_names:
        res = si.values(bat, "Res")[primary]
        res = np.where(res > 0, res, np.nan)
        if np.isfinite(res).any():
            out["res"] = envelope(t_s, res, start, end)
            units["res"] = si.unit(bat, "Res")
    rested = volt[t_us[primary] < flight.arm_us]
    out["resting_volt"] = round(float(np.median(rested[-20:])), 3) if len(rested) else None
    armed = (t_us[primary] >= flight.arm_us) & (t_us[primary] <= flight.disarm_us)
    out["lowest_volt"] = round(float(volt[armed].min()), 3) if armed.any() else None
    return out


def system(
    flight: FlightData, pack: Pack, span: tuple[float, float] | None = None
) -> dict[str, Any]:
    """Main-loop load and timing from the performance monitor, logging drops, and the
    coded errors the firmware logged, by subsystem name from the knowledge pack."""
    out = _context(flight, pack)
    start, end = _shown(flight, span)
    out["shown_s"] = [round(start, 3), round(end, 3)]
    units: dict[str, str] = {}
    pm = flight.table("PM")
    if pm is not None and pm.num_rows:
        t_s = _times(pm) / 1e6
        for name, key in (("Load", "load"), ("MaxT", "max_loop"), ("LR", "loop_rate")):
            if name in pm.column_names:
                out[key] = envelope(t_s, si.values(pm, name), start, end)
                units[key] = si.unit(pm, name)
        if {"NLon", "NL"} <= set(pm.column_names):
            nl = si.values(pm, "NL")
            share = np.where(
                nl > 0, si.values(pm, "NLon") / np.where(nl > 0, nl, 1) * 100.0, np.nan
            )
            out["long_loops"] = envelope(t_s, share, start, end)
            units["long_loops"] = "%"
    rate = float(flight.params.get("SCHED_LOOP_RATE", 0))
    out["loop_period_us"] = round(1e6 / rate, 1) if rate > 0 else None
    dsf = flight.table("DSF", ["TimeUS", "Dp"])
    if dsf is not None and dsf.num_rows:
        # Dp counts from the start of the log file: drawn from arming, as the logging
        # analyzer counts it.
        t_us = _times(dsf)
        dp = np.maximum.accumulate(_ints(dsf, "Dp")).astype(np.float64)
        before = dp[t_us < flight.arm_us]
        base = before[-1] if len(before) else dp[0]
        out["log_dropped"] = envelope(t_us / 1e6, np.maximum(dp - base, 0.0), start, end)
    out["errors"] = _errors(flight, pack, start, end)
    out["units"] = units
    return out


def _errors(flight: FlightData, pack: Pack, start: float, end: float) -> list[dict[str, Any]]:
    err = flight.table("ERR")
    if err is None or err.num_rows == 0 or not flight.firmware:
        return []
    enums = pack.enums(flight.firmware)
    subsystems: Mapping[str, str] = enums.get("error_subsystems", {})
    codes: Mapping[str, list[str]] = enums.get("error_codes", {})
    out: list[dict[str, Any]] = []
    for t_us, sub, code in zip(_times(err), _ints(err, "Subsys"), _ints(err, "ECode"), strict=True):
        if start <= t_us / 1e6 <= end:
            name = subsystems.get(str(sub), f"subsystem {sub}")
            out.append(
                {"t": round(int(t_us) / 1e6, 3), "subsystem": name, "code": int(code),
                 "meaning": error_name(name, int(code), codes)}
            )  # fmt: skip
    return out


# --- the incident timeline -----------------------------------------------------------------


def _after(flight: FlightData, t_us: float) -> float:
    return round((t_us - flight.arm_us) / 1e6, 2)


def _spans(flight: FlightData, t: np.ndarray[Any, Any], values: list[Any]) -> list[dict[str, Any]]:
    """Runs of equal values as {from_s, to_s, value}, in seconds after arming."""
    out: list[dict[str, Any]] = []
    for k in range(len(t)):
        if out and out[-1]["value"] == values[k]:
            out[-1]["to_s"] = _after(flight, float(t[k]))
            continue
        out.append({"from_s": _after(flight, float(t[k])), "to_s": _after(flight, float(t[k])),
                    "value": values[k]})  # fmt: skip
    return out


def timeline(
    flight: FlightData, pack: Pack, span: tuple[float, float] | None = None
) -> dict[str, Any]:
    """The incident timeline's lanes (spec: Whole-system incident timeline), in seconds
    after arming, with the first fault that explains the rest. Warning text is the
    ground station's and stays on this laptop. `span` is not used: the timeline is
    always the whole flight."""
    del span
    reg = Registry.load(pack)
    rules: Mapping[str, Any] = reg.rules["incident"]
    results: Mapping[str, str] = reg.rules["commands"]["results"]
    names = reg.names(flight.firmware)
    found = events(flight, reg)

    def event(e: Event) -> dict[str, Any]:
        out: dict[str, Any] = {"at_s": _after(flight, e.t_us), "kind": e.kind,
                               "words": words(rules, e)}  # fmt: skip
        if e.end_us is not None:
            out["to_s"] = _after(flight, e.end_us)
        return out | dict(e.detail)

    lanes: dict[str, Any] = {
        "link": [event(e) for e in found if e.kind in ("link_outage", "gcs_unheard")],
        "receiver": [event(e) for e in found if e.kind.startswith("receiver_")],
        "errors": [event(e) for e in found if e.kind.startswith("error:")],
        "battery": [event(e) for e in found if e.kind == "battery_low"],
        "gnss_lost": [event(e) for e in found if e.kind == "gps_lost"],
        "params": [event(e) for e in found if e.kind == "param_changed"],
    }
    mode = flight.table("MODE")
    if mode is not None and mode.num_rows:
        modes: Mapping[str, str] = names.get("modes", {}).get("copter", {})
        reasons: Mapping[str, str] = names.get("mode_reasons", {})
        t = _times(mode)
        rsn = _ints(mode, "Rsn") if "Rsn" in mode.column_names else np.zeros(len(t), np.int64)
        lanes["mode"] = [
            {"at_s": _after(flight, float(t[k])),
             "mode": modes.get(str(int(n)), f"MODE_{int(n)}"),
             "reason": reasons.get(str(int(rsn[k])), "UNKNOWN")}
            for k, n in enumerate(_ints(mode, "ModeNum"))
        ]  # fmt: skip
    rci2 = flight.table("RCI2")
    if rci2 is not None and rci2.num_rows and "OMask" in rci2.column_names:
        overridden = [bool(x) for x in _ints(rci2, "OMask") != 0]
        lanes["pilot_overrides"] = [s for s in _spans(flight, _times(rci2), overridden)
                                    if s["value"]]  # fmt: skip
    mavc = flight.table("MAVC", ["TimeUS", "Cmd", "Res", "SS"])
    if mavc is not None and mavc.num_rows:
        lanes["commands"] = [
            {"at_s": _after(flight, float(t_us)), "command": command_name(int(cmd)),
             "result": results.get(str(int(res)), str(int(res))), "sender": int(ss)}
            for t_us, cmd, res, ss in zip(_times(mavc), _ints(mavc, "Cmd"), _ints(mavc, "Res"),
                                          _ints(mavc, "SS"), strict=True)
        ]  # fmt: skip
    warnings: list[dict[str, Any]] = []
    for session, table in flight.tlog("STATUSTEXT", ["severity", "text"]):
        t = np.asarray(table.column("TimeUS").to_numpy(zero_copy_only=False), np.float64)
        sev = _ints(table, "severity")
        text = table.column("text").to_pylist()
        mine = _ints(table, "src_sysid") == session["sysid"]
        warnings += [{"at_s": _after(flight, float(t[k])), "severity": int(sev[k]),
                      "text": str(text[k])}
                     for k in np.flatnonzero(mine & np.isfinite(t) & (sev <= 4))]  # fmt: skip
    lanes["warnings"] = sorted(warnings, key=lambda w: w["at_s"])
    gps = flight.table("GPS", ["TimeUS", "Status", "I"])
    if gps is not None and gps.num_rows and "Status" in gps.column_names:
        rows = _ints(gps, "I") == 0 if "I" in gps.column_names else np.ones(gps.num_rows, bool)
        lanes["gnss_fix"] = _spans(flight, _times(gps)[rows],
                                   [int(s) for s in _ints(gps, "Status")[rows]])  # fmt: skip
    incident = first_fault(found, flight.arm_us, rules["rules"])
    first: dict[str, Any] | None = None
    if incident is not None:
        first = {
            "chain": [event(e) for e in incident.chain],
            "main": event(incident.main),
            "explained": [event(e) for e in incident.explained],
        }
    return {
        "flight_id": flight.id,
        "armed_s": _after(flight, flight.disarm_us),
        "lanes": lanes,
        "first_fault": first,
    }
