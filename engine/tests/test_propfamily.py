"""Propeller families (performance/propfamily.py), against a made-up family whose theory
parameters are known: the theory against its closed form and physical limits, the
calibration giving the family back from its tables, the ratio prediction exact when the
theory is true, and the intervals covering held-out propellers at about their rate."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import numpy as np
import pytest

from flight_insights.analyzers import Registry
from flight_insights.knowledge import load_pack
from flight_insights.performance.propeller import INCH, TablePoint
from flight_insights.performance.propfamily import (
    CP_ROTOR,
    CT_ROTOR,
    Family,
    Operating,
    Prop,
    calibrate,
    coefficients,
    predict,
    starting,
    uncalibrated,
)

RULES: dict[str, Any] = dict(Registry.load(load_pack()).rules["props"])
TRUE = np.array([5.0, -4.5, 0.012, 0.9, 0.35])  # a made-up family's theory
FAM = "tmotor_p"
RPMS = np.linspace(2000.0, 4500.0, 8)


def table(prop: Prop, ut: float = 0.0, uq: float = 0.0, noise: float = 0.0, rho: float = 1.18,
          seed: int = 1, torque: bool = True) -> list[TablePoint]:  # fmt: skip
    """A maker's table of a propeller whose coefficients sit `ut`, `uq` from the theory."""
    rng = np.random.default_rng(seed)
    ct, cp = coefficients(TRUE, RULES, prop, RPMS, rho)
    ct = ct * np.exp(ut + rng.normal(0, noise, len(RPMS)))
    cp = cp * np.exp(1.5 * ut + uq + rng.normal(0, noise, len(RPMS)))
    d = prop.diameter_in * INCH
    return [TablePoint(float(w), float(t * rho * (w / 60) ** 2 * d**4), float(t),
                       float(p) if torque else None, float(p), rho)
            for w, t, p in zip(RPMS, ct, cp, strict=True)]  # fmt: skip


def true_hover(prop: Prop, thrust: float, rho: float, kt: float = 1.0,
               kp: float = 1.0) -> tuple[float, float]:  # fmt: skip
    """(rpm, shaft W) of the true family at this thrust, its coefficients times kt, kp."""
    d, rpm = prop.diameter_in * INCH, 3000.0
    for _ in range(40):
        ct, _ = coefficients(TRUE, RULES, prop, rpm, rho)
        rpm = 60 * math.sqrt(thrust / (kt * float(ct[0]) * rho * d**4))
    cp = kp * float(coefficients(TRUE, RULES, prop, rpm, rho)[1][0])
    return rpm, cp * rho * (rpm / 60) ** 3 * d**5


def test_the_theory_against_its_closed_form() -> None:
    # constant chord, no tip loss, no compressibility, a small pitch: the helical twist is
    # ideal, the inflow uniform, C_T = (sigma a / 4)(theta_tip - lambda)(1 - r0^2) and
    # without profile drag C_P = lambda C_T (Leishman 3.3)
    rules = RULES | {"tip_loss": False, "sound_speed": 1e12,
                     "chord_shape": [[0.0, 1.0], [1.0, 1.0]]}  # fmt: skip
    params = [5.7, 0.0, 0.0, 0.0, 0.0]
    prop = Prop(FAM, 20.0, 0.4)
    ct, cp = coefficients(params, rules, prop, 3000.0, 1.2)
    sigma = 2 * 0.13 / math.pi
    theta = 0.4 / 20.0 / math.pi
    lam = sigma * 5.7 / 16 * (math.sqrt(1 + 32 * theta / (sigma * 5.7)) - 1)
    r0 = RULES["root_cutout"]
    want = sigma * 5.7 / 4 * (theta - lam) * (1 - r0**2)
    assert float(ct[0]) / CT_ROTOR == pytest.approx(want, rel=2e-3)
    assert float(cp[0]) / CP_ROTOR == pytest.approx(lam * want, rel=2e-3)


