"""The airframe's power model fitted to its flights' points (spec: Power and efficiency
analysis, "Range, endurance and speed", "The power model"; rules in
`knowledge/rules/performance.json` "priors" and "fit").

- **The model.** At a point's weight W, airspeed V, climb Vz and air density, the rotors'
  operating point comes from momentum theory (`rotor.flow`: the thrust T holding W and
  the parasite drag of a flat-plate area f, the flow through the disc u_n, along it u_p,
  the hover induced velocity v_h and the induced velocity v_i). The packs' power is
  `P = P_av + a_i T v_i + e T u_n + a_p T v_h (1 + K'' (u_p / v_h)^2)`:
  avionics, induced power over the propulsion efficiency (a_i = kappa / eta), the climb
  and parasite power (e = 1 / eta), and profile power (a_p = c_p / eta, growing with the
  in-plane speed). Each flight's power may sit a little above or below the airframe's (a
  mass a little off, a sensor's scale, a pack's state): a factor per flight, drawn to 1.
- **The fit.** For a given f the model is linear in (P_av, a_i, e, a_p, a_p K): weighted
  least squares on relative errors, each point weighted by its seconds, with each
  coefficient drawn to its prior (from the literature, marked uncalibrated), and a Huber
  weight on outliers, by iteratively reweighting. f is the one-dimensional search around
  it. Points where momentum theory fails (the vortex ring, the windmill state) are left out.
- **What the data supports.** A: level flight over a span of airspeeds at two or more
  weights; B: level flight at one weight, or few speeds (the shape partly from theory);
  C: hovers and climbs only (hover endurance measured, the rest theory).
- **How sure.** Each flight's energy over its points predicted with that flight left out
  (the leave-one-flight-out error), and a bootstrap over flights for the coefficients.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from flight_insights.performance.rotor import flow

Array = np.ndarray[Any, Any]
G = 9.80665
NAMES = ("avionics", "induced", "flight", "profile", "growth")


@dataclass(frozen=True)
class Row:
    """One point with what the fit needs."""

    flight: str
    weight: float  # N
    airspeed: float
    climb: float
    power: float  # W
    density: float
    seconds: float


@dataclass(frozen=True)
class Model:
    avionics: float  # P_av, W
    induced: float  # a_i = kappa / eta
    flight: float  # e = 1 / eta, on the climb and parasite power
    profile: float  # a_p = c_p / eta
    growth: float  # K'', profile power's growth with (u_p / v_h)^2
    drag_area: float  # f, m^2
    disc_area: float  # A, m^2
    rms: float  # of the relative residuals, weighted by time
    factors: Mapping[str, float] = field(default_factory=dict[str, float])  # per flight

    def power(self, weight: Array | float, airspeed: Array | float, climb: Array | float,
              density: Array | float, rules: Mapping[str, Any]) -> Array:  # fmt: skip
        """The packs' power the model gives, W."""
        f = flow(weight, self.drag_area, density, self.disc_area, airspeed, climb, rules)
        x = _columns(f.thrust, f.normal, f.edgewise, f.hover_induced, f.induced)
        coef = np.array([self.avionics, self.induced, self.flight, self.profile,
                         self.profile * self.growth])  # fmt: skip
        return np.asarray(x @ coef)


@dataclass(frozen=True)
class Fitted:
    model: Model
    level: str  # 'A', 'B' or 'C'
    rows: int
    flights: int
    lofo: Mapping[str, float]  # flight: relative error of its energy predicted without it
    bootstrap: Mapping[str, tuple[float, float]]  # coefficient: 95% interval
    left_out: int  # points where momentum theory fails
    replicates: tuple[Model, ...] = ()  # the bootstrap's models, for the projections' spread


def _columns(thrust: Array, normal: Array, edgewise: Array, vh: Array, vi: Array) -> Array:
    ratio = edgewise / np.maximum(vh, 1e-9)
    return np.stack(
        [np.ones_like(thrust), thrust * vi, thrust * normal, thrust * vh, thrust * vh * ratio**2],
        axis=-1,
    )


def priors(rules: Mapping[str, Any], coaxial: bool, scale_w: float) -> tuple[Array, Array]:
    """Each coefficient's prior mean and spread: (P_av, a_i, e, a_p, a_p K)."""
    p: Mapping[str, Any] = rules["priors"]
    kappa = p["kappa_coaxial" if coaxial else "kappa_open"]
    eta = p["efficiency"]
    cp = p["profile_coefficient"]
    k = p["profile_growth_hover"]
    e_mean = 1.0 / float(eta["mean"])
    e_spread = float(eta["spread"]) / float(eta["mean"]) ** 2
    ai_mean = float(kappa["mean"]) * e_mean
    ai_spread = ai_mean * math.hypot(float(kappa["spread"]) / float(kappa["mean"]),
                                     e_spread / e_mean)  # fmt: skip
    ap_mean = float(cp["mean"]) * e_mean
    ap_spread = ap_mean * math.hypot(float(cp["spread"]) / float(cp["mean"]), e_spread / e_mean)
    apk_mean = ap_mean * float(k["mean"])
    apk_spread = apk_mean * math.hypot(ap_spread / ap_mean, float(k["spread"]) / float(k["mean"]))
    avionics_spread = float(rules["fit"]["avionics_share"]) * scale_w
    mean = np.array([0.0, ai_mean, e_mean, ap_mean, apk_mean])
    spread = np.array([avionics_spread, ai_spread, e_spread, ap_spread, apk_spread])
    return mean, spread


