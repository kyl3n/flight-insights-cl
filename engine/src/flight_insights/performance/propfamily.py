"""Propeller families: blade-element theory for a fixed-pitch propeller in hover, calibrated
per family on makers' tables, and a candidate propeller predicted as a change from the one
the aircraft flies (spec: Power and efficiency analysis, "Propeller and motor choice";
rules in `knowledge/rules/props.json`).

- **The theory** (Leishman, Principles of Helicopter Aerodynamics, ch. 3): blade-element
  momentum theory in hover. At each radial station the blade's angle is the propeller's
  helical pitch, atan(p / (pi D r)), less the section's zero-lift angle; the inflow
  balances the blade's lift against the momentum it gives the air, with Prandtl's tip
  loss; the lift slope rises with the station's Mach number, and the profile drag falls
  with its Reynolds number. It gives the thrust and power coefficients (C_T = T/(rho n^2
  D^4), C_P = P/(rho n^3 D^5)) of a propeller of any diameter, pitch and blade count at
  any speed and air.
- **A family** is a maker's line whose blades share their planform and section and
  differ in diameter and pitch. Its theory parameters (lift slope, zero-lift angle,
  profile drag and its Reynolds-number exponent) start from literature values and are
  calibrated on every table of the family a person entered, by least squares on the
  logarithms of the coefficients. Each propeller of the family keeps its own two offsets
  from the theory (its thrust coefficient's, and its power's at the same thrust), and the
  spread of those offsets between propellers is measured as tables accumulate: it is
  how far a propeller nobody has measured may sit from its family's theory.
- **A candidate** is predicted as a change from the propeller flown: the operating point
  measured on the aircraft (its anchor), times the theory's ratio between the candidate
  and the current propeller, with each one's own offset where a table measured it. What
  the aircraft does to both propellers alike (its arms, its frame, a coaxial partner, the
  error of its weight) cancels in the ratio. The interval holds the theory parameters'
  uncertainty, the offsets of propellers no table measured, and how much the aircraft's
  own losses may differ between two propellers.

All coefficients use the propeller's convention, on revolutions a second; powers are the
shaft's. No flight data is read here: the anchor and the tables' points come in.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from flight_insights.performance.bench import Bench
from flight_insights.performance.propeller import INCH, TablePoint, table_points

Array = np.ndarray[Any, Any]
Params = Sequence[float] | Array  # the theory parameters, in NAMES order
NAMES = ("lift_slope", "zero_lift_deg", "cd0", "cd2", "re_exponent")
CT_ROTOR = math.pi**3 / 4  # C_T(prop) = CT_ROTOR C_T(rotor, on the tip speed)
CP_ROTOR = math.pi**4 / 4


@dataclass(frozen=True)
class Prop:
    family: str
    diameter_in: float
    pitch_in: float
    blades: int = 2

    @property
    def key(self) -> str:
        return f"{self.family}:{self.diameter_in:g}x{self.pitch_in:g}x{self.blades}"


@dataclass(frozen=True)
class Operating:
    """A propeller's measured operating point: its speed, coefficients and the air."""

    rpm: float
    ct: float
    cp: float | None  # the shaft power's, where it is known
    density: float


@dataclass(frozen=True)
class Offsets:
    """A propeller's own departure from its family's theory, from its tables."""

    prop: Prop
    thrust: float  # log of its C_T over the theory's
    power: float  # log of its power at the same thrust over the theory's
    points: int


@dataclass(frozen=True)
class Family:
    id: str
    params: tuple[float, ...]  # the theory parameters, in NAMES order
    offsets: tuple[Offsets, ...]
    covariance: tuple[tuple[float, ...], ...]  # of params, then each one's two offsets
    spread_thrust: float  # between propellers, log
    spread_power: float
    noise: float  # a table's step-to-step scatter about its propeller's curve, log
    rms: float | None  # of the fitted tables' residuals, log
    tables: int

    @property
    def calibrated(self) -> bool:
        return self.tables > 0

    def index(self, prop: Prop) -> int | None:
        """The position of the propeller's offsets in the covariance, or None when no
        table of it was used."""
        for i, o in enumerate(self.offsets):
            if o.prop.key == prop.key:
                return len(NAMES) + 2 * i
        return None

    def vector(self) -> Array:
        return np.array([*self.params, *(v for o in self.offsets for v in (o.thrust, o.power))])

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id, "calibrated": self.calibrated, "tables": self.tables,
            "params": dict(zip(NAMES, self.params, strict=True)),
            "spread_thrust": self.spread_thrust, "spread_power": self.spread_power,
            "noise": self.noise, "rms": self.rms,
            "propellers": [{"diameter_in": o.prop.diameter_in, "pitch_in": o.prop.pitch_in,
                            "blades": o.prop.blades, "thrust": o.thrust, "power": o.power,
                            "points": o.points} for o in self.offsets],
        }  # fmt: skip


