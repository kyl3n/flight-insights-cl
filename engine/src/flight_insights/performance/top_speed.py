"""How fast the airframe can fly level, and what stops it going faster (spec: Power and
efficiency analysis, "Range, endurance and speed", "Maximum speed"; rules in
`knowledge/rules/performance.json` "top_speed").

Three limits; the least binds.

- **Lean.** Flying at V through still air takes the lean that holding position in a wind
  of V does: the drag the flight measured (`analyzers/wind.py`, k1 and k2), carried to this
  weight and air. Per kilogram, the frame's drag grows with the air and falls with the
  weight (k2 as rho / m); the rotors' in-plane force goes over their induced velocity, so
  with the square root of both (k1 as sqrt(rho / m)). The position controller leans no
  further than its configured maximum (`PSC_ANGLE_MAX`, else `ATC_ANGLE_MAX`).
- **Thrust.** It also leans no further than keeps the throttle within 80% of full
  (`AC_AttitudeControl_Multi.cpp` `update_althold_lean_angle_max`). The throttle that
  holds a hover grows with the weight and falls with the air (the propellers' thrust at a
  given speed grows with the air), from the throttle measured at a known weight. Forward
  flight needs less throttle than the hover, so this errs slow.
- **Current.** The packs' power the model gives at V, at the lowest voltage the pack
  reaches before its failsafe (`BATT_LOW_VOLT`, else the cells' cutoff under load), within
  the least of the pack's continuous rating and `MOT_BAT_CURR_MAX`.

Above the fastest airspeed flown the answer is extrapolated, and says so.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from flight_insights.analyzers.wind import Fit, configured_lean, strongest, thrust_lean
from flight_insights.performance.battery import Pack
from flight_insights.performance.fit import G, Model
from flight_insights.performance.optimize import grid


@dataclass(frozen=True)
class Basis:
    """What a flight measured, and at what weight and air."""

    drag: Fit
    mass_kg: float
    density: float
    throttle: float | None  # the hover's throttle (CTUN.ThI), 0 to 1


def drag_at(basis: Basis, mass_kg: float, density: float) -> Fit:
    """The measured drag per kilogram carried to another weight and air."""
    scale = (density / basis.density) * (basis.mass_kg / mass_kg)
    return replace(basis.drag, k1=basis.drag.k1 * math.sqrt(scale), k2=basis.drag.k2 * scale)


def hover_throttle(basis: Basis, mass_kg: float, density: float) -> float | None:
    if basis.throttle is None:
        return None
    return basis.throttle * (mass_kg / basis.mass_kg) * (basis.density / density)


def lowest_volts(params: Mapping[str, float], pack: Pack) -> float:
    """The lowest voltage the pack reaches flying: the voltage failsafe's threshold where
    set, else the cells' cutoff under load."""
    return max(float(params.get("BATT_LOW_VOLT", 0.0) or 0.0), pack.cells * pack.chemistry.cutoff)


@dataclass(frozen=True)
class TopSpeed:
    airspeed: float  # m/s: the least of the limits
    by: str  # 'lean', 'thrust', 'current', or 'search' (nothing bound below the search's top)
    lean_ms: float
    lean_deg: float
    thrust_ms: float | None  # None with no throttle measured
    thrust_deg: float | None
    current_ms: float | None  # None with no current limit known
    current_w: float | None  # the packs' power at the current limit and lowest voltage
    extrapolated: bool  # faster than any airspeed flown


def current_speed(model: Model, rules: Mapping[str, Any], weight: float, density: float,
                  power_w: float) -> float:  # fmt: skip
    """The fastest level airspeed whose power, and every slower one's, stays within
    `power_w`; 0 when even the hover needs more."""
    spec: Mapping[str, Any] = rules["top_speed"]
    v = np.concatenate([[0.0], grid(float(spec["search_ms"]), float(spec["step_ms"]))])
    over = np.flatnonzero(model.power(weight, v, 0.0, density, rules) > power_w)
    if not len(over):
        return float(v[-1])
    return float(v[over[0] - 1]) if over[0] > 0 else 0.0


def top_speed(model: Model, rules: Mapping[str, Any], wind_rules: Mapping[str, Any],
              params: Mapping[str, float], basis: Basis, mass_kg: float, density: float,
              lowest_volts: float | None, current_a: float | None,
              flown_ms: float) -> TopSpeed:  # fmt: skip
    """The maximum level airspeed at this weight and air, and which limit binds;
    `wind_rules` is `wind.json`'s "envelope"."""
    spec: Mapping[str, Any] = rules["top_speed"]
    drag = drag_at(basis, mass_kg, density)
    lean_deg = configured_lean(params, wind_rules)
    limits: dict[str, float] = {"lean": strongest(drag, lean_deg)}
    throttle = hover_throttle(basis, mass_kg, density)
    thrust_deg = None if throttle is None else thrust_lean(throttle, wind_rules)
    if thrust_deg is not None:
        limits["thrust"] = strongest(drag, thrust_deg)
    amps = [a for a in (current_a, float(params.get("MOT_BAT_CURR_MAX", 0.0) or 0.0)) if a]
    current_w = lowest_volts * min(amps) if amps and lowest_volts else None
    if current_w is not None:
        limits["current"] = current_speed(model, rules, mass_kg * G, density, current_w)
    by = min(limits, key=lambda k: limits[k])
    airspeed = limits[by]
    if airspeed >= float(spec["search_ms"]):
        by, airspeed = "search", float(spec["search_ms"])
    return TopSpeed(
        airspeed=airspeed,
        by=by,
        lean_ms=limits["lean"],
        lean_deg=lean_deg,
        thrust_ms=limits.get("thrust"),
        thrust_deg=thrust_deg,
        current_ms=limits.get("current"),
        current_w=current_w,
        extrapolated=airspeed > flown_ms,
    )
