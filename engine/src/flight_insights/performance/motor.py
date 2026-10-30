"""The motor and its ESC as one drive: the electrical power it draws to turn a propeller at
a speed against a torque (spec: Power and efficiency analysis, "Propeller and motor
choice"; rules in `knowledge/rules/performance.json` "motor").

- **The model** (Drela, "First-order DC electric motor model" and "Second-order DC electric
  motor model", MIT, 2006-2007). With the speed constant k (Kv in rad/s per volt) and the
  torque constant taken equal to it in SI units:

      i = Q k + i0(Omega),   i0 = i00 + i01 Omega + i02 Omega^2   (no-load current)
      v = Omega / k + i R(i),   R = r0 + r2 i^2                   (resistance, heating up)
      P = v i = Q Omega + i0 Omega / k + i^2 R                     (shaft + iron + copper)

  P is fitted to the power the ESC draws, so the ESC's own losses fall into R and i0, and
  a table and a flight are read the same way: power in, against speed and torque.
- **From a maker's tables** with each step's speed and torque (one table, or the same
  motor on several propellers, fitted together): R and the no-load current by least
  squares on the relative error of the power drawn, each drawn to a small prior so
  a short table stays well posed. Kv is the table's, or the setup's; when the table runs
  to full throttle with its voltage, Kv is checked against the top step (the motor's
  voltage there is the supply's times the ESC's top duty) and taken from it.
- **From the datasheet** (the setup's Kv, resistance and no-load current): the first-order
  model, the no-load current held constant.
- **The fastest it turns a propeller** at a supply voltage: the speed at which the voltage
  the motor needs reaches the ESC's top duty of it.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.performance.bench import Row

Array = np.ndarray[Any, Any]
RPM_TO_RAD = 2 * math.pi / 60
NAMES = ("r0", "r2", "i00", "i01", "i02")


@dataclass(frozen=True)
class Motor:
    kv: float  # rpm per volt
    r0: float  # ohm
    r2: float = 0.0  # ohm per A^2
    i00: float = 0.0  # A
    i01: float = 0.0  # A per rad/s
    i02: float = 0.0  # A per (rad/s)^2
    source: str = "datasheet"  # 'table' or 'datasheet'
    kv_from: str = "given"  # 'given' or 'full throttle'
    rms: float | None = None  # of the relative power residuals, where fitted
    steps: int = 0

    @property
    def k(self) -> float:
        """Kv in rad/s per volt."""
        return self.kv * RPM_TO_RAD

    def current(self, torque: Array | float, omega: Array | float) -> Array:
        """The motor's current, A, at a shaft torque (N m) and speed (rad/s)."""
        w = np.asarray(omega, float)
        return np.asarray(torque, float) * self.k + self.i00 + self.i01 * w + self.i02 * w**2

    def volts(self, torque: Array | float, omega: Array | float) -> Array:
        """The voltage the motor needs, V."""
        i = self.current(torque, omega)
        return np.asarray(omega, float) / self.k + i * (self.r0 + self.r2 * i**2)

    def power(self, torque: Array | float, omega: Array | float) -> Array:
        """The electrical power the drive draws, W."""
        return self.volts(torque, omega) * self.current(torque, omega)

    def efficiency(self, torque: Array | float, omega: Array | float) -> Array:
        """Shaft power over the power drawn."""
        q, w = np.asarray(torque, float), np.asarray(omega, float)
        return q * w / self.power(q, w)

    def losses(self, torque: Array | float, omega: Array | float) -> tuple[Array, Array]:
        """(iron, copper) losses, W: the no-load current against the back-EMF, and the
        current through the resistance."""
        i = self.current(torque, omega)
        w = np.asarray(omega, float)
        i0 = self.i00 + self.i01 * w + self.i02 * w**2
        return i0 * w / self.k, i**2 * (self.r0 + self.r2 * i**2)

    def top_speed(self, torque_at: Callable[[Array], Array], supply_v: float,
                  duty: float) -> float:  # fmt: skip
        """The fastest the motor turns a load whose torque grows with speed (a propeller),
        rad/s: where the voltage it needs reaches `duty` of the supply."""
        available = duty * supply_v
        lo, hi = 0.0, self.k * available
        for _ in range(80):
            mid = (lo + hi) / 2
            need = float(self.volts(torque_at(np.asarray(mid)), mid))
            lo, hi = (mid, hi) if need < available else (lo, mid)
        return (lo + hi) / 2