def family_spec(rules: Mapping[str, Any], family: str) -> Mapping[str, Any]:
    for f in rules["families"]:
        if f["id"] == family:
            return f
    raise KeyError(f"no propeller family {family!r}")


@dataclass(frozen=True)
class _Geometry:
    r: Array  # station radii over the tip's
    dr: Array
    chord: Array  # chord over the tip radius


def _geometry(rules: Mapping[str, Any], family: str) -> _Geometry:
    n = int(rules["stations"])
    edges = np.linspace(float(rules["root_cutout"]), 1.0, n + 1)
    r = (edges[1:] + edges[:-1]) / 2
    shape = np.array(rules["chord_shape"], float)
    chord = float(family_spec(rules, family)["chord_75"]) * np.interp(r, shape[:, 0], shape[:, 1])
    return _Geometry(r, np.diff(edges), chord)


@dataclass(frozen=True)
class _Points:
    """Operating points for the theory, element-wise."""

    diameter: Array  # m
    pitch_ratio: Array
    blades: Array
    rpm: Array
    density: Array


def _theory(params: Params, rules: Mapping[str, Any], g: _Geometry,
            p: _Points) -> tuple[Array, Array]:  # fmt: skip
    """(C_T, C_P) of each point by blade-element momentum theory in hover."""
    slope0, zero_lift, cd0, cd2, re_exp = (float(v) for v in params)
    r, dr = g.r[None, :], g.dr[None, :]
    radius = p.diameter[:, None] / 2
    tip = p.rpm[:, None] * (2 * math.pi / 60) * radius  # m/s
    sigma = p.blades[:, None] * g.chord[None, :] / math.pi
    mach = np.minimum(tip * r / float(rules["sound_speed"]), float(rules["mach_limit"]))
    slope = slope0 / np.sqrt(1 - mach**2)
    theta = np.arctan(p.pitch_ratio[:, None] / (math.pi * r)) - math.radians(zero_lift)
    sa = sigma * slope
    lift = np.maximum(theta, 0.0)
    loss = np.ones_like(sa)
    lam = sa / 16 * (np.sqrt(1 + 32 * lift * r / sa) - 1)
    if rules["tip_loss"]:
        for _ in range(int(rules["tip_loss_rounds"])):
            f = p.blades[:, None] / 2 * (1 - r) / np.maximum(lam, 1e-9)
            loss = np.maximum(2 / math.pi * np.arccos(np.clip(np.exp(-f), 0.0, 1.0)), 1e-3)
            lam = sa / (16 * loss) * (np.sqrt(1 + 32 * loss * lift * r / sa) - 1)
    alpha = theta - lam / r
    reynolds = p.density[:, None] * tip * r * g.chord[None, :] * radius / float(
        rules["air_viscosity"])  # fmt: skip
    cd = cd0 * (reynolds / float(rules["re_reference"])) ** (-re_exp) + cd2 * alpha**2
    dct = 0.5 * sigma * slope * alpha * r**2 * dr
    dcp = lam * dct + 0.5 * sigma * cd * r**3 * dr
    return CT_ROTOR * dct.sum(axis=1), CP_ROTOR * dcp.sum(axis=1)


def _points(props: Sequence[Prop], rpm: Array, density: Array) -> _Points:
    return _Points(np.array([q.diameter_in * INCH for q in props], float),
                   np.array([q.pitch_in / q.diameter_in for q in props], float),
                   np.array([q.blades for q in props], float), np.asarray(rpm, float),
                   np.asarray(density, float))  # fmt: skip


def coefficients(params: Params, rules: Mapping[str, Any], prop: Prop,
                 rpm: Array | float, density: Array | float) -> tuple[Array, Array]:  # fmt: skip
    """(C_T, C_P) of a propeller at these speeds and air, by its family's theory."""
    w, rho = np.broadcast_arrays(np.atleast_1d(np.asarray(rpm, float)),
                                 np.atleast_1d(np.asarray(density, float)))  # fmt: skip
    return _theory(params, rules, _geometry(rules, prop.family), _points([prop] * len(w), w, rho))


