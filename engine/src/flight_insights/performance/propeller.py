"""The propeller as the aircraft flies it: its thrust and power coefficients measured in a
flight's steady hover, and in a maker's table (spec: Power and efficiency analysis,
"Propeller and motor choice"; rules in `knowledge/rules/performance.json` "propeller").

- **The coefficients.** At n revolutions a second, a propeller of diameter D in air of
  density rho gives thrust T = C_T rho n^2 D^4 and takes shaft power P = C_P rho n^3 D^5.
  They describe the propeller whatever its speed or the air (within its Reynolds-number
  range), so a measurement at one weight carries to another.
- **The anchor** (a flight's, `flight_anchor`): over its quiet steady hover (stick inputs
  taken out), each motor's speed from its ESC, the thrust each motor gives (the weight
  over the tilt, shared between the motors), the air's density, and the power: each
  ESC's own voltage times current when every motor's ESC reports them on real hardware,
  else the packs' power shared between the motors (it holds the avionics too, and says
  so). With a motor model (`performance/motor.py`), the shaft power is the torque that
  draws that power at that speed, so C_P is the propeller's own; without one, only the
  electrical coefficient is given.
- **From a table** (`table_points`): each step with its speed gives C_T; with its torque,
  C_P from the shaft power; in the bench's air when the table gives its temperature,
  else standard air (and says so).

Only speeds, attitude, the barometer and the packs' and ESCs' voltage and current are
read, never a position.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.motors_health import motors
from flight_insights.analyzers.rate_health import quiet_hover
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si
from flight_insights.performance.bench import Bench, test_density
from flight_insights.performance.motor import RPM_TO_RAD, Motor

Array = np.ndarray[Any, Any]
G = 9.80665
INCH = 0.0254
RHO_STANDARD = 1.225


@dataclass(frozen=True)
class Anchor:
    """A propeller's operating point in a flight's quiet steady hover, per motor."""

    rpm: float  # the motors' mean speed
    thrust_n: float  # each motor's share of the weight, over the tilt
    power_w: float  # each motor's electrical power
    power_from: str  # 'escs' (each ESC's volts times amps) or 'packs' (shared, with avionics)
    density: float  # kg/m^3
    seconds: float
    motors: int
    rpm_spread: float  # the motors' speeds' standard deviation over their mean


def coefficients(rpm: float, thrust_n: float, power_w: float, density: float,
                 diameter_m: float) -> tuple[float, float]:  # fmt: skip
    """(C_T, C_P) at a speed, thrust and power."""
    n = rpm / 60.0
    return (thrust_n / (density * n**2 * diameter_m**4),
            power_w / (density * n**3 * diameter_m**5))  # fmt: skip


def shaft(motor: Motor, rpm: float, power_w: float) -> tuple[float, float] | None:
    """(torque N m, shaft power W): what the motor turns at this speed while drawing this
    power; None when the power is less than it draws turning nothing."""
    omega = rpm * RPM_TO_RAD
    if float(motor.power(0.0, omega)) >= power_w:
        return None
    lo, hi = 0.0, power_w / omega
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if float(motor.power(mid, omega)) < power_w else (lo, mid)
    q = (lo + hi) / 2
    return q, q * omega


@dataclass(frozen=True)
class TablePoint:
    rpm: float
    thrust_n: float
    ct: float
    cp: float | None  # from the shaft power, where the step has its torque
    cp_electric: float  # from the power drawn
    density: float


def table_points(table: Bench) -> tuple[list[TablePoint], str]:
    """Each step's coefficients, and which air they are in; empty without the propeller's
    diameter or the steps' speeds."""
    air = "the bench's" if table.ambient_c is not None else "standard air (no temperature)"
    if not table.prop_diameter_in:
        return [], air
    d = table.prop_diameter_in * INCH
    rho = test_density(table.ambient_c) if table.ambient_c is not None else RHO_STANDARD
    out: list[TablePoint] = []
    for r in table.rows:
        if r.rpm is None:
            continue
        thrust = r.thrust_g / 1000.0 * G
        ct, cpe = coefficients(r.rpm, thrust, r.power_w, rho, d)
        cp = coefficients(r.rpm, thrust, r.shaft_w, rho, d)[1] if r.shaft_w is not None else None
        out.append(TablePoint(r.rpm, thrust, ct, cp, cpe, rho))
    return out, air


def _held(t: Array, spans: Sequence[tuple[int, int]]) -> Array:
    keep = np.zeros(len(t), bool)
    for a, b in spans:
        keep |= (t >= a) & (t < b)
    return keep


