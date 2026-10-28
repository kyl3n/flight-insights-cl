"""Flight time, range and radius from the model and the pack, against closed forms. A pack
with a flat voltage and no resistance gives the same energy at every load, so on a model of
avionics plus parasite drag (`P = a + rho f V^3 / 2`) the hover lasts `E / a`, the range is
`V E / P` at `V^3 = a / (rho f)`, and the radius is what `radius` makes of that energy. The
pack's resistance costs more the harder it is worked, and moves the best range slower; the
spread comes from the bootstrap's models and the flights' own errors; each what-if moves
what it should and nothing else."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from flight_insights.analyzers import Registry
from flight_insights.knowledge import load_pack
from flight_insights.performance.battery import Chemistry, Failsafe, Pack, usable_curve
from flight_insights.performance.fit import Fitted, G, Model
from flight_insights.performance.optimize import best
from flight_insights.performance.project import (
    against_weight,
    figures,
    pack_curve,
    spread,
    trip_radius,
    what_ifs,
    worst_trip,
)
from flight_insights.performance.radius import radius, standard, worst

RULES: dict[str, Any] = dict(Registry.load(load_pack()).rules["performance"])
DISC = 4 * math.pi * (22 * 0.0254 / 2) ** 2
RHO = 1.2
A_W = 800.0
F = 0.5
DRAG = Model(avionics=A_W, induced=0.0, flight=1.0, profile=0.0, growth=0.0, drag_area=F,
             disc_area=DISC, rms=0.0)  # fmt: skip
FULL = Model(avionics=60.0, induced=1.5, flight=1.25, profile=0.6, growth=0.03, drag_area=0.5,
             disc_area=DISC, rms=0.0)  # fmt: skip
FLAT = Chemistry("flat", np.array([0.0, 1.0]), np.array([3.7, 3.7]), 3.0, True, True)
SLOPE = Chemistry("slope", np.array([0.0, 1.0]), np.array([3.3, 4.2]), 3.0, True, True)
STEADY = Pack(FLAT, 12, 20.0, 0.0)  # 44.4 V x 20 Ah = 888 Wh
HEAVY = Pack(SLOPE, 12, 32.0, 0.02)
KEEP = 0.8  # what the reserve leaves: 20% of the energy to the cutoff
E = 888.0 * KEEP


def steady_curve(model: Model = DRAG, masses: tuple[float, ...] = (30.0,)) -> Any:
    return pack_curve(model, RULES, STEADY, 1.0, None, 0.2, list(masses), RHO, 30.0)


def test_a_steady_pack_gives_the_same_energy_at_any_load() -> None:
    curve = usable_curve(STEADY, 1.0, None, 0.2, 500.0, 5000.0)
    assert curve.energy_wh == pytest.approx(np.full(24, E), rel=2e-3)
    assert set(curve.stopped_by) == {"reserve"}
    assert float(curve.seconds(1000.0)) == pytest.approx(E * 3.6, rel=2e-3)
    assert float(curve.at(100.0)) == pytest.approx(E, rel=2e-3)  # below the grid: its lightest
    assert float(curve.at(5001.0)) == 0.0  # above it: nothing


def test_resistance_costs_more_the_harder_the_pack_is_worked() -> None:
    curve = usable_curve(HEAVY, 1.0, Failsafe(), 0.2, 1000.0, 20000.0)
    assert np.all(np.diff(curve.energy_wh) < 0)
    # past E^2 / 4R from a full pack it cannot give the power at all
    full = 12 * 4.2
    beyond = usable_curve(HEAVY, 1.0, None, 0.2, full**2 / 0.08 * 1.01, full**2 / 0.08 * 1.2,
                          points=3)  # fmt: skip
    assert list(beyond.energy_wh) == [0.0, 0.0, 0.0]
    assert set(beyond.stopped_by) == {"power"}


def test_flight_time_range_and_radius_match_the_closed_forms() -> None:
    curve = steady_curve()
    f = figures(DRAG, RULES, curve, 30.0, 30.0, RHO, 30.0, (29.0, 31.0))
    assert f is not None and f.measured
    assert f.hover_s == pytest.approx(E * 3600 / A_W, rel=2e-3)
    assert f.endurance_ms == 0.0 and f.endurance_s == pytest.approx(f.hover_s)
    v = (A_W / (RHO * F)) ** (1 / 3)
    assert f.range_ms == pytest.approx(v, abs=0.06)
    p = A_W + 0.5 * RHO * F * f.range_ms**3
    assert f.range_m == pytest.approx(f.range_ms * E * 3600 / p, rel=2e-3)
    for kind in ("out_and_back", "site"):
        r = radius(DRAG, RULES, 30.0, RHO, standard(kind, RULES), E, 0.0, 0.0, 30.0)
        assert r is not None
        assert f.radius_m[kind] == pytest.approx(r.metres, rel=3e-3)
    eff = f.efficiency
    ideal = 30 * G * math.sqrt(30 * G / (2 * 1.225 * DISC))
    assert eff.figure_of_merit == pytest.approx(ideal / A_W)  # avionics alone: 800 W
    assert eff.grams_per_watt == pytest.approx(30000 / A_W)
    assert eff.wh_per_km == pytest.approx(p / f.range_ms / 3.6)
    assert eff.wh_per_kg_km == pytest.approx(eff.wh_per_km / 30)
    assert eff.cost_of_transport == pytest.approx(p / (30 * G * f.range_ms))
    # an empty weight leaves no payload: the drop is out and back with its hover
    assert f.radius_m["drop"] < f.radius_m["out_and_back"]


def test_the_pack_sag_puts_the_best_range_slower() -> None:
    curve = pack_curve(FULL, RULES, HEAVY, 1.0, Failsafe(), 0.2, [32.5], RHO, 25.0)
    f = figures(FULL, RULES, curve, 32.5, 25.0, RHO, 25.0)
    assert f is not None and not f.measured
    assert f.range_ms < best(FULL, 32.5 * G, RHO, RULES, 25.0).range


def test_a_trips_energy_is_what_the_pack_gives_at_its_mean_power() -> None:
    curve = pack_curve(FULL, RULES, HEAVY, 1.0, Failsafe(), 0.2, [32.5], RHO, 25.0)
    trip = standard("site", RULES)
    r = trip_radius(FULL, RULES, curve, 32.5, RHO, trip, 4.0, 0.7, 25.0)
    assert r is not None
    spent = r.fixed_wh + r.metres * (r.out.wh_per_km + r.back.wh_per_km) / 1000
    seconds = (trip.height_m / trip.climb_ms + trip.site_s + trip.height_m / trip.descent_ms
               + r.metres / r.out.ground + r.metres / r.back.ground)  # fmt: skip
    assert float(curve.at(spent * 3600 / seconds)) == pytest.approx(spent, rel=2e-3)


def test_the_worst_direction_is_mirrored_and_matches_a_fixed_budget() -> None:
    curve = steady_curve(FULL)
    trip = standard("out_and_back", RULES)
    least, by = worst_trip(FULL, RULES, curve, 30.0, RHO, trip, 6.0, 25.0)
    assert len(by) == 72 and [d for d, _ in by] == [5.0 * k for k in range(72)]
    assert all(by[k][1] == by[-k][1] for k in range(1, 36))
    fixed, _ = worst(FULL, RULES, 30.0, RHO, trip, float(curve.at(3000.0)), 6.0, 25.0)
    assert least == pytest.approx(fixed, rel=3e-3)


def test_heavier_flies_shorter_and_beyond_the_flown_is_marked() -> None:
    curve = pack_curve(FULL, RULES, HEAVY, 1.0, Failsafe(), 0.2, [25.0, 45.0], RHO, 25.0)
    got = against_weight(FULL, RULES, curve, 25.0, 45.0, RHO, 25.0, (30.0, 35.0))
    assert [f.mass_kg for f in got] == pytest.approx(list(np.linspace(25.0, 45.0, 9)))
    for k in ("hover_s", "endurance_s", "range_m"):
        assert np.all(np.diff([getattr(f, k) for f in got]) < 0), k
    assert np.all(np.diff([f.radius_m["out_and_back"] for f in got]) < 0)
    assert [f.mass_kg for f in got if f.measured] == pytest.approx([30.0, 32.5, 35.0])


def test_a_pack_that_cannot_hold_the_hover_gives_nothing() -> None:
    weak = Pack(SLOPE, 12, 32.0, 0.5)  # 0.5 Ohm: about 780 W at most
    curve = pack_curve(FULL, RULES, weak, 1.0, None, 0.2, [32.5], RHO, 25.0)
    assert figures(FULL, RULES, curve, 32.5, 25.0, RHO, 25.0) is None
    assert against_weight(FULL, RULES, curve, 25.0, 45.0, RHO, 25.0) == []


def fitted(lofo: dict[str, float], copies: int = 40) -> Fitted:
    return Fitted(DRAG, "A", 100, len(lofo), lofo, {}, 0, (DRAG,) * copies)


def test_the_spread_is_the_models_and_the_flights_own_errors() -> None:
    curve = steady_curve()
    f = figures(DRAG, RULES, curve, 30.0, 30.0, RHO, 30.0)
    assert f is not None
    exact = spread(fitted({"a": 0.0, "b": 0.0}), RULES, curve, 30.0, 30.0, RHO, 30.0)
    assert exact["hover_s"].low == pytest.approx(f.hover_s)
    assert exact["range_m"].high == pytest.approx(f.range_m)
    assert exact["radius_m"].median == pytest.approx(f.radius_m["out_and_back"])
    # a flight predicted 5% high flew on 5% less power than the model gives: 5% longer
    wide = spread(fitted({"a": 0.05, "b": -0.05}), RULES, curve, 30.0, 30.0, RHO, 30.0)
    assert wide["hover_s"].low < f.hover_s < wide["hover_s"].high
    assert wide["hover_s"].high == pytest.approx(f.hover_s * 1.05, rel=3e-3)
    assert wide["hover_s"].low == pytest.approx(f.hover_s * 0.95, rel=3e-3)


def test_each_what_if_moves_what_it_should() -> None:
    got = {w.lever: w for w in what_ifs(DRAG, RULES, STEADY, 1.0, None, 0.2, 30.0, 25.0, None,
                                        RHO, 30.0)}  # fmt: skip
    assert set(got) == {"mass", "drag", "avionics"}  # no pack mass: no capacity
    # the avionics-and-drag model's power does not depend on weight
    assert got["mass"].change == pytest.approx(-1.5) and got["mass"].hover_s == 0.0
    assert got["drag"].hover_s == 0.0 and got["drag"].range_m > 0
    assert got["drag"].change == pytest.approx(-0.05)
    assert got["avionics"].hover_s == pytest.approx(E * 3600 / (A_W - 10) - E * 3600 / A_W,
                                                    rel=0.02)  # fmt: skip
    full = {w.lever: w for w in what_ifs(FULL, RULES, HEAVY, 1.0, Failsafe(), 0.2, 32.5, 25.0,
                                         8.0, RHO, 25.0)}  # fmt: skip
    assert full["mass"].hover_s > 0 and full["mass"].range_m > 0
    assert full["drag"].hover_s == 0.0 and full["drag"].range_m > 0
    assert full["capacity"].change == pytest.approx(3.2) and full["capacity"].range_m > 0
