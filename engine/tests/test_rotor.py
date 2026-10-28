"""Momentum theory against its closed forms: hover, axial climb, level flight with no
parasite drag, the high-speed limit, the power identity of the force balance, the
density law, and the descent states where it fails."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from flight_insights.knowledge import load_pack
from flight_insights.performance.rotor import (
    flow,
    force_balance,
    induced_velocity,
    profile_power,
    profile_power_rpm,
)

RULES: dict[str, Any] = dict(load_pack().json("rules/performance.json"))
G = 9.80665
W = 30 * G  # a 30 kg heavy-lift
A = 4 * math.pi * (22 * 0.0254 / 2) ** 2  # four 22-inch discs (an X8's coaxial pairs)
RHO = 1.225
VH = math.sqrt(W / (2 * RHO * A))


def test_hover_is_the_hover_induced_velocity() -> None:
    f = flow(W, 0.1, RHO, A, 0.0, 0.0, RULES)
    assert f.thrust == pytest.approx(W) and f.tilt == pytest.approx(0.0)
    assert f.induced == pytest.approx(VH, rel=1e-12)
    assert f.ideal_power == pytest.approx(W**1.5 / math.sqrt(2 * RHO * A), rel=1e-12)
    assert bool(f.valid)


@pytest.mark.parametrize("climb", [0.5, 1.0, 2.0, 3.0, 6.0])
def test_axial_climb_is_the_closed_form(climb: float) -> None:
    f = flow(W, 0.0, RHO, A, 0.0, climb, RULES)
    expected = W * (climb / 2 + math.sqrt(climb**2 / 4 + VH**2))
    assert f.ideal_power == pytest.approx(expected, rel=1e-10)
    # small climbs cost about half the climb power over hover: the inflow helps
    if climb <= 1.0:
        hover = W * VH
        assert f.ideal_power - hover < 0.6 * W * climb


@pytest.mark.parametrize("speed", [1.0, 3.0, 6.0, 10.0, 15.0, 25.0])
def test_level_flight_without_parasite_drag_is_the_closed_form(speed: float) -> None:
    """With no tilt the inflow equation is a quadratic in v_i^2."""
    f = flow(W, 0.0, RHO, A, speed, 0.0, RULES)
    assert f.tilt == pytest.approx(0.0)
    expected = math.sqrt((-(speed**2) + math.sqrt(speed**4 + 4 * VH**4)) / 2)
    assert f.induced == pytest.approx(expected, rel=1e-10)


def test_at_high_speed_the_induced_velocity_falls_as_one_over_speed() -> None:
    speed = 60.0
    f = flow(W, 0.0, RHO, A, speed, 0.0, RULES)
    assert f.induced == pytest.approx(VH**2 / speed, rel=0.01)


@settings(max_examples=300, deadline=None)
@given(
    mass=st.floats(2.0, 60.0),
    drag_area=st.floats(0.0, 0.5),
    rho=st.floats(0.8, 1.35),
    speed=st.floats(0.0, 25.0),
    climb=st.floats(-1.0, 5.0),
)
def test_thrust_times_the_flow_is_the_climb_and_parasite_power(
    mass: float, drag_area: float, rho: float, speed: float, climb: float
) -> None:
    """T u_n = D U + W V_z exactly, so the ideal power needs no climb or drag term added."""
    weight = mass * G
    f = flow(weight, drag_area, rho, A, speed, climb, RULES)
    path = math.hypot(speed, climb)
    drag = 0.5 * rho * drag_area * path**2
    assert float(f.flight_power) == pytest.approx(drag * path + weight * climb, rel=1e-9, abs=1e-6)


@settings(max_examples=300, deadline=None)
@given(
    vh=st.floats(1.0, 30.0),
    normal=st.floats(-5.0, 20.0),
    edgewise=st.floats(0.0, 40.0),
)
def test_the_induced_velocity_solves_glauerts_equation(
    vh: float, normal: float, edgewise: float
) -> None:
    """Wherever a root with air going down through the disc exists."""
    assume(normal >= 0 or -normal * edgewise < 0.999 * vh**2)
    v = float(induced_velocity(vh, normal, edgewise))
    assert normal + v > 0
    assert v * math.hypot(edgewise, normal + v) == pytest.approx(vh**2, rel=1e-9)


def test_hover_power_falls_with_the_square_root_of_density() -> None:
    sea = flow(W, 0.0, 1.225, A, 0.0, 0.0, RULES).ideal_power
    high = flow(W, 0.0, 1.0, A, 0.0, 0.0, RULES).ideal_power
    assert high / sea == pytest.approx(math.sqrt(1.225 / 1.0), rel=1e-12)


def test_parasite_drag_tilts_the_thrust() -> None:
    """At 12 m/s with 0.3 m^2 of flat plate, the drag is 26.5 N: the thrust leans by
    atan(D / W) and grows to the hypotenuse."""
    thrust, tilt = force_balance(W, 0.3, RHO, 12.0, 0.0)
    drag = 0.5 * RHO * 0.3 * 144
    assert float(thrust) == pytest.approx(math.hypot(W, drag))
    assert float(tilt) == pytest.approx(math.atan2(drag, W))


def test_power_falls_then_rises_with_speed() -> None:
    """The bucket: translational lift first, parasite drag later."""
    speeds = np.linspace(0, 25, 251)
    f = flow(W, 0.3, RHO, A, speeds, 0.0, RULES)
    power = f.ideal_power + profile_power(f, 0.2, 0.03)
    least = int(np.argmin(power))
    assert 0 < speeds[least] < 25
    assert power[0] > power[least] < power[-1]


def test_descending_into_the_wake_is_marked() -> None:
    hover_descent = flow(W, 0.1, RHO, A, 0.0, -0.5 * VH, RULES)
    fast_forward_descent = flow(W, 0.1, RHO, A, 2 * VH, -0.2 * VH, RULES)
    slow_descent = flow(W, 0.1, RHO, A, 0.0, -0.1 * VH, RULES)
    steep_fast_descent = flow(W, 0.1, RHO, A, 2 * VH, -VH, RULES)  # the windmill state
    assert not bool(hover_descent.valid) and not bool(steep_fast_descent.valid)
    assert bool(fast_forward_descent.valid) and bool(slow_descent.valid)
    for f in (hover_descent, steep_fast_descent):
        assert np.isfinite(f.induced)


def test_profile_power_follows_the_thrust_and_the_edgewise_speed() -> None:
    f = flow(W, 0.0, RHO, A, np.array([0.0, 10.0]), 0.0, RULES)
    p = profile_power(f, 0.25, 0.03)
    assert p[0] == pytest.approx(0.25 * W * VH)
    assert p[1] == pytest.approx(0.25 * W * VH * (1 + 0.03 * (10.0 / VH) ** 2))


def test_profile_power_from_the_propellers_speed() -> None:
    radius = 22 * 0.0254 / 2
    area = math.pi * radius**2
    p = profile_power_rpm(RHO, area, radius, np.array([3000.0]), np.array([0.0]), 0.004, 4.65)
    tip = 3000 * 2 * math.pi / 60 * radius
    assert float(p[0]) == pytest.approx(0.004 * RHO * area * tip**3)
    q = profile_power_rpm(RHO, area, radius, np.array([3000.0]), np.array([10.0]), 0.004, 4.65)
    assert float(q[0]) == pytest.approx(float(p[0]) * (1 + 4.65 * (10 / tip) ** 2))
