"""The speed build-up: the verified speed and the next step towards the most the airframe
can safely hold (spec: Power and efficiency analysis, "Range, endurance and speed", "The
speed build-up"; rules in `knowledge/rules/performance.json` "envelope").

- **What each flight showed** (`Fast`): its fast segment (`analyzers/fast.py`), the speed
  and time near its top, the weight it flew at, and each watched metric with its zone.
- **Verified speed:** the fastest fast segment held for the rules' seconds or more at the
  same weight or heavier, with every watched metric that has zones in its target zone. A
  flight with anything outside its target verifies nothing.
- **The next step:** 1 m/s or 10% past the verified speed, whichever is smaller.
- **What the models predict there**, each against its margin:
  - lean: the lean the measured drag needs at the step's airspeed (`wind.lean_needed`, the
    drag carried to the weight and air as `top_speed.drag_at`), as a share of the limit,
    inside the lean share's target zone;
  - thrust: that lean within the lean the thrust allows at this weight;
  - current: the power model's at the step over the lowest voltage the pack reaches,
    within the least of the pack's rating and `MOT_BAT_CURR_MAX`;
  - ESC temperature: each ESC's thermal model settled at its share of that current, the
    rules' margin under the ESCs' protection temperature;
  - the harmonic notch: the motors' hover frequency carried to the step's thrust (it turns
    as the square root of the thrust, which is the weight over the cosine of the lean),
    within the reach of every notch set that covers the motors in hover and does not
    follow them: a fixed notch's band (FREQ, or FREQ with REF 0, plus half its BW) or the
    in-flight FFT's search band (FFT_MAXHZ). Sets that follow the motors (throttle, ESC
    telemetry, RPM sensor) move with them;
  - motor output and vibration: a straight line against speed through the airframe's fast
    segments held long enough at the same weight or heavier (in their target zones or
    not: those outside show where it goes wrong), once they span the rules' gap of
    speeds, inside their target zones;
  - attitude and velocity tracking, which have no fixed zones: the same straight line, no
    more than the rules' growth over what the verified speed showed.

  The pack's sag is in the current check: the current is read at the lowest voltage the
  pack reaches under load before its failsafe.
- **Offered** only when every prediction holds and the required ones (lean, thrust,
  current) could be made; otherwise the first that fails is the limit that binds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.wind import configured_lean, lean_needed, thrust_lean
from flight_insights.filters.harmonic import FFT, FIXED, SETS, NotchParams
from flight_insights.performance.fit import G, Model
from flight_insights.performance.thermal import Thermal
from flight_insights.performance.top_speed import Basis, drag_at, hover_throttle

TRENDS = {"output": "fast.output_max", "vibration": "fast.vibe_p95"}
TRACKING = {"attitude": "fast.attitude_error_deg", "velocity": "fast.velocity_error_ms"}


@dataclass(frozen=True)
class Fast:
    """One flight's fast segment."""

    flight_id: str
    speed_ms: float
    seconds: float
    mass_kg: float | None
    metrics: Mapping[str, tuple[float, str | None]]  # watched metric: value, zone


def _comparable(history: Sequence[Fast], mass_kg: float, rules: Mapping[str, Any]) -> list[Fast]:
    """The fast segments held long enough at the same weight or heavier."""
    spec: Mapping[str, Any] = rules["envelope"]
    heavy_enough = mass_kg * (1.0 - float(spec["weight_share"]))
    return [f for f in history if f.seconds >= float(spec["min_seconds"])
            and f.mass_kg is not None and f.mass_kg >= heavy_enough]  # fmt: skip


def verified(history: Sequence[Fast], mass_kg: float, rules: Mapping[str, Any]) -> Fast | None:
    """The fastest segment that verifies a speed at this weight, or None."""
    ok = [f for f in _comparable(history, mass_kg, rules)
          if all(zone in (None, "target") for _, zone in f.metrics.values())]  # fmt: skip
    return max(ok, key=lambda f: f.speed_ms, default=None)