def starting(rules: Mapping[str, Any]) -> tuple[Array, Array, Array]:
    """The parameters' starting values, spreads and least values."""
    spec = rules["parameters"]
    return (np.array([float(spec[n]["mean"]) for n in NAMES]),
            np.array([float(spec[n]["spread"]) for n in NAMES]),
            np.array([float(spec[n]["least"]) for n in NAMES]))  # fmt: skip


def uncalibrated(rules: Mapping[str, Any], family: str) -> Family:
    """A family no table has calibrated: its starting values and their spreads."""
    mean, spread, _ = starting(rules)
    cov = np.diag(spread**2)
    return Family(family, tuple(map(float, mean)), (), tuple(map(tuple, cov.tolist())),
                  float(rules["offset_spread"]["thrust"]), float(rules["offset_spread"]["power"]),
                  float(rules["noise_floor"]), None, 0)  # fmt: skip


def calibrate(rules: Mapping[str, Any], family: str,
              tables: Sequence[tuple[Prop, Sequence[TablePoint]]]) -> Family:  # fmt: skip
    """The family's theory fitted to its tables: the parameters, each propeller's offsets
    and the spread of offsets between propellers. Steps without a speed are left out;
    steps without a shaft power give the thrust coefficient only."""
    g = _geometry(rules, family)
    props: dict[str, Prop] = {}
    rows: list[tuple[int, TablePoint]] = []
    used = 0
    for prop, pts in tables:
        if prop.family != family:
            continue
        good = [t for t in pts if t.rpm > 0 and t.ct > 0]
        if not good:
            continue
        used += 1
        i = list(props).index(prop.key) if prop.key in props else len(props)
        props.setdefault(prop.key, prop)
        rows += [(i, t) for t in good]
    if not rows:
        return uncalibrated(rules, family)
    order = list(props.values())
    k = len(order)
    which = np.array([i for i, _ in rows])
    pts = _points([order[int(i)] for i in which], np.array([t.rpm for _, t in rows]),
                  np.array([t.density for _, t in rows]))  # fmt: skip
    log_ct = np.log([t.ct for _, t in rows])
    has_cp = np.array([t.cp is not None and t.cp > 0 for _, t in rows])
    log_cp = np.log([t.cp if t.cp is not None and t.cp > 0 else 1.0 for _, t in rows])
    mean, spread, least = starting(rules)
    m = len(NAMES)
    floor = float(rules["noise_floor"])
    tau0 = np.array(
        [float(rules["offset_spread"]["thrust"]), float(rules["offset_spread"]["power"])]
    )
    strength = float(rules["offset_strength"])
    noise, tau = floor, tau0.copy()

    def model(params: Array) -> tuple[Array, Array]:
        ct, cp = _theory(params, rules, g, pts)
        return np.log(np.maximum(ct, 1e-12)), np.log(np.maximum(cp, 1e-12))

    def unpack(x: Array) -> tuple[Array, Array, Array]:
        u = x[m:].reshape(k, 2)
        return x[:m], u[:, 0], u[:, 1]

    def resid(x: Array, theory: tuple[Array, Array] | None = None) -> Array:
        params, ut, uq = unpack(x)
        mt, mp = theory if theory is not None else model(params)
        rt = (log_ct - mt - ut[which]) / noise
        rp = ((log_cp - mp - 1.5 * ut[which] - uq[which]) / noise)[has_cp]
        return np.concatenate([rt, rp, (params - mean) / spread, ut / tau[0], uq / tau[1]])

    x = np.concatenate([mean, np.zeros(2 * k)])
    cov = np.eye(len(x))
    for _ in range(int(rules["outer_rounds"])):
        x, jac = _least_squares(x, resid, model, which, has_cp, noise, tau, m, k, least,
                                int(rules["iterations"]))  # fmt: skip
        cov = np.linalg.inv(jac.T @ jac)  # every parameter has a prior: positive definite
        params, ut, uq = unpack(x)
        mt, mp = model(params)
        raw = np.concatenate([log_ct - mt - ut[which],
                              (log_cp - mp - 1.5 * ut[which] - uq[which])[has_cp]])  # fmt: skip
        free = max(len(raw) - m - 2 * k, 1)
        was = np.array([noise, *tau])
        noise = max(math.sqrt(float(raw @ raw) / free), floor)
        var_u = np.diag(cov)[m:].reshape(k, 2)
        squares = (np.stack([ut, uq], 1) ** 2 + var_u).sum(axis=0)
        tau = np.sqrt((squares + strength * tau0**2) / (k + strength))
        if np.all(np.abs(np.array([noise, *tau]) / was - 1) < 0.01):
            break
    params, ut, uq = unpack(x)
    mt, mp = model(params)
    raw = np.concatenate(
        [log_ct - mt - ut[which], (log_cp - mp - 1.5 * ut[which] - uq[which])[has_cp]]
    )
    offsets = tuple(Offsets(order[i], float(ut[i]), float(uq[i]), int((which == i).sum()))
                    for i in range(k))  # fmt: skip
    return Family(family, tuple(map(float, params)), offsets, tuple(map(tuple, cov.tolist())),
                  float(tau[0]), float(tau[1]), noise, float(np.sqrt(np.mean(raw**2))),
                  used)  # fmt: skip


