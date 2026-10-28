"""The maximum speed against closed forms. With frame drag alone (k2 v^2) and no lean in
still air, the lean theta holds `g sin(theta) = k2 (V cos(theta))^2`, so
`V = sqrt(g sin(theta) / k2) / cos(theta)`; the drag per kilogram halves at twice the
weight, so V grows by sqrt(2). The thrust limit leans no further than
`acos(throttle / 0.8)`, the throttle growing with the weight. On a model of avionics and
parasite drag the current limit is reached where `a + rho f V^3 / 2 = V_low I_max`."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from flight_insights.analyzers import Registry
from flight_insights.analyzers.wind import Fit
from flight_insights.knowledge import load_pack
from flight_insights.performance.battery import Chemistry, Pack
from flight_insights.performance.fit import G, Model
from flight_insights.performance.top_speed import Basis, drag_at, lowest_volts, top_speed

REG = Registry.load(load_pack())
RULES: dict[str, Any] = dict(REG.rules["performance"])
WIND: dict[str, Any] = dict(REG.rules["wind"]["envelope"])
DISC = 4 * math.pi * (22 * 0.0254 / 2) ** 2
RHO = 1.2
K2 = 0.02  # 1/m
FRAME = Fit(0.0, K2, np.zeros(2), np.zeros(2), True, 0.0)
DRAG = Model(avionics=800.0, induced=0.0, flight=1.0, profile=0.0, growth=0.0, drag_area=0.5,
             disc_area=DISC, rms=0.0)  # fmt: skip
PARAMS = {"ATC_ANGLE_MAX": 30.0}


def at_lean(deg: float, k2: float = K2) -> float:
    theta = math.radians(deg)
    return math.sqrt(G * math.sin(theta) / k2) / math.cos(theta)


def test_with_nothing_else_known_the_lean_limit_binds() -> None:
    basis = Basis(FRAME, 30.0, RHO, None)
    top = top_speed(DRAG, RULES, WIND, PARAMS, basis, 30.0, RHO, None, None, 30.0)
    assert top.by == "lean" and top.lean_deg == 30.0
    assert top.airspeed == pytest.approx(at_lean(30.0))
    assert top.thrust_ms is None and top.current_ms is None and not top.extrapolated
    psc = top_speed(DRAG, RULES, WIND, {"ATC_ANGLE_MAX": 30.0, "PSC_ANGLE_MAX": 20.0}, basis,
                    30.0, RHO, None, None, 30.0)  # fmt: skip
    assert psc.lean_deg == 20.0 and psc.airspeed == pytest.approx(at_lean(20.0))


def test_the_drag_per_kilogram_falls_with_the_weight_and_rises_with_the_air() -> None:
    basis = Basis(Fit(0.2, K2, np.zeros(2), np.zeros(2), True, 0.0), 30.0, RHO, None)
    heavy = drag_at(basis, 60.0, RHO)
    assert heavy.k2 == pytest.approx(K2 / 2) and heavy.k1 == pytest.approx(0.2 / math.sqrt(2))
    thin = drag_at(basis, 30.0, RHO / 2)
    assert thin.k2 == pytest.approx(K2 / 2) and thin.k1 == pytest.approx(0.2 / math.sqrt(2))
    frame = Basis(FRAME, 30.0, RHO, None)
    light = top_speed(DRAG, RULES, WIND, PARAMS, frame, 30.0, RHO, None, None, 30.0)
    twice = top_speed(DRAG, RULES, WIND, PARAMS, frame, 60.0, RHO, None, None, 30.0)
    assert twice.airspeed == pytest.approx(light.airspeed * math.sqrt(2))


def test_heavier_the_thrust_headroom_binds() -> None:
    basis = Basis(FRAME, 30.0, RHO, 0.5)
    params = {"ATC_ANGLE_MAX": 45.0}
    light = top_speed(DRAG, RULES, WIND, params, basis, 30.0, RHO, None, None, 30.0)
    # 0.5 of full: acos(0.5 / 0.8) = 51.3 degrees, more than the 45 configured
    assert light.by == "lean" and light.thrust_deg == pytest.approx(51.3178, abs=1e-3)
    heavy = top_speed(DRAG, RULES, WIND, params, basis, 42.0, RHO, None, None, 30.0)
    # 0.7 of full at 1.4 times the weight: acos(0.875) = 28.96 degrees
    assert heavy.by == "thrust" and heavy.thrust_deg is not None
    assert heavy.thrust_deg == pytest.approx(28.955, abs=1e-3)
    assert heavy.airspeed == pytest.approx(at_lean(heavy.thrust_deg, K2 * 30 / 42))
    # past 0.8 of full the firmware still allows 10 degrees
    over = top_speed(DRAG, RULES, WIND, params, basis, 50.0, RHO, None, None, 30.0)
    assert over.thrust_deg == 10.0


def test_the_current_limit_at_the_lowest_voltage() -> None:
    basis = Basis(FRAME, 30.0, RHO, None)
    volts, amps = 40.0, 50.0  # 2 kW
    top = top_speed(DRAG, RULES, WIND, PARAMS, basis, 30.0, RHO, volts, amps, 15.0)
    want = ((volts * amps - 800.0) / (0.5 * RHO * 0.5)) ** (1 / 3)
    assert top.current_w == 2000.0 and top.current_ms is not None
    assert top.lean_ms > top.current_ms
    assert top.current_ms == pytest.approx(want, abs=0.05)
    assert top.by == "current" and top.extrapolated  # faster than the 15 m/s flown
    # MOT_BAT_CURR_MAX lower than the pack's rating takes over
    capped = top_speed(DRAG, RULES, WIND, PARAMS | {"MOT_BAT_CURR_MAX": 30.0}, basis, 30.0,
                       RHO, volts, amps, 15.0)  # fmt: skip
    assert capped.current_w == 1200.0 and capped.airspeed < top.airspeed
    # a pack that cannot hold the hover at that voltage (600 W against 800) flies nowhere
    weak = top_speed(DRAG, RULES, WIND, PARAMS, basis, 30.0, RHO, volts, 15.0, 15.0)
    assert weak.airspeed == 0.0 and weak.by == "current"


def test_the_lowest_voltage_is_the_failsafes_or_the_cutoff() -> None:
    pack = Pack(Chemistry("c", np.array([0.0, 1.0]), np.array([3.3, 4.2]), 3.0, True, True),
                12, 30.0, 0.02)  # fmt: skip
    assert lowest_volts({}, pack) == pytest.approx(36.0)
    assert lowest_volts({"BATT_LOW_VOLT": 42.0}, pack) == 42.0
