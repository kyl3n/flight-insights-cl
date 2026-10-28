"""The speeds to fly and the radius, against closed forms. A model of avionics plus parasite
drag alone (`P = a + rho f V^3 / 2` in level flight, momentum theory's identity) has its
best range at `V^3 = a / (rho f)`, its best airspeed into a headwind w where
`2 c V^3 - 3 c w V^2 - a = 0`, and out and back in calm air reaches `E V / 2P`. The full
model's bucket gives its speeds in order; a wind costs radius, the same whichever way it
blows along the track, and a drop flies home lighter."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from flight_insights.analyzers import Registry
from flight_insights.knowledge import load_pack
from flight_insights.performance.fit import G, Model
from flight_insights.performance.optimize import (
    airspeed_for,
    best,
    ground_for,
    leg,
    one_setting,
    with_resistance,
)
from flight_insights.performance.radius import radius, standard, worst

RULES: dict[str, Any] = dict(Registry.load(load_pack()).rules["performance"])
DISC = 4 * math.pi * (22 * 0.0254 / 2) ** 2
RHO = 1.2
A_W = 800.0  # avionics, W
F = 0.5  # flat plate, m^2
DRAG = Model(avionics=A_W, induced=0.0, flight=1.0, profile=0.0, growth=0.0, drag_area=F,
             disc_area=DISC, rms=0.0)  # fmt: skip
FULL = Model(avionics=60.0, induced=1.5, flight=1.25, profile=0.6, growth=0.03, drag_area=0.5,
             disc_area=DISC, rms=0.0)  # fmt: skip
W = 30 * G


def test_level_flight_power_is_avionics_and_parasite_drag() -> None:
    v = np.array([0.0, 5.0, 10.0])
    p = DRAG.power(W, v, 0.0, RHO, RULES)
    assert p == pytest.approx(A_W + 0.5 * RHO * F * v**3, rel=1e-9)


def test_the_best_range_is_the_tangent_from_the_origin() -> None:
    s = best(DRAG, W, RHO, RULES, top=30.0)
    assert s.range == pytest.approx((A_W / (RHO * F)) ** (1 / 3), abs=0.05)
    assert s.endurance == 0.0  # power only grows with speed
    assert s.range_band[0] < s.range < s.range_band[1]
    assert s.long_range > s.range  # 99% of the range, faster


def test_the_full_model_orders_its_speeds() -> None:
    s = best(FULL, W, RHO, RULES, top=30.0)
    assert 0 < s.endurance < s.range < s.long_range
    # the pack's heat grows with the current, so it favours less power: a little slower
    hot = best(FULL, W, RHO, RULES, top=30.0, cells=with_resistance(50.0, 0.05))
    assert hot.range <= s.range and hot.range_wh_per_km > s.range_wh_per_km


def test_into_a_headwind_fly_faster_through_the_air() -> None:
    c = 0.5 * RHO * F
    for w in (0.0, 3.0, 6.0):
        roots = np.roots([2 * c, -3 * c * w, 0.0, -A_W])
        want = max(r.real for r in roots if abs(r.imag) < 1e-9)
        got = leg(DRAG, W, RHO, RULES, 40.0, -w, 0.0)
        assert got is not None
        assert got.airspeed == pytest.approx(want, abs=0.06)
        assert got.ground == pytest.approx(got.airspeed - w, abs=1e-9)


def test_a_crosswind_takes_airspeed_to_hold_the_track() -> None:
    assert airspeed_for(10.0, 0.0, 6.0) == pytest.approx(math.hypot(10, 6))
    assert float(ground_for(np.array([10.0]), 2.0, 6.0)[0]) == pytest.approx(2 + 8)
    assert math.isnan(float(ground_for(np.array([5.0]), 0.0, 6.0)[0]))  # cannot hold it


def test_one_speed_setting_out_and_back() -> None:
    """In calm air the setting is the best-range speed; along a wind it is a little faster
    (the slow leg into the wind costs more than the fast one saves)."""
    calm = one_setting(DRAG, W, RHO, RULES, 30.0, 0.0, 0.0)
    assert calm is not None
    assert calm[0] == pytest.approx(best(DRAG, W, RHO, RULES, 30.0).range, abs=0.06)
    windy = one_setting(DRAG, W, RHO, RULES, 30.0, 4.0, 0.0)
    assert windy is not None and windy[1] > calm[1]


def test_out_and_back_in_calm_air_reaches_e_v_over_2p() -> None:
    trip = standard("out_and_back", RULES)
    flat = type(trip)(trip.kind, 0.0, 1.0, 1.0, 0.0)  # no climb, no hover
    budget = 1500.0
    r = radius(DRAG, RULES, 30.0, RHO, flat, budget, 0.0, 0.0, top=30.0)
    assert r is not None
    v = r.out.airspeed
    p = A_W + 0.5 * RHO * F * v**3
    assert r.metres == pytest.approx(budget * 3600 * v / (2 * p), rel=1e-6)


def test_wind_along_the_track_costs_the_same_either_way() -> None:
    trip = standard("out_and_back", RULES)
    calm = radius(FULL, RULES, 30.0, RHO, trip, 1500.0, 0.0, 0.0, 25.0)
    behind = radius(FULL, RULES, 30.0, RHO, trip, 1500.0, 6.0, 0.0, 25.0)
    ahead = radius(FULL, RULES, 30.0, RHO, trip, 1500.0, 6.0, math.pi, 25.0)
    assert calm is not None and behind is not None and ahead is not None
    assert behind.metres == pytest.approx(ahead.metres, rel=1e-9)
    assert behind.metres < calm.metres
    least, by = worst(FULL, RULES, 30.0, RHO, trip, 1500.0, 6.0, 25.0)
    assert least == pytest.approx(min(m for _, m in by))
    assert least <= behind.metres + 1e-6


def test_a_drop_flies_home_lighter_and_a_site_hover_costs_its_minutes() -> None:
    oab = radius(FULL, RULES, 38.0, RHO, standard("out_and_back", RULES), 1500.0, 0.0, 0.0, 25.0)
    drop = radius(FULL, RULES, 38.0, RHO, standard("drop", RULES, drop_kg=8.0), 1500.0, 0.0,
                  0.0, 25.0)  # fmt: skip
    site = radius(FULL, RULES, 38.0, RHO, standard("site", RULES), 1500.0, 0.0, 0.0, 25.0)
    assert oab is not None and drop is not None and site is not None
    assert drop.back.power < drop.out.power and drop.metres > oab.metres
    hover_wh = float(FULL.power(38.0 * G, 0.0, 0.0, RHO, RULES)[()]) * 300 / 3600
    assert site.fixed_wh - oab.fixed_wh == pytest.approx(hover_wh, rel=1e-9)
    assert site.metres < oab.metres


def test_no_budget_left_after_the_climb_gives_no_radius() -> None:
    trip = standard("site", RULES)
    assert radius(FULL, RULES, 30.0, RHO, trip, 50.0, 0.0, 0.0, 25.0) is None