@dataclass(frozen=True)
class _Data:
    x: Array  # (n, 5)
    power: Array
    seconds: Array
    flight: Array  # (n,) index into names
    names: tuple[str, ...]


def _prepare(rows: Sequence[Row], drag_area: float, disc_area: float,
             rules: Mapping[str, Any]) -> tuple[_Data, Array]:  # fmt: skip
    w = np.array([r.weight for r in rows])
    f = flow(
        w,
        drag_area,
        np.array([r.density for r in rows]),
        disc_area,
        np.array([r.airspeed for r in rows]),
        np.array([r.climb for r in rows]),
        rules,
    )
    names = tuple(sorted({r.flight for r in rows}))
    index = {n: k for k, n in enumerate(names)}
    data = _Data(
        _columns(f.thrust, f.normal, f.edgewise, f.hover_induced, f.induced),
        np.array([r.power for r in rows]),
        np.array([r.seconds for r in rows]),
        np.array([index[r.flight] for r in rows]),
        names,
    )
    return data, np.asarray(f.valid, bool)


def _solve(d: _Data, mean: Array, spread: Array, rules: Mapping[str, Any]
           ) -> tuple[Array, Array, float]:  # fmt: skip
    """Coefficients, per-flight factors and the cost (the profile likelihood with the
    priors), for fixed rows."""
    spec: Mapping[str, Any] = rules["fit"]
    tau = float(spec["random_effect_spread"])
    huber = float(spec["huber"])
    floor = float(spec["noise_floor"])
    sigma = float(spec["noise_start"])
    factors = np.ones(len(d.names))
    robust = np.ones(len(d.power))
    coef = mean.copy()
    weight: Array = d.seconds / np.sum(d.seconds) * len(d.seconds)
    for _ in range(int(spec["irls_steps"])):
        scale = factors[d.flight]
        # relative errors: each row divided by its observed power and the noise
        rw = np.sqrt(weight * robust) / (d.power * sigma)
        a = np.vstack([d.x * (scale * rw)[:, None], np.diag(1.0 / spread)])
        b = np.concatenate([d.power * rw, mean / spread])
        coef, *_ = np.linalg.lstsq(a, b, rcond=None)
        coef = np.maximum(coef, 0.0)
        model = np.maximum(d.x @ coef, 1e-9)
        rel: Array = np.log(np.maximum(d.power, 1e-9) / model)
        # each flight's factor: its mean log residual, drawn to 1; together they average 1,
        # so the airframe's own level stays in the coefficients
        share = np.zeros(len(d.names))
        for j in range(len(d.names)):
            mine: Array = np.asarray(d.flight == j)
            share[j] = float(np.sum(weight[mine]))
            factors[j] = math.exp(float(np.sum(weight[mine] * rel[mine]))
                                  / (share[j] + sigma**2 / tau**2))  # fmt: skip
        factors /= math.exp(float(np.average(np.log(factors), weights=share)))
        resid = (d.power - factors[d.flight] * model) / d.power
        sigma = max(float(np.sqrt(np.average(resid**2, weights=weight * robust))), floor)
        robust = np.minimum(1.0, huber * sigma / np.maximum(np.abs(resid), 1e-12))
    cost = (len(d.power) * math.log(sigma**2) + float(np.sum(((coef - mean) / spread) ** 2))
            + float(np.sum(np.log(factors) ** 2)) / tau**2)  # fmt: skip
    return coef, factors, cost