def flight_anchor(flight: FlightData, reg: Registry, mass_kg: float,
                  density: Callable[[Array, Array], Array],
                  pack_power: tuple[Array, Array] | None) -> Anchor | str:  # fmt: skip
    """The propeller's operating point in the flight's quiet steady hover, or why it
    cannot be read. `pack_power` is the packs' (times, power), for when the ESCs do not
    report their own."""
    spec: Mapping[str, Any] = reg.rules["performance"]["propeller"]
    spans = quiet_hover(flight, reg)
    spans = [(a, b) for a, b in spans if b - a >= float(spec["min_window_s"]) * 1e6]
    seconds = sum(b - a for a, b in spans) / 1e6
    if seconds < float(spec["min_seconds"]):
        return f"less than {spec['min_seconds']:g} s of quiet steady hover"
    ms = motors(flight.params)
    esc, att = flight.table("ESC"), flight.table("ATT")
    if esc is None or not {"RPM", "Instance"} <= set(esc.column_names):
        return "no ESC speed telemetry: the propeller's speed is not known"
    if att is None or not {"Roll", "Pitch"} <= set(att.column_names) or not ms:
        return "no attitude or no motors in the parameters"
    te = np.asarray(esc.column("TimeUS").to_numpy(), np.int64)
    inst = np.asarray(esc.column("Instance").to_numpy(), np.int64)
    rpm = si.values(esc, "RPM")
    held = _held(te, spans)
    speeds: list[float] = []
    escs_w: list[float] = []
    reports_power = not flight.simulated and {"Volt", "Curr"} <= set(esc.column_names)
    volt = si.values(esc, "Volt") if reports_power else None
    curr = si.values(esc, "Curr") if reports_power else None
    for m in ms:
        rows = held & (inst == m.channel - 1)
        if not rows.any():
            return f"no ESC telemetry for motor {m.number} in the hover"
        s = float(np.median(rpm[rows]))
        if s <= 0:
            return f"motor {m.number}'s ESC reports no speed in the hover"
        speeds.append(s)
        if volt is not None and curr is not None:
            v, a = float(np.median(volt[rows])), float(np.median(curr[rows]))
            if v > 0 and a > 0:
                escs_w.append(v * a)
    ta = np.asarray(att.column("TimeUS").to_numpy(), np.int64)
    ha = _held(ta, spans)
    tilt = float(np.mean(np.cos(np.radians(si.values(att, "Roll")[ha]))
                         * np.cos(np.radians(si.values(att, "Pitch")[ha]))))  # fmt: skip
    starts = np.array([a for a, _ in spans], np.int64)
    ends = np.array([b for _, b in spans], np.int64)
    widths = (ends - starts).astype(float)
    rho = float(np.average(density(starts, ends), weights=widths))
    n = len(ms)
    if len(escs_w) == n:
        power, power_from = float(np.mean(escs_w)), "escs"
    elif pack_power is not None:
        tp, pp = pack_power
        hp = _held(tp, spans)
        if not hp.any():
            return "no pack power in the hover"
        power, power_from = float(np.mean(pp[hp])) / n, "packs"
    else:
        return "no ESC or pack power in the hover"
    mean = float(np.mean(speeds))
    return Anchor(rpm=mean, thrust_n=mass_kg * G / (tilt * n), power_w=power,
                  power_from=power_from, density=rho, seconds=seconds, motors=n,
                  rpm_spread=float(np.std(speeds)) / mean)  # fmt: skip


def anchor_view(a: Anchor, diameter_in: float | None, motor: Motor | None) -> dict[str, Any]:
    """The anchor as the screen shows it: per motor thrust, power and speed, its
    coefficients where the propeller's diameter is known, and with a motor model, the
    shaft power, the propeller's own C_P and the drive's efficiency."""
    out: dict[str, Any] = {
        "rpm": a.rpm, "thrust_g": a.thrust_n / G * 1000.0, "power_w": a.power_w,
        "power_from": a.power_from, "density": a.density, "seconds": a.seconds,
        "rpm_spread": a.rpm_spread, "g_per_w": a.thrust_n / G * 1000.0 / a.power_w,
    }  # fmt: skip
    if not diameter_in:
        return out
    d = diameter_in * INCH
    ct, cpe = coefficients(a.rpm, a.thrust_n, a.power_w, a.density, d)
    out |= {"ct": ct, "cp_electric": cpe, "tip_ms": a.rpm * RPM_TO_RAD * d / 2}
    if motor is not None and a.power_from == "escs":
        got = shaft(motor, a.rpm, a.power_w)
        if got is not None:
            q, w = got
            out |= {"torque_nm": q, "shaft_w": w, "efficiency": w / a.power_w,
                    "cp": coefficients(a.rpm, a.thrust_n, w, a.density, d)[1],
                    "figure_of_merit": a.thrust_n**1.5 / math.sqrt(2 * a.density * math.pi
                                                                   * (d / 2) ** 2) / w}  # fmt: skip
    return out