def test_the_theory_behaves_as_propellers_do() -> None:
    def fm(params: Any, prop: Prop, rpm: float = 3000.0) -> float:
        ct, cp = coefficients(params, RULES, prop, rpm, 1.2)
        return float((ct[0] / CT_ROTOR) ** 1.5 / math.sqrt(2) / (cp[0] / CP_ROTOR))

    p22 = Prop(FAM, 22.0, 6.6)
    # without profile drag only the induced losses remain: below but near the ideal
    assert 0.85 < fm([5.7, -3.0, 0.0, 0.0, 0.3], p22) < 1.0
    assert 0.55 < fm(TRUE, p22) < 0.85
    lo, hi = (coefficients(TRUE, RULES, Prop(FAM, 22.0, p), 3000.0, 1.2) for p in (6.0, 8.0))
    assert hi[0][0] > lo[0][0] and hi[1][0] > lo[1][0]  # more pitch: more thrust and power
    slow, fast = (coefficients(TRUE, RULES, p22, w, 1.2) for w in (2000.0, 5000.0))
    assert fast[1][0] < slow[1][0]  # faster, a higher Reynolds number: less profile drag
    assert fm(TRUE, p22, 5000.0) > fm(TRUE, p22, 2000.0)


def test_calibration_gives_the_tables_back() -> None:
    props = [Prop(FAM, d, p) for d, p in ((20.0, 6.0), (22.0, 6.6), (22.0, 7.4), (24.0, 8.0))]
    offsets = [(0.03, -0.02), (-0.02, 0.01), (0.01, 0.02), (-0.02, -0.01)]
    tables = [(q, table(q, *u, noise=0.003, seed=i))
              for i, (q, u) in enumerate(zip(props, offsets, strict=True))]  # fmt: skip
    fam = calibrate(RULES, FAM, tables)
    assert fam.calibrated and fam.tables == 4 and len(fam.offsets) == 4
    assert fam.rms is not None and fam.rms < 0.01
    assert fam.noise == pytest.approx(0.005, abs=0.002)  # at the floor: the tables are clean
    n = len(fam.vector())
    assert np.array(fam.covariance).shape == (n, n)
    # each table read back through the family and its own offsets
    for q, pts in tables:
        i = fam.index(q)
        assert i is not None
        ut, uq = fam.vector()[i], fam.vector()[i + 1]
        ct, cp = coefficients(fam.params, RULES, q, np.array([t.rpm for t in pts]), 1.18)
        assert np.exp(ut) * ct == pytest.approx([t.ct for t in pts], rel=0.01)
        assert np.exp(1.5 * ut + uq) * cp == pytest.approx([t.cp for t in pts], rel=0.01)
    # the tables of another family, and steps without a speed, are left out
    other = calibrate(RULES, "tmotor_g", tables)
    assert not other.calibrated and other.params == tuple(starting(RULES)[0])


def test_the_ratio_is_exact_when_the_theory_is_true() -> None:
    fam = replace(uncalibrated(RULES, FAM), params=tuple(map(float, TRUE)), tables=1)
    current, candidate = Prop(FAM, 22.0, 6.6), Prop(FAM, 24.0, 8.0)
    # on the aircraft the arms and frame lose 6% of the thrust and cost 5% more power
    kt, kp = 0.94, 1.05
    w_now, _ = true_hover(current, 40.0, 1.1, kt, kp)
    ct, cp = coefficients(TRUE, RULES, current, w_now, 1.1)
    at = Operating(w_now, kt * float(ct[0]), kp * float(cp[0]), 1.1)
    got = predict(RULES, {FAM: fam}, candidate, 45.0, 1.15, current, at)
    rpm, shaft = true_hover(candidate, 45.0, 1.15, kt, kp)
    assert got.rpm == pytest.approx(rpm, rel=1e-6) and got.shaft_w == pytest.approx(shaft, rel=1e-6)
    assert got.anchored and got.calibrated
    assert set(got.notes) == {"candidate_not_measured", "current_not_measured"}
    # the interval holds both propellers' unknown offsets and the aircraft's losses
    floor = math.sqrt(2 * fam.spread_power**2 + RULES["installation_spread"]["power"] ** 2)
    assert got.power_spread is not None and got.power_spread >= floor
    lo, hi = got.bounds()["shaft_w"] or (0.0, 0.0)
    assert lo < shaft < hi


def test_the_same_propeller_at_another_weight() -> None:
    prop, other = Prop(FAM, 22.0, 6.6), Prop(FAM, 24.0, 8.0)
    fam = calibrate(RULES, FAM, [(prop, table(prop, noise=0.003)), (other, table(other, seed=2))])
    w, _ = true_hover(prop, 40.0, 1.1, 0.95, 1.04)
    ct, cp = coefficients(TRUE, RULES, prop, w, 1.1)
    at = Operating(w, 0.95 * float(ct[0]), 1.04 * float(cp[0]), 1.1)
    got = predict(RULES, {FAM: fam}, prop, 55.0, 1.1, prop, at)
    rpm, shaft = true_hover(prop, 55.0, 1.1, 0.95, 1.04)
    # only the theory's change with speed is predicted: the offsets cancel exactly
    assert got.notes == () and got.power_spread is not None and got.power_spread < 0.01
    assert got.rpm == pytest.approx(rpm, rel=0.003) and got.shaft_w == pytest.approx(
        shaft, rel=0.005
    )