def from_datasheet(kv: float, resistance_mohm: float, no_load_a: float = 0.0) -> Motor:
    """The first-order model from the setup's datasheet values."""
    return Motor(kv=kv, r0=resistance_mohm / 1000.0, i00=no_load_a, source="datasheet")


def _usable(rows: Sequence[Row]) -> list[Row]:
    return [r for r in rows if r.rpm is not None and r.torque_nm is not None]


def _fit(kv: float, q: Array, w: Array, p: Array, rules: Mapping[str, Any]) -> tuple[Array, float]:
    """R and the no-load current for a given Kv: Levenberg-Marquardt on the relative
    power error, each parameter drawn to its prior and kept at or above zero."""
    spec: Mapping[str, Any] = rules["motor"]
    prior = np.array([float(spec["priors"][n]["mean"]) for n in NAMES])
    spread = np.array([float(spec["priors"][n]["spread"]) for n in NAMES])
    # a first guess: the copper loss and the no-load current from the two ends of the table
    x = prior.copy()

    def resid(x: Array) -> Array:
        m = Motor(kv, *x)
        return np.concatenate(
            [(m.power(q, w) - p) / p, (x - prior) / spread * float(spec["prior_weight"])]
        )

    lam = 1e-3
    r = resid(x)
    cost = float(r @ r)
    for _ in range(int(spec["iterations"])):
        jac = np.empty((len(r), len(x)))
        for j in range(len(x)):
            step = max(abs(x[j]) * 1e-6, 1e-9)
            dx = x.copy()
            dx[j] += step
            jac[:, j] = (resid(dx) - r) / step
        a = jac.T @ jac
        g = jac.T @ r
        while True:
            trial = np.maximum(x - np.linalg.solve(a + lam * np.diag(np.diag(a) + 1e-12), g), 0.0)
            rt = resid(trial)
            ct = float(rt @ rt)
            if ct < cost:
                x, r, cost, lam = trial, rt, ct, max(lam / 3, 1e-9)
                break
            lam *= 4
            if lam > 1e9:
                break
        if lam > 1e9 or float(np.max(np.abs(g))) < 1e-12:
            break
    fit_err = (Motor(kv, *x).power(q, w) - p) / p
    return x, float(np.sqrt(np.mean(fit_err**2)))


def from_table(rows: Sequence[Row], kv: float, rules: Mapping[str, Any]) -> Motor | None:
    """The drive fitted to a maker's table with each step's speed and torque; None with
    fewer usable steps than the rules ask for."""
    return from_tables([rows], kv, rules)


def from_tables(tables: Sequence[Sequence[Row]], kv: float,
                rules: Mapping[str, Any]) -> Motor | None:  # fmt: skip
    """One motor fitted to all its makers' tables at once (the same motor on several
    propellers: its constants do not depend on the propeller, and more operating points
    pin them better); None with fewer usable steps than the rules ask for."""
    spec: Mapping[str, Any] = rules["motor"]
    use = [r for rows in tables for r in _usable(rows)]
    if len(use) < int(spec["min_steps"]) or kv <= 0:
        return None
    q = np.array([r.torque_nm for r in use], float)
    w = np.array([r.rpm for r in use], float) * RPM_TO_RAD
    p = np.array([r.power_w for r in use], float)
    x, rms = _fit(kv, q, w, p, rules)
    kv_from = "given"
    # at full throttle the motor sees the supply's voltage times the ESC's top duty
    tops = [max(u, key=lambda r: r.throttle_pct) for u in map(_usable, tables) if u]
    tops = [r for r in tops if r.throttle_pct >= float(spec["full_throttle_pct"]) and r.volts]
    if tops:
        duty = float(spec["full_duty"])
        for _ in range(int(spec["kv_rounds"])):
            m = Motor(kv, *x)
            found: list[float] = []
            for top in tops:
                assert top.torque_nm is not None and top.rpm is not None and top.volts
                omega = top.rpm * RPM_TO_RAD
                i = float(m.current(top.torque_nm, omega))
                back_emf = duty * top.volts - i * (m.r0 + m.r2 * i**2)
                if back_emf > 0:
                    found.append(omega / back_emf / RPM_TO_RAD)
            if not found:
                break
            kv = float(np.mean(found))
            x, rms = _fit(kv, q, w, p, rules)
            kv_from = "full throttle"
    return Motor(kv, *map(float, x), source="table", kv_from=kv_from, rms=rms,
                 steps=len(use))  # fmt: skip
