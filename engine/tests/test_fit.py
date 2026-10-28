"""The power model's fit on points made from a known airframe: a 30 kg coaxial X8 with
0.5 m^2 of flat-plate drag swept from hover to 20 m/s at two weights, with 1% noise and
each flight's power up to 2% off.
The fit gives back the power at every point, the drag area and the speeds that matter,
says what the data supports, and predicts each flight's energy without it."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from flight_insights.analyzers import Registry
from flight_insights.knowledge import load_pack
from flight_insights.performance.fit import G, Model, Row, fit, fitted, level, priors

RULES: dict[str, Any] = dict(Registry.load(load_pack()).rules["performance"])
DISC = 4 * math.pi * (22 * 0.0254 / 2) ** 2  # four distinct 22-inch discs
TRUE = Model(avionics=60.0, induced=1.5, flight=1.25, profile=0.6, growth=0.03, drag_area=0.5,
             disc_area=DISC, rms=0.0)  # fmt: skip
SPEEDS = (0.0, *range(2, 21))


def flown(name: str, kg: float, rng: np.random.Generator, offset: float,
          speeds: tuple[float, ...] = SPEEDS, climbs: tuple[float, ...] = (1.0, 2.0, 3.0),
          noise: float = 0.01) -> list[Row]:  # fmt: skip
    rows: list[Row] = []
    for v, c in [(v, 0.0) for v in speeds] + [(0.0, c) for c in climbs]:
        p = float(TRUE.power(kg * G, v, c, 1.2, RULES)[()]) * (1 + offset)
        rows.append(Row(name, kg * G, v, c, p * (1 + rng.normal(0, noise)), 1.2, 20.0))
    return rows


@pytest.fixture(scope="module")
def sweep() -> list[Row]:
    rng = np.random.default_rng(5)
    return (flown("light-1", 30.0, rng, 0.01) + flown("light-2", 30.0, rng, -0.015)
            + flown("loaded-1", 38.0, rng, 0.02) + flown("loaded-2", 38.0, rng, -0.01))  # fmt: skip


def test_the_true_model_has_a_power_bucket() -> None:
    p = TRUE.power(30 * G, np.array([0.0, 10.0, 18.0]), 0.0, 1.2, RULES)
    assert p[1] < p[0] and p[1] < p[2]


def test_the_fit_gives_back_the_power_at_every_point(sweep: list[Row]) -> None:
    model = fit(sweep, DISC, True, RULES)
    assert model is not None
    for kg in (30.0, 38.0):
        v = np.linspace(0, 20, 41)
        got = model.power(kg * G, v, 0.0, 1.2, RULES)
        want = TRUE.power(kg * G, v, 0.0, 1.2, RULES)
        assert np.max(np.abs(got / want - 1)) < 0.03
    assert model.drag_area == pytest.approx(TRUE.drag_area, rel=0.25)
    assert model.rms < 0.02
    # each flight's factor read back as its offset
    assert model.factors["loaded-1"] == pytest.approx(1.02, abs=0.01)


def test_the_best_speeds_come_back(sweep: list[Row]) -> None:
    model = fit(sweep, DISC, True, RULES)
    assert model is not None
    v = np.linspace(0.5, 25, 491)

    def speeds(m: Model) -> tuple[float, float]:
        p = m.power(30 * G, v, 0.0, 1.2, RULES)
        return float(v[int(np.argmin(p))]), float(v[int(np.argmin(p / v))])

    (endurance, distance), (got_endurance, got_distance) = speeds(TRUE), speeds(model)
    assert got_endurance == pytest.approx(endurance, abs=0.6)
    assert got_distance == pytest.approx(distance, abs=1.0)


def test_a_sweep_at_two_weights_is_level_a_and_hovers_are_c(sweep: list[Row]) -> None:
    assert level(sweep, RULES) == "A"
    one = [r for r in sweep if r.flight.startswith("light")]
    assert level(one, RULES) == "B"
    hovers = [r for r in sweep if r.airspeed == 0.0]
    assert level(hovers, RULES) == "C"


def test_each_flights_energy_is_predicted_without_it(sweep: list[Row]) -> None:
    rules = dict(RULES)
    rules["fit"] = dict(RULES["fit"]) | {"bootstrap": 20}
    got = fitted(sweep, DISC, True, rules)
    assert got is not None and got.level == "A" and got.flights == 4
    assert set(got.lofo) == {"light-1", "light-2", "loaded-1", "loaded-2"}
    # what is left is each flight's own offset: a few percent
    assert max(abs(e) for e in got.lofo.values()) < 0.04
    lo, hi = got.bootstrap["drag_area"]
    assert lo <= got.model.drag_area <= hi
    # the bootstrap's own models are kept for the projections' spread
    assert len(got.replicates) == 20
    areas = [m.drag_area for m in got.replicates]
    assert (np.percentile(areas, 2.5), np.percentile(areas, 97.5)) == pytest.approx((lo, hi))


def test_with_hovers_alone_the_priors_hold_the_shape() -> None:
    rng = np.random.default_rng(9)
    rows = flown("h-1", 30.0, rng, 0.0, speeds=(0.0,)) + flown("h-2", 34.0, rng, 0.0,
                                                                 speeds=(0.0,))  # fmt: skip
    model = fit(rows, DISC, True, RULES)
    assert model is not None
    mean, _ = priors(RULES, True, 3000.0)
    assert model.flight == pytest.approx(mean[2], rel=0.05)  # nothing measured it
    hover = model.power(32 * G, 0.0, 0.0, 1.2, RULES)
    assert float(hover[()]) == pytest.approx(float(TRUE.power(32 * G, 0.0, 0.0, 1.2, RULES)[()]),
                                             rel=0.02)  # fmt: skip


def test_too_few_points_give_no_model() -> None:
    rng = np.random.default_rng(1)
    assert fit(flown("x", 30.0, rng, 0.0, speeds=(0.0,), climbs=())[:1], DISC, True,
               RULES) is None  # fmt: skip