def next_speed(verified_ms: float, rules: Mapping[str, Any]) -> float:
    spec: Mapping[str, Any] = rules["envelope"]
    return verified_ms + min(float(spec["step_ms"]), float(spec["step_share"]) * verified_ms)


@dataclass(frozen=True)
class Inputs:
    """What the airframe's models and settings give the predictions."""

    model: Model
    basis: Basis
    params: Mapping[str, float]
    lowest_volts: float | None
    current_a: float | None  # the packs' continuous rating
    thermals: Sequence[Thermal] = ()
    esc_limit_c: float | None = None  # the ESCs' protection temperature
    motors: int = 4
    motor_hz: tuple[float, float] | None = None  # the motors' frequency in hover, at a weight (kg)


@dataclass(frozen=True)
class Check:
    name: str  # 'lean', 'thrust', 'current', 'esc_temp', 'output', 'vibration', 'attitude'...
    value: float
    limit: float
    holds: bool
    how: str  # 'model' or 'trend'


@dataclass(frozen=True)
class Step:
    verified: Fast | None
    next_ms: float | None
    checks: tuple[Check, ...]
    missing: tuple[str, ...]  # what could not be predicted
    binding: str | None  # the first check that fails, or a required one missing
    offered: bool


def _trend(history: Sequence[Fast], metric: str, at: float, gap: float) -> float | None:
    points = [(f.speed_ms, f.metrics[metric][0]) for f in history if metric in f.metrics]
    if len(points) < 2:
        return None
    speeds = np.array([p[0] for p in points])
    if float(np.ptp(speeds)) < gap:
        return None
    slope, intercept = np.polyfit(speeds, np.array([p[1] for p in points]), 1)
    return float(slope * at + intercept)


def _target_limit(reg: Registry, metric: str) -> float:
    """The top of a metric's target zone."""
    [(_, high)] = reg.metrics[metric].zones["target"]
    assert high is not None
    return float(high)


FFT_DEFAULTS = {"FFT_MINHZ": 50.0, "FFT_MAXHZ": 450.0}  # AP_GyroFFT.cpp var_info, 4.7.0


def notch_reach(params: Mapping[str, float], motor_hz: float) -> float | None:
    """The highest motor frequency the harmonic notch sets that cover the motors at
    `motor_hz` and do not follow them still reach: a fixed notch's band, or the in-flight
    FFT's search band. None when every set that covers them follows them, or none does."""
    reaches: list[float] = []
    for prefix in SETS:
        if f"{prefix}ENABLE" not in params:
            continue
        p = NotchParams.read(params, prefix)
        if not p.enable or p.num_harmonics == 0:
            continue
        if p.mode == FIXED or p.ref == 0:  # tracking.py: where such a notch sits
            centre = max(p.freq, p.bw * 0.52) if p.mode == FIXED else p.freq
            if abs(motor_hz - centre) <= p.bw / 2:
                reaches.append(centre + p.bw / 2)
        elif p.mode == FFT:
            low = float(params.get("FFT_MINHZ", FFT_DEFAULTS["FFT_MINHZ"]))
            high = float(params.get("FFT_MAXHZ", FFT_DEFAULTS["FFT_MAXHZ"]))
            if low <= motor_hz <= high:
                reaches.append(high)
    return min(reaches) if reaches else None


def _static(params: Mapping[str, float]) -> bool:
    """Whether an enabled harmonic notch set does not follow the motors."""
    sets = [NotchParams.read(params, prefix) for prefix in SETS if f"{prefix}ENABLE" in params]
    return any(p.enable and p.num_harmonics and (p.mode in (FIXED, FFT) or p.ref == 0)
               for p in sets)  # fmt: skip


