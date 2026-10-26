"""Motor and ESC health (spec: Analyzers common to all vehicles, "Motor and ESC health";
ESC and motor health; Reference range library, "Propulsion and power").

From the motor outputs (RCOU) and ESC telemetry (ESC, EDT2):

- saturation: a motor at its maximum output, in steady hover (out of range: a
  hardware fix) or briefly in flight;
- output spread in hover: motors working against each other, from a CG offset, a
  twisted motor or yaw imbalance, with the highest and lowest motor named;
- desync or stall: a motor's RPM collapsing while its command stays up;
- extended DShot telemetry status bits (alert, warning, error: demag, desync, stall);
- ESC temperature against its siblings, and the supply voltage each ESC sees;
- current per RPM squared in hover, a load figure judged against the motor's history.

Motors are found from SERVOn_FUNCTION (Motor1 to Motor12); ESC telemetry instance n is
output channel n + 1. In the simulator, ESC temperature, voltage and current are
placeholders, so those checks are left out there.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa

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

MOTOR_FUNCTIONS = {**{33 + k: k + 1 for k in range(8)}, **{82 + k: k + 9 for k in range(4)}}
SATURATED_MARGIN = 0.01  # within 1% of the range below the top counts as saturated
HOT_SIBLING_C = 15.0  # an ESC this much hotter than its siblings' median
SUPPLY_DROP_V = 0.3  # an ESC seeing this much less voltage than its siblings
DESYNC_SHARE = 0.5  # RPM falling below this share of the motor's hover RPM...
DESYNC_UPDATES = 3  # ...for this many updates in a row, under a hover-level command,
DESYNC_RUNNING_US = 500_000  # after it held above that share this long
EDT2_ALERT, EDT2_WARNING, EDT2_ERROR = 1 << 2, 1 << 3, 1 << 4  # log_Edt2_Status


@dataclass(frozen=True)
class Motor:
    number: int  # Motor1, Motor2...
    channel: int  # output channel, 1-based: RCOU.C<channel>, ESC instance channel - 1


def motors(params: Mapping[str, float]) -> list[Motor]:
    found: list[Motor] = []
    for channel in range(1, 33):
        function = int(params.get(f"SERVO{channel}_FUNCTION", 0))
        if function in MOTOR_FUNCTIONS:
            found.append(Motor(MOTOR_FUNCTIONS[function], channel))
    return sorted(found, key=lambda m: m.number)


def _times(t: pa.Table) -> np.ndarray[tuple[int], np.dtype[np.int64]]:
    return np.asarray(t.column("TimeUS").to_numpy(), np.int64)


def _inside(times: np.ndarray[Any, Any], spans: list[tuple[int, int]]) -> np.ndarray[Any, Any]:
    sel = np.zeros(len(times), bool)
    for a, b in spans:
        sel |= (times >= a) & (times <= b)
    return sel


def _outputs(flight: FlightData, reg: Registry, ms: list[Motor], out: Result) -> None:
    rcou = flight.table("RCOU")
    names = [f"C{m.channel}" for m in ms]
    if rcou is None or rcou.num_rows == 0 or not set(names) <= set(rcou.column_names):
        return
    low = float(flight.params.get("MOT_PWM_MIN", 0)) or float(flight.params.get("RC3_MIN", 1000))
    high = float(flight.params.get("MOT_PWM_MAX", 0)) or float(flight.params.get("RC3_MAX", 2000))
    span = high - low
    if span <= 0:
        return
    top = low + span * float(flight.params.get("MOT_SPIN_MAX", 0.95))
    t = _times(rcou)
    pwm = np.column_stack([np.asarray(rcou.column(n).to_numpy(), np.float64) for n in names])
    armed = (t >= flight.arm_us) & (t <= flight.disarm_us)
    hover = flight.hover()
    held = _inside(t, [(w.start_us, w.end_us) for w in hover])
    saturated = (pwm >= top - SATURATED_MARGIN * span).any(axis=1)
    flight_pct = 100.0 * float(saturated[armed].mean()) if armed.any() else 0.0
    ev = Evidence(
        id="motors:outputs",
        kind="metric",
        summary=f"{len(ms)} motor outputs between {low:.0f} and {high:.0f}; top {top:.0f}",
        messages=("RCOU",),
        fields=tuple(names),
        segment=flight.armed,
        values={"pwm_min": low, "pwm_max": high, "saturation_pwm": round(top, 1)},
    )
    out.evidence.append(ev)
    zone = reg.metrics["motors.saturation_flight"].zone(flight_pct)
    out.metrics.append(MetricValue("motors.saturation_flight", "flight", flight_pct, zone, ev.id))
    worst_zone, worst_where = zone, "flight"
    if held.any():
        hover_pct = 100.0 * float(saturated[held].mean())
        hz = reg.metrics["motors.saturation_hover"].zone(hover_pct)
        out.metrics.append(MetricValue("motors.saturation_hover", "hover", hover_pct, hz, ev.id))
        if hz == "out":
            worst_zone, worst_where = hz, "hover"
        medians = np.median(pwm[held], axis=0)
        spread = 100.0 * float(medians.max() - medians.min()) / span
        sz = reg.metrics["motors.output_spread"].zone(spread)
        spread_ev = Evidence(
            id="motors:spread",
            kind="metric",
            summary=f"hover outputs {medians.min():.0f} to {medians.max():.0f} across motors",
            messages=("RCOU",),
            fields=tuple(names),
            segment=Segment("hover", hover[0].start_us, hover[-1].end_us),
            values={
                f"motor{m.number}": round(float(v), 1) for m, v in zip(ms, medians, strict=True)
            },
        )
        out.evidence.append(spread_ev)
        out.metrics.append(MetricValue("motors.output_spread", "hover", spread, sz, spread_ev.id))
        if sz in ("caution", "out"):
            out.findings.append(
                Finding(
                    code="motors.output_spread",
                    severity=SEVERITY_OF_ZONE[sz],
                    confidence="high",
                    evidence=(spread_ev,),
                    params={
                        "percent": round(spread, 1),
                        "highest": ms[int(np.argmax(medians))].number,
                        "lowest": ms[int(np.argmin(medians))].number,
                    },
                )
            )
    if worst_zone in ("caution", "out"):
        out.findings.append(
            Finding(
                code="motors.saturation",
                severity=SEVERITY_OF_ZONE[worst_zone],
                confidence="high",
                evidence=(ev,),
                params={"where": worst_where, "percent": round(flight_pct, 1)},
            )
        )


def _first_run(flags: np.ndarray[Any, Any], length: int) -> int | None:
    """Where the first run of at least `length` consecutive True values starts."""
    edges = np.diff(np.concatenate([[0], flags.astype(np.int8), [0]]))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    long = np.flatnonzero(ends - starts >= length)
    return int(starts[long[0]]) if len(long) else None


def _held(
    t: np.ndarray[Any, Any], above: np.ndarray[Any, Any], span_us: int
) -> np.ndarray[Any, Any]:
    """True from the first moment `above` has held for `span_us` without a break."""
    edges = np.diff(np.concatenate([[0], above.astype(np.int8), [0]]))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    out = np.zeros(len(t), dtype=bool)
    for a, b in zip(starts, ends, strict=True):
        reached = np.flatnonzero(t[a:b] - t[a] >= span_us)
        if len(reached):
            out[a + int(reached[0]) :] = True
            break
    return out


def _desync(flight: FlightData, ms: list[Motor], out: Result) -> None:
    esc, rcou = flight.table("ESC"), flight.table("RCOU")
    hover = flight.hover()
    if esc is None or rcou is None or not hover or "RPM" not in esc.column_names:
        return
    spans = [(w.start_us, w.end_us) for w in hover]
    et, inst, rpm = (
        _times(esc),
        np.asarray(esc.column("Instance").to_numpy(), np.int64),
        si.values(esc, "RPM"),
    )
    rt = _times(rcou)
    events: list[dict[str, Any]] = []
    for m in ms:
        column = f"C{m.channel}"
        if column not in rcou.column_names:
            continue
        mine = inst == m.channel - 1
        if not mine.any():
            continue
        t, r = et[mine], rpm[mine]
        held = _inside(t, spans)
        if not (held & (r > 0)).any():
            continue  # this ESC reports no RPM
        hover_rpm = float(np.median(r[held & (r > 0)]))
        pwm = np.asarray(rcou.column(column).to_numpy(), np.float64)
        hover_pwm = float(np.median(pwm[_inside(rt, spans)]))
        command = pwm[np.clip(np.searchsorted(rt, t, side="right") - 1, 0, len(rt) - 1)]
        armed = (t >= flight.arm_us) & (t <= flight.disarm_us)
        # A collapse, not a spool-up: the motor held above the share before it fell. Large
        # propellers take seconds to reach speed while take-off already commands more, and
        # a spool-up on the ground can read in bursts (ArduPilot's simulator swings between
        # zero and full speed until the aircraft lifts off), so one reading above is not
        # enough: the speed must hold there for a while.
        running = _held(t, armed & (r >= DESYNC_SHARE * hover_rpm), DESYNC_RUNNING_US)
        low = armed & running & (command >= hover_pwm) & (r < DESYNC_SHARE * hover_rpm)
        start = _first_run(low, DESYNC_UPDATES)
        if start is not None:
            events.append(
                {
                    "motor": m.number,
                    "at_us": int(t[start]),
                    "rpm": float(r[start]),
                    "hover_rpm": hover_rpm,
                }
            )
    if not events:
        return
    ev = Evidence(
        id="esc:desync",
        kind="fact",
        summary=(
            "RPM collapsed under a hover-level command on motor(s) "
            + ", ".join(str(e["motor"]) for e in events)
        ),
        messages=("ESC", "RCOU"),
        fields=("RPM",),
        segment=flight.armed,
        values={"events": events},
    )
    first = min(events, key=lambda e: e["at_us"])
    out.findings.append(
        Finding(
            code="esc.desync",
            severity="out",
            confidence="medium",
            evidence=(ev,),
            params={
                "motors": ", ".join(str(e["motor"]) for e in events),
                "first_s": round((first["at_us"] - flight.arm_us) / 1e6, 1),
            },
        )
    )


def _edt2(flight: FlightData, ms: list[Motor], out: Result) -> None:
    edt = flight.table("EDT2")
    if edt is None or edt.num_rows == 0 or "Status" not in edt.column_names:
        return
    t = _times(edt)
    armed = (t >= flight.arm_us) & (t <= flight.disarm_us)
    inst = np.asarray(edt.column("Instance").to_numpy(), np.int64)
    status = np.asarray(edt.column("Status").to_numpy(), np.int64)
    by_channel = {m.channel - 1: m.number for m in ms}
    counts: dict[int, dict[str, int]] = {}
    for bit, name in ((EDT2_ALERT, "alert"), (EDT2_WARNING, "warning"), (EDT2_ERROR, "error")):
        for i in np.unique(inst[armed & (status & bit > 0)]):
            sel = armed & (inst == i) & (status & bit > 0)
            motor = by_channel.get(int(i), int(i) + 1)
            counts.setdefault(motor, {})[name] = int(sel.sum())
    stress = {}
    if "MaxStress" in edt.column_names:
        max_stress = np.asarray(edt.column("MaxStress").to_numpy(), np.int64)
        for i in np.unique(inst[armed]):
            stress[by_channel.get(int(i), int(i) + 1)] = int(max_stress[armed & (inst == i)].max())
    if not counts:
        return
    worst = (
        "error"
        if any("error" in c for c in counts.values())
        else ("warning" if any("warning" in c for c in counts.values()) else "alert")
    )
    ev = Evidence(
        id="esc:edt2",
        kind="fact",
        summary=f"extended DShot telemetry {worst} bits on motor(s) {sorted(counts)}",
        messages=("EDT2",),
        fields=("Status", "MaxStress"),
        segment=flight.armed,
        values={"by_motor": {str(k): v for k, v in sorted(counts.items())}, "max_stress": stress},
    )
    out.findings.append(
        Finding(
            code="esc.edt_status",
            severity="caution" if worst == "alert" else "out",
            confidence="high",
            evidence=(ev,),
            params={"kind": worst, "motors": ", ".join(map(str, sorted(counts)))},
        )
    )


def _esc_condition(flight: FlightData, ms: list[Motor], out: Result) -> None:
    """Temperatures against siblings, supply voltage per ESC, current per RPM squared.
    Real ESC telemetry only: the simulator's values are placeholders."""
    esc = flight.table("ESC")
    if flight.simulated or esc is None or esc.num_rows == 0:
        return
    t, inst = _times(esc), np.asarray(esc.column("Instance").to_numpy(), np.int64)
    armed = (t >= flight.arm_us) & (t <= flight.disarm_us)
    held = _inside(t, [(w.start_us, w.end_us) for w in flight.hover()])
    by_channel = {m.channel - 1: m.number for m in ms}
    escs = [int(i) for i in np.unique(inst[armed])]
    if "Temp" in esc.column_names:
        temp = si.values(esc, "Temp")
        peaks = {i: float(temp[armed & (inst == i)].max()) for i in escs}
        if any(v > 0 for v in peaks.values()):
            ev = Evidence(
                id="esc:temperature",
                kind="metric",
                summary=f"ESC peak temperatures {', '.join(f'{v:.0f}' for v in peaks.values())} °C",
                messages=("ESC",),
                fields=("Temp",),
                segment=flight.armed,
                values={f"motor{by_channel.get(i, i + 1)}": round(v, 1) for i, v in peaks.items()},
            )
            out.evidence.append(ev)
            out.metrics.append(
                MetricValue("esc.peak_temperature", "flight", max(peaks.values()), None, ev.id)
            )
            for i, v in peaks.items():
                others = [w for j, w in peaks.items() if j != i]
                if len(others) >= 2 and v - float(np.median(others)) >= HOT_SIBLING_C:
                    out.findings.append(
                        Finding(
                            code="esc.hot",
                            severity="caution",
                            confidence="medium",
                            evidence=(ev,),
                            params={
                                "motor": by_channel.get(i, i + 1),
                                "temperature": round(v),
                                "siblings": round(float(np.median(others))),
                            },
                        )
                    )
    if held.any() and {"Volt", "Curr", "RPM"} <= set(esc.column_names):
        volt, curr, rpm = si.values(esc, "Volt"), si.values(esc, "Curr"), si.values(esc, "RPM")
        supply = {
            i: float(np.median(volt[held & (inst == i)]))
            for i in escs
            if (held & (inst == i)).any()
        }
        if len(supply) >= 3 and all(v > 0 for v in supply.values()):
            typical = float(np.median(list(supply.values())))
            ev = Evidence(
                id="esc:supply",
                kind="metric",
                summary=(
                    f"ESC supply in hover {min(supply.values()):.2f} "
                    f"to {max(supply.values()):.2f} V"
                ),
                messages=("ESC",),
                fields=("Volt",),
                values={f"motor{by_channel.get(i, i + 1)}": round(v, 3) for i, v in supply.items()},
            )
            out.evidence.append(ev)
            for i, v in supply.items():
                if typical - v >= SUPPLY_DROP_V:
                    out.findings.append(
                        Finding(
                            code="esc.supply_drop",
                            severity="caution",
                            confidence="medium",
                            evidence=(ev,),
                            params={
                                "motor": by_channel.get(i, i + 1),
                                "drop": round(typical - v, 2),
                            },
                        )
                    )
        load: list[float] = []
        for i in escs:
            sel = held & (inst == i) & (rpm > 0) & (curr > 0)
            if sel.any():
                load.append(float(np.median(curr[sel] / (rpm[sel] / 1000.0) ** 2)))
        if load:
            ev = Evidence(
                id="esc:load",
                kind="metric",
                summary="current per (1000 rpm)^2 in hover, per ESC",
                messages=("ESC",),
                fields=("Curr", "RPM"),
                values={"per_esc": [round(v, 5) for v in load]},
            )
            out.evidence.append(ev)
            out.metrics.append(
                MetricValue("esc.current_per_rpm2", "hover", float(np.median(load)), None, ev.id)
            )


def _motors_health(flight: FlightData, reg: Registry) -> Result:
    ms = motors(flight.params)
    if not ms:
        return Result(skipped="no motor outputs are configured (SERVOn_FUNCTION)")
    out = Result()
    _outputs(flight, reg, ms, out)
    _desync(flight, ms, out)
    _edt2(flight, ms, out)
    _esc_condition(flight, ms, out)
    if not out.metrics and not out.findings:
        out.skipped = "the log has no motor outputs or ESC telemetry"
    return out


MOTORS_HEALTH = Analyzer(
    name="motors",
    version=2,
    run=_motors_health,
    metrics=(
        "motors.saturation_flight", "motors.saturation_hover", "motors.output_spread",
        "esc.peak_temperature", "esc.current_per_rpm2",
    ),
    findings=(
        "motors.saturation", "motors.output_spread", "esc.desync", "esc.edt_status", "esc.hot",
        "esc.supply_drop",
    ),
)  # fmt: skip