def from_tables(rules: Mapping[str, Any], tables: Sequence[Bench]) -> dict[str, Family]:
    """Each family calibrated on the tables marked with it (and with the propeller's
    diameter and pitch, and steps with their speed); a table without a family calibrates
    none."""
    by: dict[str, list[tuple[Prop, Sequence[TablePoint]]]] = {}
    known = {f["id"] for f in rules["families"]}
    for b in tables:
        if b.prop_family not in known or not b.prop_diameter_in or not b.prop_pitch_in:
            continue
        assert b.prop_family is not None
        points, _ = table_points(b)
        if points:
            prop = Prop(b.prop_family, b.prop_diameter_in, b.prop_pitch_in, b.prop_blades or 2)
            by.setdefault(b.prop_family, []).append((prop, points))
    return {f: calibrate(rules, f, found) for f, found in by.items()}


def flown(rules: Mapping[str, Any], answers: Mapping[str, Any]) -> Prop | None:
    """The propeller the setup says the aircraft flies, in the default family when the
    setup names none; None without its diameter and pitch."""

    def number(key: str) -> float | None:
        v = answers.get(key)
        return (
            float(v) if isinstance(v, int | float) and not isinstance(v, bool) and v > 0 else None
        )

    d, p = number("prop_diameter_in"), number("prop_pitch_in")
    if d is None or p is None:
        return None
    family = answers.get("prop_family")
    if family not in {f["id"] for f in rules["families"]}:
        family = rules["default_family"]
    blades = number("prop_blades")
    return Prop(str(family), d, p, int(blades) if blades else 2)


def _least_squares(x: Array, resid: Callable[..., Array],
                   model: Callable[[Array], tuple[Array, Array]], which: Array, has_cp: Array,
                   noise: float, tau: Array, m: int, k: int, least: Array,
                   iterations: int) -> tuple[Array, Array]:  # fmt: skip
    """Levenberg-Marquardt; the parameters' Jacobian by differences, the offsets' exact.
    Returns the solution and the Jacobian there."""

    def jacobian(x: Array, base: tuple[Array, Array]) -> Array:
        r0 = resid(x, base)
        jac = np.zeros((len(r0), len(x)))
        for j in range(m):
            step = max(abs(x[j]) * 1e-6, 1e-8)
            dx = x.copy()
            dx[j] += step
            jac[:, j] = (resid(dx) - r0) / step
        n_t, n_p = len(which), int(has_cp.sum())
        for i in range(k):
            rows_t = np.flatnonzero(which == i)
            rows_p = np.flatnonzero(which[has_cp] == i)
            jac[rows_t, m + 2 * i] = -1 / noise
            jac[n_t + rows_p, m + 2 * i] = -1.5 / noise
            jac[n_t + rows_p, m + 2 * i + 1] = -1 / noise
            jac[n_t + n_p + m + i, m + 2 * i] = 1 / tau[0]
            jac[n_t + n_p + m + k + i, m + 2 * i + 1] = 1 / tau[1]
        return jac

    lower = np.concatenate([least, np.full(2 * k, -np.inf)])
    base = model(x[:m])
    r = resid(x, base)
    cost = float(r @ r)
    jac = jacobian(x, base)
    lam = 1e-3
    for _ in range(iterations):
        a, grad = jac.T @ jac, jac.T @ r
        improved = False
        while lam < 1e10:
            trial = np.maximum(x - np.linalg.solve(a + lam * np.diag(np.diag(a) + 1e-12), grad),
                               lower)  # fmt: skip
            tb = model(trial[:m])
            rt = resid(trial, tb)
            ct = float(rt @ rt)
            if ct < cost:
                done = cost - ct < 1e-12 * max(cost, 1.0)
                x, r, cost, base, lam = trial, rt, ct, tb, max(lam / 3, 1e-9)
                jac = jacobian(x, base)
                improved = not done
                break
            lam *= 4
        if not improved:
            break
    return x, jac


