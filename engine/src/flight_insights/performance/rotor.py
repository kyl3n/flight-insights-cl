"""The power a multirotor's rotors need, by momentum theory (spec: Power and efficiency
analysis, "Range, endurance and speed"; rules in `knowledge/rules/performance.json`).

All functions take arrays and work element-wise, in SI units: weight and thrust in N,
speeds in m/s (climb positive up), density in kg/m^3, areas in m^2, power in W.

- **The force balance.** In steady flight the rotors' resultant force holds the weight
  and the airframe's parasite drag, `D = 1/2 rho f U^2`, which acts against the flight
  path (U the airspeed along it, f the flat-plate area). Its size is the thrust and its
  lean from vertical the tip-path tilt, which is not the body's tilt: the rotors' own
  in-plane drag (H-force) tilts the body further but takes no power beyond the induced
  and profile terms.
- **The inflow.** Through the tilted disc the air arrives at `u_n` normal to it and
  `u_p` along it. Glauert's equation gives the induced velocity,
  `v_i = v_h^2 / sqrt(u_p^2 + (u_n + v_i)^2)` with `v_h^2 = T / (2 rho A)`, solved by
  Newton's method. The ideal power is `T (u_n + v_i)`: `T u_n` is exactly the power to
  climb and to overcome the parasite drag, so neither is added again.
- **Where momentum theory fails.** Descending into its own wake with little forward
  speed (the vortex-ring and turbulent-wake states), the rotor follows no momentum
  balance; descending so fast that no air goes down through the disc (the windmill
  state, where the equation has no root with `u_n + v_i > 0`), it is not powering the
  air at all. Such points are marked and left out of a fit.
- **Profile power** of fixed-pitch propellers. Their speed follows the thrust, so their
  profile power at a given thrust scales like the induced power, `T^1.5 / sqrt(2 rho A)`,
  and grows with the in-plane speed as `1 + K mu^2`. With the propellers' measured speed
  (ESC RPM) it is computed per propeller from the tip speed instead.

A coaxial pair shares one disc: the disc area is that of the distinct arms.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

Array = np.ndarray[Any, Any]
Values = float | Array  # a number, or numbers element-wise
NEWTON_STEPS = 60
NEWTON_TOLERANCE = 1e-12  # relative change in the induced velocity


@dataclass(frozen=True)
class Flow:
    """The rotors' operating point, element-wise."""

    thrust: Array  # N
    tilt: Array  # rad, the resultant force's lean from vertical
    normal: Array  # u_n, m/s, the air through the disc from the flight, downward positive
    edgewise: Array  # u_p, m/s, the air along the disc
    hover_induced: Array  # v_h, m/s
    induced: Array  # v_i, m/s
    valid: Array  # bool: momentum theory holds (not in the vortex-ring or wake states)

    @property
    def ideal_power(self) -> Array:
        """`T (u_n + v_i)`: induced, climb and parasite power of an ideal rotor."""
        return self.thrust * (self.normal + self.induced)

    @property
    def induced_power(self) -> Array:
        return self.thrust * self.induced

    @property
    def flight_power(self) -> Array:
        """`T u_n`: the power to climb and to overcome the parasite drag."""
        return self.thrust * self.normal


def force_balance(weight: Values, drag_area: Values, rho: Values, airspeed: Values,
                  climb: Values) -> tuple[Array, Array]:  # fmt: skip
    """Thrust and its tilt from vertical, holding the weight and the parasite drag."""
    w, f, r, v, c = np.broadcast_arrays(
        *(np.asarray(x, np.float64) for x in (weight, drag_area, rho, airspeed, climb))
    )
    path = np.hypot(v, c)
    drag = 0.5 * r * f * path**2
    gamma = np.arctan2(c, v)
    forward = drag * np.cos(gamma)
    up = w + drag * np.sin(gamma)
    return np.hypot(forward, up), np.arctan2(forward, up)


