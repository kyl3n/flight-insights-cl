"""How far out the airframe can go and come back, for the standard trips (spec: Power and
efficiency analysis, "Range, endurance and speed": the radius; rules in
`knowledge/rules/performance.json` "trips").

The fleet owner's decision (2026-10-27): radius for fixed trip types only, never a route.

- **The trips.** Out and back; out loaded, drop the payload, back light; out and back with
  minutes at the site. Each climbs to the trip's height, flies out, hovers at the site (the
  drop's hover, or the site's minutes), flies back and descends to land.
- **The legs.** Each leg flies its own best airspeed in its wind (least energy per metre
  over the ground), up to a cap: the verified speed. The wind blows at an angle to the
  track; along it, it helps one leg and hinders the other, across it both legs crab.
- **The energy.** The climb at its rate in the model, the hover at the site, and the
  descent at hover power (the model is not trusted descending into its own wake); the
  legs' energy per metre over the ground. All of it fixed except the legs, so the radius
  is the energy left for the legs over their energy per metre, both ways.
- **The worst direction.** The radius for each direction of travel relative to the wind,
  and the least of them.

The budget is the energy to plan with: what the pack gives before the reserve
(`battery.usable`), with the reserve the fleet owner set kept back.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.performance.fit import G, Model
from flight_insights.performance.optimize import Cells, Leg, leg, terminals


@dataclass(frozen=True)
class Trip:
    kind: str  # 'out_and_back', 'drop' or 'site'
    height_m: float
    climb_ms: float
    descent_ms: float
    site_s: float  # hover at the site: the drop's, or the site's minutes
    drop_kg: float = 0.0  # left at the site on a drop


def standard(kind: str, rules: Mapping[str, Any], drop_kg: float = 0.0) -> Trip:
    spec: Mapping[str, Any] = rules["trips"]
    site = {"out_and_back": 0.0, "drop": float(spec["drop_hover_s"]),
            "site": float(spec["site_minutes"]) * 60}[kind]  # fmt: skip
    return Trip(kind, float(spec["height_m"]), float(spec["climb_ms"]),
                float(spec["descent_ms"]), site, drop_kg if kind == "drop" else 0.0)  # fmt: skip


@dataclass(frozen=True)
class Radius:
    metres: float
    out: Leg
    back: Leg
    fixed_wh: float  # climb, site and descent


def radius(model: Model, rules: Mapping[str, Any], mass_kg: float, density: float,
           trip: Trip, budget_wh: float, wind_ms: float, angle_rad: float, top: float,
           cells: Cells = terminals) -> Radius | None:  # fmt: skip
    """The trip's radius flying out at `angle_rad` from where the wind goes (0: the wind
    behind on the way out), or None when a leg cannot make way or the fixed energy alone
    spends the budget."""
    heavy = mass_kg * G
    light = (mass_kg - trip.drop_kg) * G
    along, across = wind_ms * math.cos(angle_rad), wind_ms * math.sin(angle_rad)
    out = leg(model, heavy, density, rules, top, along, across, cells)
    back = leg(model, light, density, rules, top, -along, across, cells)
    if out is None or back is None:
        return None

    def at(weight: float, climb: float) -> float:
        return float(cells(model.power(weight, 0.0, climb, density, rules))[()])

    climb_s = trip.height_m / trip.climb_ms
    descent_s = trip.height_m / trip.descent_ms
    fixed = (at(heavy, trip.climb_ms) * climb_s + at(heavy, 0.0) * trip.site_s
             + at(light, 0.0) * descent_s) / 3600  # fmt: skip
    per_metre = (out.wh_per_km + back.wh_per_km) / 1000
    left = budget_wh - fixed
    if left <= 0:
        return None
    return Radius(left / per_metre, out, back, fixed)


def worst(model: Model, rules: Mapping[str, Any], mass_kg: float, density: float, trip: Trip,
          budget_wh: float, wind_ms: float, top: float, cells: Cells = terminals
          ) -> tuple[float, list[tuple[float, float]]]:  # fmt: skip
    """The least radius over the directions of travel relative to the wind, and the radius
    in each (degrees, metres); a direction no leg can fly counts as 0."""
    step = float(rules["trips"]["direction_step_deg"])
    by: list[tuple[float, float]] = []
    for deg in np.arange(0.0, 360.0, step):
        r = radius(model, rules, mass_kg, density, trip, budget_wh, wind_ms,
                   math.radians(float(deg)), top, cells)  # fmt: skip
        by.append((float(deg), 0.0 if r is None else r.metres))
    return min(m for _, m in by), by