def test_what_widens_the_interval() -> None:
    current, candidate = Prop(FAM, 22.0, 6.6), Prop(FAM, 22.0, 7.4)
    tables = [(q, table(q, noise=0.003, seed=i)) for i, q in
              enumerate((current, candidate, Prop(FAM, 24.0, 8.0)))]  # fmt: skip
    fam = calibrate(RULES, FAM, tables)
    ct, cp = coefficients(TRUE, RULES, current, 3300.0, 1.2)
    at = Operating(3300.0, float(ct[0]), float(cp[0]), 1.2)

    def spread(families: dict[str, Family], **kw: Any) -> float:
        got = predict(RULES, families, candidate, 45.0, 1.2, **kw)
        assert got.power_spread is not None
        return got.power_spread

    measured = spread({FAM: fam}, current=current, at=at)  # both propellers on tables
    one = calibrate(RULES, FAM, tables[2:])  # neither: the family's spread of offsets
    blind = spread({FAM: one}, current=current, at=at)
    theory = spread({}, current=current, at=at)  # no table of the family at all
    alone = spread({FAM: fam})  # nothing measured on the aircraft
    assert measured < blind < theory and measured < alone
    got = predict(RULES, {}, candidate, 45.0, 1.2, current, at)
    assert not got.calibrated and "uncalibrated" in got.notes
    assert "not_anchored" in predict(RULES, {FAM: fam}, candidate, 45.0, 1.2).notes
    # without the shaft power on the aircraft only the speed is predicted
    speed_only = predict(RULES, {FAM: fam}, candidate, 45.0, 1.2, current, replace(at, cp=None))
    assert speed_only.shaft_w is None and "no_shaft_power" in speed_only.notes


def test_steps_without_torque_still_give_the_thrust() -> None:
    q = Prop(FAM, 22.0, 6.6)
    fam = calibrate(RULES, FAM, [(q, table(q, ut=0.05, torque=False))])
    assert fam.calibrated and fam.offsets[0].points == len(RPMS)
    i = fam.index(q)
    assert i is not None
    ct, _ = coefficients(fam.params, RULES, q, RPMS, 1.18)
    assert np.exp(fam.vector()[i]) * ct == pytest.approx([t.ct for t in table(q, ut=0.05)],
                                                         rel=0.01)  # fmt: skip


def test_intervals_cover_held_out_propellers() -> None:
    rng = np.random.default_rng(20261030)
    shapes = [(18.0, 5.5), (20.0, 6.0), (22.0, 6.6), (22.0, 7.4), (24.0, 8.0), (26.0, 8.5)]
    tau = RULES["offset_spread"]["power"]
    hits, n = 0, 25
    for rep in range(n):
        u = rng.normal(0, tau, (len(shapes), 2))
        props = [Prop(FAM, d, p) for d, p in shapes]
        held = int(rng.integers(len(shapes)))
        tables = [(q, table(q, *u[i], noise=0.004, seed=rep * 10 + i))
                  for i, q in enumerate(props) if i != held]  # fmt: skip
        fam = calibrate(RULES, FAM, tables)
        cur = (held + 1) % len(shapes)
        w = 3200.0
        ct, cp = coefficients(TRUE, RULES, props[cur], w, 1.2)
        at = Operating(w, float(ct[0]) * math.exp(u[cur, 0]),
                       float(cp[0]) * math.exp(1.5 * u[cur, 0] + u[cur, 1]), 1.2)  # fmt: skip
        got = predict(RULES, {FAM: fam}, props[held], 40.0, 1.2, props[cur], at)
        _, shaft = true_hover(props[held], 40.0, 1.2, math.exp(u[held, 0]),
                              math.exp(1.5 * u[held, 0] + u[held, 1]))  # fmt: skip
        assert got.shaft_w is not None and got.power_spread is not None
        hits += abs(math.log(got.shaft_w / shaft)) <= 1.96 * got.power_spread
    assert hits / n >= 0.85