def induced_velocity(hover_induced: Values, normal: Values, edgewise: Values) -> Array:
    """Glauert's induced velocity, by Newton's method from the larger of v_h and the
    high-speed value: `v (sqrt(u_p^2 + (u_n + v)^2)) = v_h^2` has one root with
    `u_n + v > 0`, and the function is increasing there."""
    vh, un, up = np.broadcast_arrays(
        *(np.asarray(x, np.float64) for x in (hover_induced, normal, edgewise))
    )
    speed = np.hypot(un, up)
    v = np.maximum(vh, np.where(speed > 0, vh**2 / np.maximum(speed, 1e-12), vh))
    v = np.maximum(v, -un + 1e-9)  # start on the branch where the air goes through the disc
    for _ in range(NEWTON_STEPS):
        root = np.sqrt(up**2 + (un + v) ** 2)
        g = v * root - vh**2
        slope = root + v * (un + v) / root
        step = g / slope
        nxt = v - step
        # stay on the physical branch: never let the air stop through the disc
        nxt = np.where(un + nxt <= 0, (v + np.maximum(-un, 0.0)) / 2, nxt)
        done = np.abs(nxt - v) <= NEWTON_TOLERANCE * np.maximum(np.abs(v), 1e-9)
        v = nxt
        if bool(np.all(done)):
            break
    return v


def flow(weight: Values, drag_area: Values, rho: Values, disc_area: Values, airspeed: Values,
         climb: Values, rules: Mapping[str, Any]) -> Flow:  # fmt: skip
    """The operating point of rotors of total disc area `disc_area` carrying `weight` at
    `airspeed` (horizontal) and `climb` (positive up) against a flat-plate area
    `drag_area`."""
    thrust, tilt = force_balance(weight, drag_area, rho, airspeed, climb)
    v, c = np.broadcast_arrays(np.asarray(airspeed, np.float64), np.asarray(climb, np.float64))
    normal = v * np.sin(tilt) + c * np.cos(tilt)
    edgewise = np.abs(v * np.cos(tilt) - c * np.sin(tilt))
    vh = np.sqrt(thrust / (2 * np.asarray(rho, np.float64) * np.asarray(disc_area, np.float64)))
    vi = induced_velocity(vh, normal, edgewise)
    ring = rules["vortex_ring"]
    in_ring = (normal < -float(ring["descent_share"]) * vh) & (
        edgewise < float(ring["edgewise_share"]) * vh
    )
    # descending fast enough that no air goes down through the disc: the windmill state
    windmill = (normal < 0) & (-normal * edgewise >= vh**2)
    valid = ~(in_ring | windmill)
    return Flow(thrust, tilt, normal, edgewise, vh, vi, valid)


def profile_power(flow: Flow, coefficient: float, advance: float) -> Array:
    """Profile power without the propellers' speed: `c_p T^1.5 / sqrt(2 rho A)` times
    `1 + K'' (u_p / v_h)^2`, the propellers' speed following the thrust."""
    ideal_hover = flow.thrust * flow.hover_induced  # T^1.5 / sqrt(2 rho A)
    return coefficient * ideal_hover * (1 + advance * (flow.edgewise / flow.hover_induced) ** 2)


def profile_power_rpm(rho: Values, prop_area: float, radius: float, rpm: Values,
                      edgewise: Values, coefficient: float, mu_factor: float) -> Array:  # fmt: skip
    """Profile power of one propeller from its measured speed:
    `pi0 rho A_p (Omega R)^3 (1 + K mu^2)`, with `mu = u_p / (Omega R)`."""
    tip = np.asarray(rpm, np.float64) * 2 * np.pi / 60 * radius
    mu = np.asarray(edgewise, np.float64) / np.maximum(tip, 1e-9)
    return coefficient * np.asarray(rho, np.float64) * prop_area * tip**3 * (1 + mu_factor * mu**2)
