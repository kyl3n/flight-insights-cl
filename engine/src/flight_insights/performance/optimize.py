"""The speeds to fly, from the airframe's power model (spec: Power and efficiency analysis,
"Range, endurance and speed", "What the user gets"; rules in
`knowledge/rules/performance.json` "speeds").

- **Best endurance:** the airspeed of least power.
- **Best range:** the airspeed of least energy per metre through the air, the tangent from
  the origin to power against airspeed. With the pack's resistance given, the energy is
  what leaves the cells: the power at the terminals plus the pack's own heat.
- **Long-range cruise:** the faster airspeed that keeps the rules' share of the best range
  (99%): a little range for a lot of time.
- **Each with its band:** the airspeeds within the rules' share (2%) of its best.
- **Into a wind:** least energy per metre over the ground, the power over the ground speed.
  A wind along the track adds to the ground speed; one across it takes airspeed to hold
  the track: the ground speed is `along + sqrt(V^2 - across^2)`.
- **Out and back at one speed setting:** ArduPilot holds the ground speed (`WP_SPD`), so
  one setting flies both legs at the same ground speed, faster through the air into the
  wind: the best setting has the least energy for the two legs per metre.

Airspeeds are searched on a grid up to a cap (the verified speed, or the model's top), so
the speeds stay where the model was fitted unless asked otherwise.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.performance.fit import Model

Array = np.ndarray[Any, Any]
Cells = Callable[[Array], Array]  # terminal power -> power leaving the cells, W


def terminals(power: Array) -> Array:
    return power


def with_resistance(volts: float, ohms: float) -> Cells:
    """The cells' power for a terminal power P from an open-circuit voltage and resistance:
    `I = (E - sqrt(E^2 - 4RP)) / 2R`, the cells giving `E I`."""

    def cells(power: Array) -> Array:
        if ohms <= 0:
            return power
        disc = np.maximum(volts**2 - 4 * ohms * power, 0.0)
        return volts * (volts - np.sqrt(disc)) / (2 * ohms)

    return cells


@dataclass(frozen=True)
class Speeds:
    endurance: float  # m/s, airspeed
    endurance_band: tuple[float, float]
    range: float  # m/s, airspeed
    range_band: tuple[float, float]
    long_range: float  # m/s
    endurance_w: float  # the power there, W
    range_wh_per_km: float  # through the air, from the cells


def grid(top: float, step: float) -> Array:
    return np.arange(step, top + step / 2, step)


def _band(v: Array, cost: Array, share: float) -> tuple[float, float]:
    near = v[cost <= cost.min() * (1 + share)]
    return float(near.min()), float(near.max())


def best(model: Model, weight: float, density: float, rules: Mapping[str, Any],
         top: float, cells: Cells = terminals) -> Speeds:  # fmt: skip
    """The speeds of least power and least energy per metre, at a weight (N) and air."""
    spec: Mapping[str, Any] = rules["speeds"]
    v = grid(top, float(spec["step_ms"]))
    v = np.concatenate([[0.0], v])
    power = cells(model.power(weight, v, 0.0, density, rules))
    k = int(np.argmin(power))
    per_metre = power[1:] / v[1:]
    j = int(np.argmin(per_metre))
    best_range = 1.0 / per_metre[j]
    keep = 1.0 / per_metre >= float(spec["long_range_share"]) * best_range
    faster = np.flatnonzero(keep & (v[1:] >= v[1:][j]))
    share = float(spec["band_share"])
    return Speeds(
        endurance=float(v[k]),
        endurance_band=_band(v, power, share),
        range=float(v[1:][j]),
        range_band=_band(v[1:], per_metre, share),
        long_range=float(v[1:][faster[-1]]),
        endurance_w=float(power[k]),
        range_wh_per_km=float(per_metre[j]) * 1000 / 3600,
    )


def airspeed_for(ground: Array | float, along: float, across: float) -> Array:
    """The airspeed that holds a track at this ground speed in a wind `along` it (positive
    behind) and `across` it."""
    return np.hypot(np.asarray(ground, np.float64) - along, across)


def ground_for(airspeed: Array, along: float, across: float) -> Array:
    """The ground speed along the track at this airspeed: nan where the airspeed cannot
    hold the track against the crosswind or make way into the headwind."""
    hold = airspeed**2 - across**2
    g = along + np.sqrt(np.maximum(hold, 0.0))
    return np.where((hold > 0) & (g > 0), g, np.nan)


@dataclass(frozen=True)
class Leg:
    airspeed: float  # m/s
    ground: float  # m/s
    power: float  # W at the terminals
    wh_per_km: float  # over the ground, from the cells


def leg(model: Model, weight: float, density: float, rules: Mapping[str, Any], top: float,
        along: float, across: float, cells: Cells = terminals) -> Leg | None:  # fmt: skip
    """The leg's best airspeed in its wind, up to `top`: least energy per metre over the
    ground. None when no airspeed up to `top` makes way."""
    v = grid(top, float(rules["speeds"]["step_ms"]))
    g = ground_for(v, along, across)
    if not np.isfinite(g).any():
        return None
    p = model.power(weight, v, 0.0, density, rules)
    cost = np.where(np.isfinite(g), cells(p) / np.where(np.isfinite(g), g, 1.0), np.inf)
    k = int(np.argmin(cost))
    return Leg(float(v[k]), float(g[k]), float(p[k]), float(cost[k]) * 1000 / 3600)


def one_setting(model: Model, weight: float, density: float, rules: Mapping[str, Any],
                top: float, along: float, across: float,
                cells: Cells = terminals) -> tuple[float, float] | None:  # fmt: skip
    """The ground speed to set for out and back (ArduPilot holds it on both legs) in a wind
    `along` the outbound track and `across` it, and its energy per km of the two legs
    (Wh/km of radius), with neither leg's airspeed above `top`. None when none makes way."""
    g = grid(top + abs(along), float(rules["speeds"]["step_ms"]))
    out = airspeed_for(g, along, across)
    back = airspeed_for(g, -along, across)
    ok = (out <= top) & (back <= top)
    if not ok.any():
        return None
    p = (cells(model.power(weight, out, 0.0, density, rules))
         + cells(model.power(weight, back, 0.0, density, rules)))  # fmt: skip
    cost = np.where(ok, p / g, np.inf)
    k = int(np.argmin(cost))
    return float(g[k]), float(cost[k]) * 1000 / 3600
