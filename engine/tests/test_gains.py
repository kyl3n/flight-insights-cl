"""What each limit the models predict allows, and what each of the rules' solutions would
add, against closed forms: the lean a quadratic drag needs, the current a cubic parasite
power draws at the failsafe's voltage, the ESCs' settled temperature, and the lean the
thrust allows."""

from __future__ import annotations

import math

import numpy as np
import pytest

from flight_insights.analyzers import Registry
from flight_insights.analyzers.wind import Fit
from flight_insights.knowledge import load_pack
from flight_insights.performance.envelope import Inputs, allows, gains
from flight_insights.performance.fit import G, Model
from flight_insights.performance.thermal import Thermal
from flight_insights.performance.top_speed import Basis

REG = Registry.load(load_pack())
RHO, MASS, K2, A_W, F, VOLTS = 1.2, 30.0, 0.004, 800.0, 0.5, 42.0
DISC = 4 * math.pi * (22 * 0.0254 / 2) ** 2
MODEL = Model(avionics=A_W, induced=0.0, flight=1.0, profile=0.0, growth=0.0, drag_area=F,
              disc_area=DISC, rms=0.0)  # fmt: skip


def inputs(rating: float = 100.0, thermals: tuple[Thermal, ...] = (), esc: float | None = None,
           throttle: float | None = 0.5, k2: float = K2) -> Inputs:  # fmt: skip
    drag = Fit(k1=0.0, k2=k2, wind=np.zeros(2), bias=np.zeros(2), quadratic=True, rms=0.0)
    return Inputs(MODEL, Basis(drag, MASS, RHO, throttle), {"PSC_ANGLE_MAX": 30.0}, VOLTS,
                  rating, thermals, esc, 4)  # fmt: skip


def wind_at_lean(deg: float, k2: float) -> float:
    """The wind whose drag g sin(t) = k2 (w cos t)^2 balances at lean t."""
    t = math.radians(deg)
    return math.sqrt(G * math.sin(t) / k2) / math.cos(t)


def speed_at_power(watts: float, drag_area: float = F) -> float:
    return ((watts - A_W) / (0.5 * RHO * drag_area)) ** (1 / 3)


def test_the_lean_allows_the_speed_its_target_lean_holds() -> None:
    # 80% of the 30-degree limit, 24 degrees
    now, more = gains("lean", inputs(), MASS, RHO, REG)
    assert now == pytest.approx(wind_at_lean(24.0, K2), abs=0.06)
    assert more["drag"] == pytest.approx(wind_at_lean(24.0, K2 * 0.85) - now, abs=0.06)


def test_the_current_allows_the_speed_the_packs_rating_powers() -> None:
    now, more = gains("current", inputs(), MASS, RHO, REG)
    assert now == pytest.approx(speed_at_power(100 * VOLTS), abs=0.06)
    assert more["rating"] == pytest.approx(speed_at_power(125 * VOLTS) - now, abs=0.06)
    assert more["drag"] == pytest.approx(speed_at_power(100 * VOLTS, F * 0.85) - now, abs=0.06)


def test_the_escs_allow_the_speed_they_settle_cool_enough_at() -> None:
    hot = Thermal(instance=1, tau_s=90.0, k=0.15, air_c=30.0, rms_c=0.5, current_max_a=60.0)
    # 100 degC protection less 10: (I/4)^2 <= 60/0.15, I <= 80 A at the failsafe's volts
    now, more = gains("esc_temp", inputs(1000.0, (hot,), 100.0), MASS, RHO, REG)
    assert now == pytest.approx(speed_at_power(80 * VOLTS), abs=0.06)
    cooler = 4 * math.sqrt(60 / (0.15 * 0.8)) * VOLTS
    assert more["cooling"] == pytest.approx(speed_at_power(cooler) - now, abs=0.06)


def test_the_thrust_allows_the_lean_its_throttle_leaves() -> None:
    k2 = 0.04  # draggy enough that the thrust's lean is reached inside the search
    now, more = gains("thrust", inputs(k2=k2), MASS, RHO, REG)
    assert now == pytest.approx(wind_at_lean(math.degrees(math.acos(0.5 / 0.8)), k2), abs=0.06)
    stronger = math.degrees(math.acos(0.5 / 1.1 / 0.8))
    assert more["thrust"] == pytest.approx(wind_at_lean(stronger, k2) - now, abs=0.06)
    # 10% lighter: more drag per kilogram, but a lower throttle leaves more lean
    lighter = math.degrees(math.acos(0.5 * 0.9 / 0.8))
    assert more["weight"] == pytest.approx(wind_at_lean(lighter, k2 / 0.9) - now, abs=0.06)
    assert more["thrust"] > 0 and more["weight"] > 0


def test_a_limit_that_holds_nowhere_allows_nothing() -> None:
    # a rating below the avionics' own draw at the failsafe's voltage
    assert allows("current", inputs(rating=10.0), MASS, RHO, REG) == 0.0
    # a check the models cannot make: none
    assert allows("esc_temp", inputs(), MASS, RHO, REG) == 0.0