def motor_hz_at(motor_hz: tuple[float, float], mass_kg: float, lean_deg: float) -> float:
    """The motors' frequency at a weight and lean: in steady level flight the thrust is
    the weight over the cosine of the lean, and a fixed-pitch propeller's speed goes as
    the square root of its thrust."""
    hz, kg = motor_hz
    return hz * float(np.sqrt(mass_kg / kg / np.cos(np.radians(lean_deg))))


def model_checks(inputs: Inputs, at: float, mass_kg: float, density: float,
                 reg: Registry) -> tuple[list[Check], list[str]]:  # fmt: skip
    """The checks the models predict at an airspeed (lean, thrust, current, ESC
    temperature), and those they cannot predict."""
    rules: Mapping[str, Any] = reg.rules["performance"]
    wind_rules: Mapping[str, Any] = reg.rules["wind"]["envelope"]
    spec: Mapping[str, Any] = rules["envelope"]
    checks: list[Check] = []
    missing: list[str] = []

    limit_deg = configured_lean(inputs.params, wind_rules)
    lean = lean_needed(drag_at(inputs.basis, mass_kg, density), at)
    share = 100.0 * lean / limit_deg
    lean_max = _target_limit(reg, "fast.lean_share")
    checks.append(Check("lean", share, lean_max, share < lean_max, "model"))
    throttle = hover_throttle(inputs.basis, mass_kg, density)
    if throttle is None:
        missing.append("thrust")
    else:
        allowed = thrust_lean(throttle, wind_rules)
        checks.append(Check("thrust", lean, allowed, lean <= allowed, "model"))
    if inputs.motor_hz is not None:
        hover = motor_hz_at(inputs.motor_hz, mass_kg, 0.0)
        reach = notch_reach(inputs.params, hover)
        if reach is not None:
            hz = motor_hz_at(inputs.motor_hz, mass_kg, lean)
            checks.append(Check("notch", hz, reach, hz <= reach, "model"))
    elif _static(inputs.params):
        missing.append("notch")

    power = float(inputs.model.power(mass_kg * G, at, 0.0, density, rules)[()])
    ratings = [
        a for a in (inputs.current_a, float(inputs.params.get("MOT_BAT_CURR_MAX", 0.0) or 0.0)) if a
    ]
    if inputs.lowest_volts and ratings:
        amps = power / inputs.lowest_volts
        checks.append(Check("current", amps, min(ratings), amps <= min(ratings), "model"))
        if inputs.thermals and inputs.esc_limit_c is not None:
            hottest = max(m.steady(amps / max(inputs.motors, 1)) for m in inputs.thermals)
            cap = inputs.esc_limit_c - float(spec["esc_margin_c"])
            checks.append(Check("esc_temp", hottest, cap, hottest <= cap, "model"))
        else:
            missing.append("esc_temp")
    else:
        missing.append("current")
    return checks, missing


def _holds(name: str, inputs: Inputs, at: float, mass_kg: float, density: float,
           reg: Registry) -> bool | None:  # fmt: skip
    found = [c for c in model_checks(inputs, at, mass_kg, density, reg)[0] if c.name == name]
    return found[0].holds if found else None


def allows(name: str, inputs: Inputs, mass_kg: float, density: float, reg: Registry) -> float:
    """The fastest airspeed at which the model's check `name` holds, searched up to the top
    speed's search end every half metre a second, then to its step past the last that
    holds; 0 when it holds at none. The current dips past the hover and rises again
    (the power's bucket), so it is the fastest that holds, not the first that fails."""
    spec: Mapping[str, Any] = reg.rules["performance"]["top_speed"]
    top, fine = float(spec["search_ms"]), float(spec["step_ms"])
    coarse = np.arange(0.5, top + 0.25, 0.5)
    ok = [v for v in coarse if _holds(name, inputs, float(v), mass_kg, density, reg)]
    if not ok:
        return 0.0
    last = float(max(ok))
    if last >= top:
        return top
    for v in np.arange(last + fine, last + 0.5, fine)[::-1]:
        if _holds(name, inputs, float(v), mass_kg, density, reg):
            return float(v)
    return last


