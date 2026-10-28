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
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.wind import configured_lean, lean_needed, thrust_lean
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


def step(history: Sequence[Fast], inputs: Inputs, mass_kg: float, density: float,
         reg: Registry) -> Step:  # fmt: skip
    """The next step at this weight and air, what the models predict there, and whether it
    is offered."""
    rules: Mapping[str, Any] = reg.rules["performance"]
    wind_rules: Mapping[str, Any] = reg.rules["wind"]["envelope"]
    spec: Mapping[str, Any] = rules["envelope"]
    base = verified(history, mass_kg, rules)
    if base is None:
        return Step(None, None, (), ("verified",), "verified", False)
    at = next_speed(base.speed_ms, rules)
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