@dataclass(frozen=True)
class Hover:
    """A propeller's predicted hover at a thrust: its speed, shaft power and coefficients,
    each figure's spread a standard deviation of its logarithm."""

    rpm: float
    shaft_w: float | None
    ct: float
    cp: float | None
    rpm_spread: float
    power_spread: float | None
    calibrated: bool  # both families calibrated on tables
    anchored: bool  # changed from an operating point measured on the aircraft
    notes: tuple[str, ...] = field(default=())

    def bounds(self, z: float = 1.96) -> dict[str, tuple[float, float] | None]:
        def band(v: float | None, s: float | None) -> tuple[float, float] | None:
            return None if v is None or s is None else (v * math.exp(-z * s), v * math.exp(z * s))

        return {
            "rpm": band(self.rpm, self.rpm_spread),
            "shaft_w": band(self.shaft_w, self.power_spread),
        }


@dataclass(frozen=True)
class _Change:
    """A candidate changed from the propeller flown: both families' parameters and
    offsets as one vector with its covariance, and where each part sits in it."""

    rules: Mapping[str, Any]
    candidate: Prop
    current: Prop | None
    at: Operating | None
    fam_n: Family
    fam_c: Family
    z0: Array
    cov: Array
    off_n: int
    off_c: int
    i_n: int | None
    i_c: int | None

    @property
    def anchored(self) -> bool:
        return self.at is not None and self.current is not None

    @property
    def same(self) -> bool:
        return self.current is not None and self.anchored and self.current.key == self.candidate.key

    def _theory(self, z: Array, off: int, prop: Prop, rpm: float,
                rho: float) -> tuple[float, float]:  # fmt: skip
        ct, cp = _theory(z[off : off + len(NAMES)], self.rules, _geometry(self.rules, prop.family),
                         _points([prop], np.array([rpm]), np.array([rho])))  # fmt: skip
        return math.log(float(ct[0])), math.log(float(cp[0]))

    def _offsets(self, z: Array, off: int, i: int | None) -> tuple[float, float]:
        return (0.0, 0.0) if i is None else (float(z[off + i]), float(z[off + i + 1]))

    def coefficients(self, z: Array, rpm: float, rho: float) -> tuple[float, float]:
        """The candidate's (log C_T, log C_P) at a speed and air: its family's theory, its
        own offsets, and what the anchor measured over what the theory and the current
        propeller's offsets give for it."""
        ut_n, uq_n = self._offsets(z, self.off_n, self.i_n)
        dt = dp = 0.0
        if self.anchored and self.at is not None and self.current is not None:
            ut_c, uq_c = self._offsets(z, self.off_c, self.i_c)
            mt, mp = self._theory(z, self.off_c, self.current, self.at.rpm, self.at.density)
            dt = math.log(self.at.ct) - mt - ut_c
            dp = (math.log(self.at.cp) - mp - 1.5 * ut_c - uq_c) if self.at.cp else 0.0
        mt, mp = self._theory(z, self.off_n, self.candidate, rpm, rho)
        return mt + ut_n + dt, mp + 1.5 * ut_n + uq_n + dp

    def used(self) -> list[int]:
        """The vector's entries the candidate's coefficients depend on."""
        m = len(NAMES)
        out = list(range(self.off_n, self.off_n + m))
        if self.anchored and self.fam_c is not self.fam_n:
            out += list(range(self.off_c, self.off_c + m))
        for off, i in ((self.off_n, self.i_n), (self.off_c, self.i_c)):
            if i is not None:
                out += [off + i, off + i + 1]
        return sorted(set(out))


