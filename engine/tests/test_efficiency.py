"""A flight that drew more power than its airframe's other flights predict, by phase.
Synthetic flights from a known power model: one whose hover draws 20% more (a damaged
propeller) is flagged in hover alone, one whose level flight draws 25% more (something
hanging) in level flight alone, and a few per cent of scatter flags nothing. On ArduPilot's
simulator, Callisto's loaded speed card entered at its light weight is flagged in both,
and four flights entered at their own weights are not."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest
from test_perf_store import FIXTURES, PACK, airframe

from flight_insights.analyzers import Registry
from flight_insights.analyzers.registry import LANGUAGES, PLACEHOLDER
from flight_insights.analyzers.run import run_analysis
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.performance import airframe as perf
from flight_insights.performance.airframe import AirframeModel, rows
from flight_insights.performance.efficiency import HOVER, LEVEL, anomaly, over_time, spread
from flight_insights.performance.fit import G, Model, fitted
from flight_insights.performance.points import FlightPoints, Point
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store, open_store
from flight_insights.store.keys import StoreKeys

REG = Registry.load(PACK)
RULES: dict[str, Any] = dict(REG.rules["performance"])
DISC = 4 * math.pi * (22 * 0.0254 / 2) ** 2
TRUE = Model(avionics=60.0, induced=1.4, flight=1.2, profile=0.6, growth=0.03, drag_area=0.4,
             disc_area=DISC, rms=0.0)  # fmt: skip
CALLISTO = {"wheelbase_mm": 1325, "prop_diameter_in": 30}


def texts_filled(f: dict[str, Any]) -> None:
    text = REG.findings[f["code"]]
    for lang in LANGUAGES:
        for words in (text.title[lang], text.action[lang]):
            assert set(PLACEHOLDER.findall(words)) <= set(f["params"]), (f["code"], words)


def flight(name: str, kg: float, seed: int, hover: float = 1.0, level: float = 1.0
           ) -> FlightPoints:  # fmt: skip
    """Hover and level points from the true model, with 2% scatter, scaled per phase."""
    rng = np.random.default_rng(seed)
    pts: list[Point] = []
    for phase, speeds, scale in (("hover", [0.0, 0.0, 0.0], hover),
                                 ("level", [4.0, 6.0, 8.0, 10.0, 12.0], level)):  # fmt: skip
        for v in speeds:
            p = float(TRUE.power(kg * G, v, 0.0, 1.2, RULES)) * scale
            pts.append(Point(phase, v, 0.0, p * (1 + 0.02 * rng.standard_normal()), 1.2, 30.0, 15))
    return FlightPoints(name, tuple(pts), kg, "weighed", "flight", "baro")


def model_of(flights: list[FlightPoints]) -> AirframeModel:
    f = tuple(flights)
    got = fitted(rows(f), DISC, False, RULES)
    assert got is not None
    return AirframeModel("af", got, DISC, False, f, {}, None)


def test_the_phase_that_drew_more_is_flagged() -> None:
    clean = [flight(f"c{k}", kg, k) for k, kg in enumerate((28.0, 32.0, 36.0, 40.0))]
    am = model_of([*clean, flight("prop", 34.0, 9, hover=1.2), flight("drag", 30.0, 8, level=1.25)])
    analysis = {"evidence": [{"id": "energy:flight"}]}
    [h] = anomaly("prop", am, RULES, analysis)
    assert h["code"] == HOVER and h["params"]["excess_pct"] == pytest.approx(20, abs=4)
    assert h["evidence"] == ["energy:flight"] and h["severity"] == "caution"
    [lv] = anomaly("drag", am, RULES, analysis)
    assert lv["code"] == LEVEL and lv["params"]["excess_pct"] == pytest.approx(25, abs=5)
    for f in (h, lv):
        texts_filled(f)
    # the clean flights miss their predictions by a few per cent: nothing
    for c in clean:
        assert anomaly(c.flight_id, am, RULES, analysis) == []


def test_too_few_flights_or_no_weight_flags_nothing() -> None:
    two = model_of([flight("a", 30.0, 1), flight("b", 36.0, 2, hover=1.5)])
    assert anomaly("b", two, RULES, {}) == []
    assert spread([]) == 0.0 and spread([0.01, 0.01, 0.01]) == 0.0


def test_each_flight_on_one_scale() -> None:
    """The airframe's figures at one weight, scaled by how far each flight's power sat from
    the model: a flight that drew 20% more hovering shows 20% fewer grams per watt, one
    that drew 25% more in level flight 25% more energy per km, at whatever weight each
    flew; the rest sit on the airframe's own within their scatter."""
    clean = [flight(f"c{k}", kg, k) for k, kg in enumerate((28.0, 32.0, 36.0, 40.0))]
    prop, drag = flight("prop", 34.0, 9, hover=1.2), flight("drag", 30.0, 8, level=1.25)
    unweighed = FlightPoints("x", prop.points, None, "none", "flight", "baro")
    order = [clean[0], prop, clean[1], drag, unweighed, clean[2], clean[3]]
    got, thresholds = over_time(TRUE, order, RULES, 8.0, 0.6, 100.0)
    assert [o.flight_id for o in got] == ["c0", "prop", "c1", "drag", "c2", "c3"]
    by = {o.flight_id: o for o in got}
    assert by["prop"].flagged == ("hover",) and by["drag"].flagged == ("level",)
    assert by["prop"].grams_per_watt == pytest.approx(8.0 / 1.2, rel=0.03)
    assert by["prop"].figure_of_merit == pytest.approx(0.6 / 1.2, rel=0.03)
    assert by["drag"].wh_per_km == pytest.approx(125.0, rel=0.03)
    for c in clean:
        o = by[c.flight_id]
        assert o.flagged == () and o.hover == pytest.approx(1.0, abs=0.04)
        assert o.wh_per_km == pytest.approx(100.0 * (o.level or 0), rel=1e-12)
    assert thresholds["hover"] == pytest.approx(RULES["anomaly"]["min_excess"])
    # without a cruise speed, no energy per km; with two flights, no threshold to judge by
    few, none = over_time(TRUE, order[:2], RULES, 8.0, 0.6, None)
    assert all(o.wh_per_km is None for o in few) and few[1].flagged == ()
    assert none == {"hover": None, "level": None}


