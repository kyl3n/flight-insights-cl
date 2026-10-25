"""Power and electrical health (spec: Power and efficiency analysis; Electrical system
health; Reference range library, "Propulsion and power").

From the battery monitor (BAT), the board's own supply (POWR) and ESC telemetry:

- hover power and energy per minute, the baseline every efficiency figure starts from;
- where the power goes in hover: battery loss (current squared times the resistance
  ArduPilot estimates), and with real ESC telemetry the propulsion share, what is
  left for avionics and payload, and the loss in the wiring between battery and ESCs;
- the battery's resistance and its sag under load;
- hover throttle against ArduPilot's usual band, and against MOT_THST_HOVER where hover
  learning is off;
- the battery monitor against real ESC telemetry: more current into the ESCs than the
  monitor reports, or a voltage apart from theirs while little current flows, means the
  monitor needs calibrating;
- the board's supply voltage: its lowest value and its dips under load.

Hover power and battery resistance have no universal range: they are judged against
the airframe's and the pack's own history (trends). In the simulator, ESC voltage and
current are placeholders, so the ESC-based shares are left out there.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import (
    SEVERITY_OF_ZONE,
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

Floats = np.ndarray[tuple[int], np.dtype[np.float64]]
STEADY_ENOUGH_S = 10.0
# The battery monitor disagrees with ESC telemetry when the ESCs draw this much more
# current than it reports (they cannot: the battery feeds them and the avionics), or
# their voltage differs from its by this share while at most QUIET_SHARE of the hover
# current flows (harness drop negligible). App rules, uncalibrated: ESC sensors have
# errors of their own, so the finding asks for a calibration, never a guessed scale.
ESC_CURRENT_OVER = 0.10
VOLTAGE_APART = 0.03
QUIET_SHARE = 0.2
NEAR_US = 100_000  # ESC rows this close to a quiet battery row count as read with it
# MOT_THST_HOVER counts as stale this far from the measured hover throttle.
HOVER_STALE = 0.05


def _times(t: pa.Table) -> np.ndarray[tuple[int], np.dtype[np.int64]]:
    return np.asarray(t.column("TimeUS").to_numpy(), np.int64)


def _inside(times: np.ndarray[Any, Any], spans: list[tuple[int, int]]) -> np.ndarray[Any, Any]:
    sel = np.zeros(len(times), bool)
    for a, b in spans:
        sel |= (times >= a) & (times <= b)
    return sel


def _spans(flight: FlightData) -> tuple[str, list[tuple[int, int]]]:
    hover = flight.hover()
    if hover:
        return "hover", [(w.start_us, w.end_us) for w in hover]
    return "flight", [(flight.arm_us, flight.disarm_us)]


def _battery(
    flight: FlightData, reg: Registry, kind: str, spans: list[tuple[int, int]], out: Result
) -> None:
    bat = flight.table("BAT")
    if bat is None or bat.num_rows == 0 or not {"Volt", "Curr"} <= set(bat.column_names):
        return
    t = _times(bat)
    inst = (
        np.asarray(bat.column("Inst").to_numpy(), np.int64)
        if "Inst" in bat.column_names
        else np.zeros(len(t), np.int64)
    )
    first = int(np.min(inst))
    primary = inst == first  # the battery the failsafes watch
    volt, curr = si.values(bat, "Volt"), si.values(bat, "Curr")
    armed = (t >= flight.arm_us) & (t <= flight.disarm_us) & primary
    held = _inside(t, spans) & primary
    if not held.any() or not armed.any():
        return
    power = volt * curr
    hover_w = float(np.median(power[held]))
    hover_a = float(np.median(curr[held]))
    res = si.values(bat, "Res") if "Res" in bat.column_names else np.full(len(t), np.nan)
    armed_res = res[armed]
    resistance = (
        float(armed_res[np.isfinite(armed_res) & (armed_res > 0)][-1])
        if (np.isfinite(armed_res) & (armed_res > 0)).any()
        else None
    )
    rested = volt[(t < flight.arm_us) & primary]
    resting_v = float(np.median(rested[-20:])) if len(rested) else None
    peak = int(np.argmax(np.where(armed, curr, -np.inf)))
    sag_v = resting_v - float(volt[peak]) if resting_v is not None else None
    seconds = sum(b - a for a, b in spans) / 1e6
    ev = Evidence(
        id="power:battery",
        kind="metric",
        summary=(
            f"{hover_w:.0f} W at {hover_a:.1f} A in {kind} over {seconds:.1f} s"
            + (f"; battery resistance {resistance * 1000:.1f} mOhm" if resistance else "")
            + (
                f"; sag {sag_v:.2f} V at the {float(curr[peak]):.0f} A peak"
                if sag_v is not None
                else ""
            )
        ),
        messages=("BAT",),
        fields=("Volt", "Curr", "Res"),
        segment=Segment(kind, spans[0][0], spans[-1][1]),
        values={
            "hover_power_w": round(hover_w, 1),
            "hover_current_a": round(hover_a, 2),
            "hover_voltage_v": round(float(np.median(volt[held])), 3),
            "resistance_ohm": resistance,
            "resting_voltage_v": resting_v,
            "peak_current_a": round(float(curr[peak]), 2),
            "sag_v": None if sag_v is None else round(sag_v, 3),
            "seconds": round(seconds, 1),
            "battery": first,
        },
    )
    out.evidence.append(ev)
    out.metrics.append(MetricValue("power.hover_power", kind, hover_w, None, ev.id))
    out.metrics.append(MetricValue("power.energy_per_minute", kind, hover_w / 60.0, None, ev.id))
    out.metrics.append(MetricValue("power.peak_current", "flight", float(curr[peak]), None, ev.id))
    if resistance:
        out.metrics.append(
            MetricValue("power.battery_resistance", "flight", resistance * 1000, None, ev.id)
        )
        out.metrics.append(
            MetricValue("power.battery_loss", kind, hover_a**2 * resistance, None, ev.id)
        )
    if not flight.simulated:
        _escs(flight, kind, spans, hover_w, float(np.median(volt[held])), hover_a, out)


def _escs(
    flight: FlightData,
    kind: str,
    spans: list[tuple[int, int]],
    total_w: float,
    bat_v: float,
    bat_a: float,
    out: Result,
) -> None:
    """Propulsion share, avionics remainder and harness loss from real ESC telemetry."""
    esc = flight.table("ESC")
    if esc is None or not {"Volt", "Curr", "Instance"} <= set(esc.column_names):
        return
    t = _times(esc)
    inst = np.asarray(esc.column("Instance").to_numpy(), np.int64)
    volt, curr = si.values(esc, "Volt"), si.values(esc, "Curr")
    held = _inside(t, spans)
    escs = [int(i) for i in np.unique(inst[held])]
    if not escs:
        return
    per_v = [float(np.median(volt[held & (inst == i)])) for i in escs]
    per_a = [float(np.median(curr[held & (inst == i)])) for i in escs]
    if not all(v > 0 for v in per_v) or not any(a > 0 for a in per_a):
        return  # telemetry without voltage or current
    propulsion = float(sum(v * a for v, a in zip(per_v, per_a, strict=True)))
    harness = max(bat_v - float(np.median(per_v)), 0.0) * bat_a
    ev = Evidence(
        id="power:split",
        kind="metric",
        summary=(
            f"of {total_w:.0f} W in {kind}: {propulsion:.0f} W into {len(escs)} ESCs, "
            f"{harness:.1f} W lost between battery and ESCs, "
            f"{max(total_w - propulsion, 0):.0f} W left for avionics and payload"
        ),
        messages=("BAT", "ESC"),
        fields=("Volt", "Curr"),
        segment=Segment(kind, spans[0][0], spans[-1][1]),
        values={
            "esc_voltage_v": [round(v, 3) for v in per_v],
            "esc_current_a": [round(a, 2) for a in per_a],
            "propulsion_w": round(propulsion, 1),
            "harness_loss_w": round(harness, 2),
            "avionics_w": round(max(total_w - propulsion, 0.0), 1),
        },
    )
    out.evidence.append(ev)
    out.metrics.append(MetricValue("power.propulsion", kind, propulsion, None, ev.id))
    out.metrics.append(
        MetricValue("power.avionics", kind, max(total_w - propulsion, 0.0), None, ev.id)
    )
    out.metrics.append(MetricValue("power.harness_loss", kind, harness, None, ev.id))
    _monitor_vs_escs(flight, esc, sum(per_a), bat_a, ev, out)


def _monitor_vs_escs(
    flight: FlightData, esc: pa.Table, esc_a: float, bat_a: float, split: Evidence, out: Result
) -> None:
    """The battery monitor against what the ESCs report: in hover, current; while little
    current flows, voltage."""
    bat = flight.table("BAT")
    assert bat is not None
    t, et = _times(bat), _times(esc)
    inst = (
        np.asarray(bat.column("Inst").to_numpy(), np.int64)
        if "Inst" in bat.column_names
        else np.zeros(len(t), np.int64)
    )
    primary = inst == int(np.min(inst))
    volt, curr = si.values(bat, "Volt"), si.values(bat, "Curr")
    quiet = primary & (t >= flight.arm_us) & (t <= flight.disarm_us)
    quiet &= curr <= QUIET_SHARE * bat_a
    bat_v = esc_v = None
    if quiet.sum() >= 5:
        qt = np.sort(t[quiet])
        # the ESC rows within NEAR_US of a quiet battery row
        k = np.clip(np.searchsorted(qt, et), 1, len(qt) - 1)
        gap = np.minimum(np.abs(et - qt[k - 1]), np.abs(et - qt[k]))
        ev_volt = si.values(esc, "Volt")[gap <= NEAR_US]
        ev_volt = ev_volt[ev_volt > 0]
        if len(ev_volt):
            bat_v, esc_v = float(np.median(volt[quiet])), float(np.median(ev_volt))
    current_over = esc_a > (1 + ESC_CURRENT_OVER) * bat_a
    voltage_apart = (
        bat_v is not None and esc_v is not None and abs(bat_v - esc_v) > VOLTAGE_APART * esc_v
    )
    if not (current_over or voltage_apart):
        return
    ev = Evidence(
        id="power:monitor",
        kind="metric",
        summary=(
            (f"ESCs draw {esc_a:.1f} A in hover, the battery monitor reports {bat_a:.1f} A; "
             if current_over else "")
            + (f"at low current the battery monitor reads {bat_v:.2f} V, the ESCs {esc_v:.2f} V"
               if voltage_apart and bat_v is not None and esc_v is not None else "")
        ).strip("; "),
        messages=("BAT", "ESC"),
        fields=("Volt", "Curr"),
        segment=split.segment,
        values={
            "esc_current_a": round(esc_a, 2), "battery_current_a": round(bat_a, 2),
            "battery_voltage_v": None if bat_v is None else round(bat_v, 3),
            "esc_voltage_v": None if esc_v is None else round(esc_v, 3),
        },
    )  # fmt: skip
    out.evidence.append(ev)
    out.findings.append(
        Finding(
            code="power.monitor_vs_escs",
            severity="caution",
            confidence="medium",
            evidence=(ev, split),
            params={
                "what": " and ".join(
                    w for w, on in (("current", current_over), ("voltage", voltage_apart)) if on
                ),
            },
        )
    )


def _hover_throttle(flight: FlightData, reg: Registry, out: Result) -> None:
    value = flight.fingerprint.get("hover_throttle", {}).get("value")
    hover = flight.hover()
    if not value or not hover:
        return
    seconds = sum(w.end_us - w.start_us for w in hover) / 1e6
    throttle = float(value)
    zone = reg.metrics["power.hover_throttle"].zone(throttle)
    ev = Evidence(
        id="power:hover-throttle",
        kind="metric",
        summary=f"hover throttle {throttle:.3f} over {seconds:.1f} s of steady hover",
        messages=("CTUN", "MOTB"),
        fields=("ThO", "ThrOut"),
        segment=Segment("hover", hover[0].start_us, hover[-1].end_us),
        values={"hover_throttle": round(throttle, 3), "seconds": round(seconds, 1)},
    )
    out.evidence.append(ev)
    out.metrics.append(MetricValue("power.hover_throttle", "hover", throttle, zone, ev.id))
    learned = flight.params.get("MOT_THST_HOVER")
    if (
        flight.params.get("MOT_HOVER_LEARN") == 0
        and learned is not None
        and abs(learned - throttle) > HOVER_STALE
    ):
        out.findings.append(
            Finding(
                code="power.thst_hover_stale",
                severity="caution",
                confidence="high" if seconds >= STEADY_ENOUGH_S else "medium",
                evidence=(ev,),
                params={"value": round(learned, 3), "throttle": round(throttle, 3)},
                changes=(CandidateChange("MOT_THST_HOVER", learned, round(throttle, 3), "setup"),),
            )
        )
    if zone in ("caution", "out"):
        out.findings.append(
            Finding(
                code="power.hover_throttle_high" if throttle >= 0.6 else "power.hover_throttle_low",
                severity=SEVERITY_OF_ZONE[zone],
                confidence="high" if seconds >= STEADY_ENOUGH_S else "medium",
                evidence=(ev,),
                params={"throttle": round(throttle, 2)},
            )
        )


def _board(flight: FlightData, reg: Registry, out: Result) -> None:
    powr = flight.table("POWR")
    if powr is None or powr.num_rows == 0 or "Vcc" not in powr.column_names:
        return
    t = _times(powr)
    vcc = si.values(powr, "Vcc")
    armed = (t >= flight.arm_us) & (t <= flight.disarm_us) & (vcc > 0)
    if not armed.any():
        return
    low = float(vcc[armed].min())
    typical = float(np.median(vcc[armed]))
    dip = typical - low
    when = int(t[armed][int(np.argmin(vcc[armed]))])
    ev = Evidence(
        id="power:board",
        kind="metric",
        summary=f"board supply {typical:.2f} V while armed, lowest {low:.2f} V",
        messages=("POWR",),
        fields=("Vcc",),
        segment=flight.armed,
        values={"typical_v": round(typical, 3), "lowest_v": round(low, 3), "lowest_at_us": when},
    )
    out.evidence.append(ev)
    low_zone = reg.metrics["power.board_voltage_min"].zone(low)
    dip_zone = reg.metrics["power.board_voltage_dip"].zone(dip)
    out.metrics.append(MetricValue("power.board_voltage_min", "flight", low, low_zone, ev.id))
    out.metrics.append(MetricValue("power.board_voltage_dip", "flight", dip, dip_zone, ev.id))
    worst = low_zone if low_zone == "out" else dip_zone
    if worst in ("caution", "out"):
        out.findings.append(
            Finding(
                code="power.board_voltage",
                severity=SEVERITY_OF_ZONE[worst],
                confidence="high",
                evidence=(ev,),
                params={
                    "lowest": round(low, 2),
                    "typical": round(typical, 2),
                    "at_s": round((when - flight.arm_us) / 1e6, 1),
                },
            )
        )


def _power(flight: FlightData, reg: Registry) -> Result:
    out = Result()
    kind, spans = _spans(flight)
    _battery(flight, reg, kind, spans, out)
    _hover_throttle(flight, reg, out)
    _board(flight, reg, out)
    if not out.metrics:
        out.skipped = "the log has no battery monitor, hover or board supply data"
    return out


POWER = Analyzer(
    name="power",
    version=2,
    run=_power,
    metrics=(
        "power.hover_power", "power.energy_per_minute", "power.peak_current",
        "power.battery_resistance", "power.battery_loss", "power.propulsion", "power.avionics",
        "power.harness_loss", "power.hover_throttle", "power.board_voltage_min",
        "power.board_voltage_dip",
    ),
    findings=(
        "power.hover_throttle_high", "power.hover_throttle_low", "power.board_voltage",
        "power.thst_hover_stale", "power.monitor_vs_escs",
    ),
)  # fmt: skip