def _change(rules: Mapping[str, Any], families: Mapping[str, Family], candidate: Prop,
            current: Prop | None, at: Operating | None) -> _Change:  # fmt: skip
    fam_n = families.get(candidate.family) or uncalibrated(rules, candidate.family)
    blocks: list[Family] = [fam_n]
    fam_c = fam_n
    if at is not None and current is not None:
        fam_c = families.get(current.family) or uncalibrated(rules, current.family)
        if fam_c.id != fam_n.id:
            blocks.append(fam_c)
    starts = np.cumsum([0] + [len(b.vector()) for b in blocks])
    z0 = np.concatenate([b.vector() for b in blocks])
    cov = np.zeros((len(z0), len(z0)))
    for b, s in zip(blocks, starts, strict=False):
        n = len(b.vector())
        cov[s : s + n, s : s + n] = np.array(b.covariance)
    return _Change(rules, candidate, current, at, fam_n, fam_c, z0, cov, int(starts[0]),
                   int(starts[blocks.index(fam_c)]), fam_n.index(candidate),
                   fam_c.index(current) if current is not None else None)  # fmt: skip


Coefficients = Callable[[float, float], tuple[float, float]]  # (rpm, density) -> (C_T, C_P)


def anchored(rules: Mapping[str, Any], families: Mapping[str, Family], candidate: Prop,
             current: Prop | None = None, at: Operating | None = None) -> Coefficients:  # fmt: skip
    """The candidate's (C_T, C_P) at any speed (rpm) and air, as `predict` reads them:
    for matching the propeller to its motor away from hover."""
    c = _change(rules, families, candidate, current, at)

    def at_speed(rpm: float, rho: float) -> tuple[float, float]:
        lt, lp = c.coefficients(c.z0, rpm, rho)
        return math.exp(lt), math.exp(lp)

    return at_speed


def predict(rules: Mapping[str, Any], families: Mapping[str, Family], candidate: Prop,
            thrust_n: float, density: float, current: Prop | None = None,
            at: Operating | None = None) -> Hover:  # fmt: skip
    """The candidate's hover at this thrust and air: changed from the current propeller's
    measured operating point `at` when there is one, from its family's theory alone
    otherwise."""
    c = _change(rules, families, candidate, current, at)
    fam_n, fam_c, z0, cov, i_n, i_c = c.fam_n, c.fam_c, c.z0, c.cov, c.i_n, c.i_c
    anchored, same = c.anchored, c.same
    d = candidate.diameter_in * INCH

    def solve(z: Array) -> tuple[float, float, float, float]:
        """(log rpm, log shaft power, C_T, C_P) of the candidate."""
        rpm = at.rpm if same and at is not None else 3000.0
        ct = cp = 0.0
        for _ in range(int(rules["hover_rounds"])):
            lt, lp = c.coefficients(z, rpm, density)
            ct, cp = math.exp(lt), math.exp(lp)
            rpm = 60 * math.sqrt(thrust_n / (ct * density * d**4))
        n = rpm / 60
        return math.log(rpm), math.log(cp * density * n**3 * d**5), ct, cp

    log_rpm, log_p, ct, cp = solve(z0)
    grad = np.zeros((2, len(z0)))
    for j in c.used():
        step = max(abs(z0[j]) * 1e-5, 1e-6)
        hi, lo = z0.copy(), z0.copy()
        hi[j] += step
        lo[j] -= step
        a, b = solve(hi), solve(lo)
        grad[:, j] = [(a[0] - b[0]) / (2 * step), (a[1] - b[1]) / (2 * step)]
    var = np.einsum("ij,jk,ik->i", grad, cov, grad)
    notes: list[str] = []
    if not same:
        # offsets of propellers no table measured: the candidate's, and the current one's,
        # which the anchor holds; and the aircraft's losses differing between the two
        for fam, i, who in ((fam_n, i_n, "candidate"), (fam_c, i_c, "current")):
            if who == "current" and not anchored:
                continue
            if i is None:
                var += [0.25 * fam.spread_thrust**2, fam.spread_power**2]
                notes.append(f"{who}_not_measured")
        spread = rules["installation_spread"] if anchored else rules["unanchored_spread"]
        var += [0.25 * float(spread["thrust"]) ** 2, float(spread["power"]) ** 2]
    if not anchored:
        notes.append("not_anchored")
    if not (fam_n.calibrated and fam_c.calibrated):
        notes.append("uncalibrated")
    power_known = not anchored or (at is not None and at.cp is not None)
    if not power_known:
        notes.append("no_shaft_power")
    return Hover(rpm=math.exp(log_rpm), shaft_w=math.exp(log_p) if power_known else None,
                 ct=ct, cp=cp if power_known else None, rpm_spread=math.sqrt(max(var[0], 0.0)),
                 power_spread=math.sqrt(max(var[1], 0.0)) if power_known else None,
                 calibrated=fam_n.calibrated and fam_c.calibrated, anchored=anchored,
                 notes=tuple(notes))  # fmt: skip