def fly(store: Store, cache: ColumnCache, name: str, af: str, kg: float) -> str:
    batch = scan(store, PACK, bytes(32), [FIXTURES / f"{name}.bin"])
    decision = Decision(batch[0].id, airframe_id=af, takeoff_kg=kg)
    [res] = commit(store, PACK, cache, batch, propose(store, PACK, batch), [decision])
    run_analysis(store, cache, PACK, res.flights[0])
    return res.flights[0]


def test_a_weight_entered_wrong_is_flagged_on_the_simulator(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("anomaly")
    store = open_store(folder / "a.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    af = airframe(store, "Callisto", CALLISTO)
    clean = (("sweep", 32.5), ("light", 32.5), ("loaded", 40.5), ("discharge", 32.5))
    ids = [fly(store, cache, f"copter-heavylift-{name}", af, kg) for name, kg in clean]
    wrong = fly(store, cache, "copter-heavylift-sweep-loaded", af, 32.5)  # 40.5 kg flown
    am = perf.load(store, cache, PACK, af)
    analysis = run_analysis(store, cache, PACK, wrong)
    got = {f["code"]: f["params"] for f in anomaly(wrong, am, RULES, analysis)}
    # 25% more thrust: the simulator's motors draw power in proportion to it
    assert set(got) == {HOVER, LEVEL} and got[HOVER]["excess_pct"] > 25
    # the flights entered at their own weights: nothing
    assert all(anomaly(f, am, RULES, {}) == [] for f in ids)
