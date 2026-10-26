"""Losing a motor: the linear program solver against vertex enumeration, and the margin of
each frame class against what the firmware's geometry allows."""

from __future__ import annotations

import itertools
from typing import Any

import numpy as np
import pytest

from flight_insights.knowledge import load_pack
from flight_insights.motor_out import output_for, solve, thrust_at, worst
from flight_insights.simplex import linprog

Array = np.ndarray[Any, Any]
FRAMES = load_pack().frames("4.7.0")


def motors(cls: int, kind: int) -> list[dict[str, Any]]:
    return FRAMES[str(cls)]["types"][str(kind)]["motors"]


def by_vertices(c: Array, a: Array, b: Array) -> float | None:
    """The minimum over every basic feasible solution: slow, and certainly right."""
    m, n = int(a.shape[0]), int(a.shape[1])
    best: float | None = None
    for cols in itertools.combinations(range(n), m):
        sub = a[:, cols]
        if abs(np.linalg.det(sub)) < 1e-9:
            continue
        x = np.zeros(n)
        x[list(cols)] = np.linalg.solve(sub, b)
        if (x >= -1e-9).all():
            value = float(c @ x)
            best = value if best is None else min(best, value)
    return best


@pytest.mark.parametrize("seed", range(40))
def test_the_simplex_finds_the_vertices_minimum(seed: int) -> None:
    rng = np.random.default_rng(seed)
    m, n = int(rng.integers(1, 4)), int(rng.integers(4, 8))
    a = rng.normal(size=(m, n))
    x0 = rng.uniform(0, 2, size=n) * (rng.uniform(size=n) > 0.3)  # a feasible point
    b = a @ x0
    c = rng.uniform(0.1, 2, size=n)  # positive costs: bounded below by zero
    x = linprog(c, a, b)
    expected = by_vertices(c, a, b)
    assert x is not None and expected is not None
    assert float(c @ x) == pytest.approx(expected, abs=1e-7)
    assert np.allclose(a @ x, b, atol=1e-7) and (x >= -1e-9).all()


def test_the_simplex_says_when_nothing_fits() -> None:
    assert linprog(np.ones(2), np.array([[1.0, 1.0]]), np.array([-1.0])) is None
    # redundant rows are fine
    x = linprog(np.array([1.0, 2.0]), np.array([[1.0, 1.0], [2.0, 2.0]]), np.array([1.0, 2.0]))
    assert x is not None and x.tolist() == pytest.approx([1.0, 0.0])


def test_a_quad_cannot_hold_its_heading_on_three_motors() -> None:
    quad = motors(1, 1)
    for m in worst(quad, weight=2.0):
        assert m.peak is None
    # without the heading, the diagonal pair carries it all, spinning
    loose = solve(quad, 1, weight=2.0, hold_yaw=False)
    assert loose.peak == pytest.approx(1.0)
    assert loose.thrusts[2] == pytest.approx(1.0) and loose.thrusts[3] == pytest.approx(1.0)


def test_a_hexa_x_stops_the_opposite_motor_and_carries_on_four() -> None:
    """Losing one motor of a hexa X, the one across from it must stop too to balance roll,
    and the four left carry the load: 1.5 times their hover thrust. Two of them spin each
    way, so the heading balances too, but nothing is left to turn it with: the spec's
    target for a hexa does not ask for yaw."""
    hexa = motors(2, 1)
    weight = 6 * 0.5  # hovering at half thrust on all six
    for hold_yaw in (False, True):
        out = worst(hexa, weight, hold_yaw)
        assert all(m.peak == pytest.approx(0.75) for m in out)
    lost = solve(hexa, 1, weight, hold_yaw=False)
    assert lost.thrusts[:2] == (0.0, 0.0) and lost.thrusts[2:] == pytest.approx([0.75] * 4)


def test_an_octa_holds_its_heading_on_seven() -> None:
    octa = motors(3, 1)
    weight = 8 * 0.5
    out = worst(octa, weight, hold_yaw=True)
    assert all(m.peak is not None for m in out)
    peak = out[0].peak
    assert peak is not None and weight / 7 < peak < 1.0  # more than an even share, less than full
    m = out[0]
    arms_balance = np.array([[x["roll"], x["pitch"], x["yaw"]] for x in octa]).T @ np.array(
        m.thrusts
    )
    assert np.allclose(arms_balance, 0, atol=1e-6) and sum(m.thrusts) == pytest.approx(weight)


def test_a_coaxial_x8_holds_its_heading_with_the_partner_working_harder() -> None:
    x8 = motors(4, 1)
    weight = 8 * 0.5
    out = worst(x8, weight, hold_yaw=True)
    assert all(m.peak is not None for m in out)
    # the co-rotating X8 (top layer scaled 0.9 by the mixer) has the same geometry
    cor = worst(motors(4, 20), weight, hold_yaw=False)
    plain = worst(x8, weight, hold_yaw=False)
    assert cor[0].peak == pytest.approx(plain[0].peak, rel=1e-6)


def test_the_peak_scales_with_the_weight() -> None:
    octa = motors(3, 1)
    a, b = worst(octa, 3.0)[0].peak, worst(octa, 4.5)[0].peak
    assert a is not None and b is not None and b == pytest.approx(1.5 * a)


@pytest.mark.parametrize("expo", [0.0, 0.3, 0.65, 1.0])
def test_the_output_inverts_the_thrust_curve(expo: float) -> None:
    for x in (0.1, 0.5, 0.9):
        assert output_for(thrust_at(x, expo), expo) == pytest.approx(x)