def _solved(solution: Mapping[str, Any], inputs: Inputs, mass_kg: float) -> tuple[Inputs, float]:
    """The inputs and weight with one of the rules' solutions applied."""
    if "drag_share" in solution:  # the frame's flat plate: its drag and its parasite power
        k = float(solution["drag_share"])
        basis = replace(inputs.basis, drag=replace(inputs.basis.drag, k2=inputs.basis.drag.k2 * k))
        model = replace(inputs.model, drag_area=inputs.model.drag_area * k)
        return replace(inputs, basis=basis, model=model), mass_kg
    if "thrust_share" in solution:  # as much more thrust: the hover throttle that much lower
        throttle = inputs.basis.throttle
        if throttle is None:
            return inputs, mass_kg
        basis = replace(inputs.basis, throttle=throttle / float(solution["thrust_share"]))
        return replace(inputs, basis=basis), mass_kg
    if "mass_share" in solution:
        return inputs, mass_kg * float(solution["mass_share"])
    if "rating_share" in solution:  # the packs' rating and MOT_BAT_CURR_MAX alike
        k = float(solution["rating_share"])
        params = dict(inputs.params)
        if params.get("MOT_BAT_CURR_MAX"):
            params["MOT_BAT_CURR_MAX"] = float(params["MOT_BAT_CURR_MAX"]) * k
        rating = inputs.current_a * k if inputs.current_a else None
        return replace(inputs, params=params, current_a=rating), mass_kg
    if "heating_share" in solution:  # cooler ESCs: the same current heats them less
        k = float(solution["heating_share"])
        return replace(inputs, thermals=[replace(t, k=t.k * k) for t in inputs.thermals]), mass_kg
    raise ValueError(f"an unknown solution: {dict(solution)}")


def gains(name: str, inputs: Inputs, mass_kg: float, density: float,
          reg: Registry) -> tuple[float, dict[str, float]]:  # fmt: skip
    """The airspeed the model's check `name` allows now, and how much faster each of the
    rules' solutions for it would let the airframe fly."""
    now = allows(name, inputs, mass_kg, density, reg)
    out: dict[str, float] = {}
    for solution in reg.rules["performance"]["envelope"]["solutions"].get(name, ()):
        changed, mass = _solved(solution, inputs, mass_kg)
        out[str(solution["name"])] = allows(name, changed, mass, density, reg) - now
    return now, out


def step(history: Sequence[Fast], inputs: Inputs, mass_kg: float, density: float,
         reg: Registry) -> Step:  # fmt: skip
    """The next step at this weight and air, what the models predict there, and whether it
    is offered."""
    rules: Mapping[str, Any] = reg.rules["performance"]
    spec: Mapping[str, Any] = rules["envelope"]
    base = verified(history, mass_kg, rules)
    if base is None:
        return Step(None, None, (), ("verified",), "verified", False)
    at = next_speed(base.speed_ms, rules)
    checks, missing = model_checks(inputs, at, mass_kg, density, reg)

    flown = _comparable(history, mass_kg, rules)
    for name, metric in TRENDS.items():
        predicted = _trend(flown, metric, at, float(spec["trend_gap_ms"]))
        if predicted is None:
            missing.append(name)
            continue
        cap = _target_limit(reg, metric)
        checks.append(Check(name, predicted, cap, predicted < cap, "trend"))
    for name, metric in TRACKING.items():
        predicted = _trend(flown, metric, at, float(spec["trend_gap_ms"]))
        if predicted is None or metric not in base.metrics:
            missing.append(name)
            continue
        cap = float(spec["tracking_growth"]) * base.metrics[metric][0]
        checks.append(Check(name, predicted, cap, predicted <= cap, "trend"))

    failed = next((c.name for c in checks if not c.holds), None)
    required = [m for m in missing if m in spec["required"]]
    binding = failed or (required[0] if required else None)
    return Step(base, at, tuple(checks), tuple(missing), binding, binding is None)
