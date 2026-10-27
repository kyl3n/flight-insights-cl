"""Losing a motor: the linear program solver against vertex enumeration, and the margin of
each frame class against what the firmware's geometry allows."""

from __future__ import annotations

import itertools
from typing import Any

import numpy as np
import pytest

from flight_insights.knowledge import load_pack
from flight_insights.motor_out import arms, mixer, output_for, solve, thrust_at, worst
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
    cor = worst(motors(4, 20), weight, hold_yaw=False, best=True)
    plain = worst(x8, weight, hold_yaw=False, best=True)
    assert cor[0].peak == pytest.approx(plain[0].peak, rel=1e-6)


# --- what ArduPilot's mixer does -------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "kind", "mixed", "best"),
    [
        (4, 1, 1.5, 4 / 3),  # X8: the mixer leans on the partner more than it must
        (3, 1, 1.5, 2**0.5),  # octa X
        (12, 1, 1.375, 1.2),  # dodeca-hexa
        (14, 1, 1.4361, 1.25),  # deca
        (2, 1, 1.5, 1.5),  # hexa X: no other way to balance
        (5, 10, 2.0, 2.0),  # Y6B
    ],
)
def test_the_mixer_against_the_best_split(cls: int, kind: int, mixed: float, best: float) -> None:
    """Each motor lost, as ArduPilot's mixer flies on and at the best split, in times its
    hover thrust: the mixer can only reach the thrusts its four demands span."""
    frame = motors(cls, kind)
    n = len(frame)
    fw = worst(frame, float(n))[0].peak
    lp = worst(frame, float(n), best=True)[0].peak
    assert fw is not None and lp is not None
    assert fw == pytest.approx(mixed, abs=1e-3) and lp == pytest.approx(best, abs=1e-3)
    assert fw >= lp - 1e-9  # no mixer beats the best split


def test_the_mixers_balance_lies_in_what_its_demands_reach() -> None:
    x8 = motors(4, 1)
    lost = mixer(x8, 1, 8.0)
    thrusts = np.array(lost.thrusts)
    assert thrusts[0] == 0.0 and thrusts.sum() == pytest.approx(8.0)
    assert np.allclose(arms(x8).T @ thrusts, 0.0, atol=1e-9)  # no roll, pitch or yaw left
    factors = np.array([[m["throttle"], m["roll"], m["pitch"], m["yaw"]] for m in x8])[1:]
    demands, *_ = np.linalg.lstsq(factors, thrusts[1:], rcond=None)
    assert np.allclose(factors @ demands, thrusts[1:], atol=1e-9)
    assert lost.peak == pytest.approx(1.5) and thrusts[5] == pytest.approx(1.5)  # its partner


def test_the_co_rotating_x8_is_as_good_as_it_can_be() -> None:
    """A co-rotating X8 can hold its heading only by stopping the lost motor's partner's
    yaw: its mixer and the best split both leave 2 hover thrusts on the busiest motor,
    the top layer's 0.9 scaling (control, not geometry) notwithstanding."""
    cor = motors(4, 20)
    assert worst(cor, 8.0)[0].peak == pytest.approx(2.0, abs=1e-6)
    assert worst(cor, 8.0, best=True)[0].peak == pytest.approx(2.0, abs=1e-6)


def test_a_quad_has_no_balance_on_the_mixer_either() -> None:
    assert all(m.peak is None for m in worst(motors(1, 1), 4.0))


def test_every_frame_that_can_lose_a_motor_balances_on_its_mixer() -> None:
    """In 4.7.0, every Copter frame of six or more motors settles with every motor at zero
    thrust or above, whichever is lost: a later release that breaks this shows here."""
    for cls, entry in FRAMES.items():
        for kind, frame in entry["types"].items():
            ms = frame["motors"]
            if len(ms) < 6 or "plane" in frame.get("vehicles", ["copter"]):
                continue
            for m in worst(ms, float(len(ms))):
                assert m.peak is not None, (cls, kind, m.failed)
                assert min(m.thrusts) >= 0.0


def test_the_peak_scales_with_the_weight() -> None:
    octa = motors(3, 1)
    a, b = worst(octa, 3.0)[0].peak, worst(octa, 4.5)[0].peak
    assert a is not None and b is not None and b == pytest.approx(1.5 * a)


@pytest.mark.parametrize("expo", [0.0, 0.3, 0.65, 1.0])
def test_the_output_inverts_the_thrust_curve(expo: float) -> None:
    for x in (0.1, 0.5, 0.9):
        assert output_for(thrust_at(x, expo), expo) == pytest.approx(x)