def fit(rows: Sequence[Row], disc_area: float, coaxial: bool, rules: Mapping[str, Any]
        ) -> Model | None:  # fmt: skip
    """The model fitted to `rows`, or None with too few."""
    spec: Mapping[str, Any] = rules["fit"]
    if len(rows) < int(spec["min_rows"]):
        return None
    mean, spread = priors(rules, coaxial, float(np.median([r.power for r in rows])))

    def at(f: float) -> tuple[float, Array, Array, _Data]:
        d, valid = _prepare(rows, f, disc_area, rules)
        kept = _Data(d.x[valid], d.power[valid], d.seconds[valid], d.flight[valid], d.names)
        coef, factors, cost = _solve(kept, mean, spread, rules)
        return cost, coef, factors, kept

    lo, hi = 0.0, float(spec["drag_area_max_m2"])
    golden = (math.sqrt(5) - 1) / 2
    c, dd = hi - golden * (hi - lo), lo + golden * (hi - lo)
    fc, fd = at(c)[0], at(dd)[0]
    for _ in range(int(spec["drag_area_steps"])):
        if fc < fd:
            hi, dd, fd = dd, c, fc
            c = hi - golden * (hi - lo)
            fc = at(c)[0]
        else:
            lo, c, fc = c, dd, fd
            dd = lo + golden * (hi - lo)
            fd = at(dd)[0]
    best = (lo + hi) / 2
    _, coef, factors, d = at(best)
    model = d.x @ coef
    resid = (d.power - factors[d.flight] * model) / d.power
    rms = float(np.sqrt(np.average(resid**2, weights=d.seconds)))
    return Model(float(coef[0]), float(coef[1]), float(coef[2]), float(coef[3]),
                 float(coef[4] / coef[3]) if coef[3] > 0 else 0.0, best, disc_area, rms,
                 {n: float(factors[j]) for j, n in enumerate(d.names)})  # fmt: skip


def level(rows: Sequence[Row], rules: Mapping[str, Any]) -> str:
    """What the points can support: A, B or C."""
    spec: Mapping[str, Any] = rules["fit"]["levels"]
    flown = [r for r in rows if r.airspeed >= float(spec["moving_ms"]) and abs(r.climb) < 0.3]
    if not flown:
        return "C"
    span = max(r.airspeed for r in flown) - min(r.airspeed for r in flown)
    weights = sorted({r.weight for r in flown})
    apart = len(weights) >= 2 and weights[-1] / weights[0] - 1 >= float(spec["weight_apart"])
    if span >= float(spec["airspeed_span_ms"]) and apart:
        return "A"
    return "B"


def energy_error(model: Model, rows: Sequence[Row], rules: Mapping[str, Any]) -> float:
    """The relative error of a flight's energy over its points, predicted by the model."""
    w = np.array([r.weight for r in rows])
    p = model.power(w, np.array([r.airspeed for r in rows]), np.array([r.climb for r in rows]),
                    np.array([r.density for r in rows]), rules)  # fmt: skip
    seconds = np.array([r.seconds for r in rows])
    observed = float(np.sum(np.array([r.power for r in rows]) * seconds))
    return float(np.sum(p * seconds)) / observed - 1.0


def fitted(rows: Sequence[Row], disc_area: float, coaxial: bool, rules: Mapping[str, Any]
           ) -> Fitted | None:  # fmt: skip
    """The model with what the data supports and how sure it is."""
    model = fit(rows, disc_area, coaxial, rules)
    if model is None:
        return None
    spec: Mapping[str, Any] = rules["fit"]
    names = sorted({r.flight for r in rows})
    lofo: dict[str, float] = {}
    if len(names) >= 2:
        for name in names:
            without = fit([r for r in rows if r.flight != name], disc_area, coaxial, rules)
            if without is not None:
                lofo[name] = energy_error(without, [r for r in rows if r.flight == name], rules)
    rng = np.random.default_rng(int(spec["seed"]))
    draws: dict[str, list[float]] = {n: [] for n in (*NAMES, "drag_area")}
    replicates: list[Model] = []
    if len(names) >= 2:
        by = {n: [r for r in rows if r.flight == n] for n in names}
        for _ in range(int(spec["bootstrap"])):
            picked = rng.choice(len(names), len(names))
            # a flight drawn twice counts as two flights, each with its own factor
            sample = [
                Row(f"{k}:{r.flight}", r.weight, r.airspeed, r.climb, r.power, r.density, r.seconds)
                for k, j in enumerate(picked)
                for r in by[names[j]]
            ]
            again = fit(sample, disc_area, coaxial, rules)
            if again is None:
                continue
            replicates.append(again)
            for n in draws:
                draws[n].append(float(getattr(again, n)))
    tail = (1.0 - float(spec["level"])) / 2 * 100
    bootstrap = {n: (float(np.percentile(v, tail)), float(np.percentile(v, 100 - tail)))
                 for n, v in draws.items() if v}  # fmt: skip
    valid = flow(np.array([r.weight for r in rows]), model.drag_area,
                 np.array([r.density for r in rows]), disc_area,
                 np.array([r.airspeed for r in rows]), np.array([r.climb for r in rows]),
                 rules).valid  # fmt: skip
    return Fitted(model, level(rows, rules), len(rows), len(names), lofo, bootstrap,
                  int(np.count_nonzero(~np.asarray(valid, bool))), tuple(replicates))  # fmt: skip
