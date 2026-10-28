"""The speeds the plan proposes from the airframe's power model: legs held at a set ground
speed against their worked answers, the wind climate against the still-air setting, the
choice between the current setting and the best (lowered always, raised only up to the
verified speed less the typical wind, never judged past what was flown), the plan's never
rules with the measured limits, and ArduPilot's simulator flying Callisto's speed card."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest
from test_perf_report import make_fleet
from test_perf_store import PACK
from test_recommend import BASE, finding, one

from flight_insights.analyzers import Registry
from flight_insights.analyzers.registry import LANGUAGES, PLACEHOLDER
from flight_insights.analyzers.run import run_analysis
from flight_insights.history.persist import load_snapshot
from flight_insights.params import Dialect
from flight_insights.performance.advice import CRUISE, HOME, UNVERIFIED, choose, speed_advice
from flight_insights.performance.fit import Model
from flight_insights.performance.optimize import Array, climate, held, one_setting
from flight_insights.recommend import build_plan
from flight_insights.recommend.plan import Plan

REG = Registry.load(PACK)
RULES: dict[str, Any] = dict(REG.rules["performance"])
RHO, A_W, F = 1.2, 800.0, 0.5
DISC = 4 * math.pi * (22 * 0.0254 / 2) ** 2
DRAG = Model(avionics=A_W, induced=0.0, flight=1.0, profile=0.0, growth=0.0, drag_area=F,
             disc_area=DISC, rms=0.0)  # fmt: skip
W = 30.0 * 9.80665


def power(v: float) -> float:
    return A_W + 0.5 * RHO * F * v**3


def test_a_held_leg_costs_the_airspeed_its_wind_takes() -> None:
    # 10 m/s over the ground into 5 m/s takes 15 through the air; with it, 5
    g = np.array([10.0])
    assert held(DRAG, W, RHO, RULES, g, -5.0, 0.0)[0] == pytest.approx(power(15.0) / 10)
    assert held(DRAG, W, RHO, RULES, g, 5.0, 0.0)[0] == pytest.approx(power(5.0) / 10)
    # 3 m/s across: the airspeed holds the track, sqrt(10^2 + 3^2)
    across = held(DRAG, W, RHO, RULES, g, 0.0, 3.0)[0]
    assert across == pytest.approx(power(math.hypot(10, 3)) / 10)


def test_in_still_air_the_climate_is_the_one_out_and_back_setting() -> None:
    g = np.arange(1.0, 30.0, 0.05)
    calm = climate(DRAG, W, RHO, RULES, [], g)
    assert calm == pytest.approx(2 * np.array([power(x) for x in g]) / g * 1000 / 3600)
    best = one_setting(DRAG, W, RHO, RULES, 30.0, 0.0, 0.0)
    assert best is not None and g[int(np.argmin(calm))] == pytest.approx(best[0], abs=0.05)


def test_a_wind_from_every_side_costs_range_and_asks_a_faster_setting() -> None:
    g = np.arange(6.0, 30.0, 0.05)
    calm, windy = climate(DRAG, W, RHO, RULES, [0.0], g), climate(DRAG, W, RHO, RULES, [5.0], g)
    assert np.all(windy > calm)
    assert g[int(np.argmin(windy))] > g[int(np.argmin(calm))]


def bowl(best: float) -> Callable[[Array], Array]:
    """A cost least at `best`, rising 1% for each m/s squared away from it."""

    def cost(g: Array) -> Array:
        return 1.0 + 0.01 * (np.asarray(g, np.float64) - best) ** 2

    return cost


def test_the_choice_between_the_setting_and_the_best() -> None:
    # within 2% of the best: nothing
    assert choose(bowl(10.0), 14.0, 11.0, RULES, 0.1, None) is None
    # slower than the band: raised to the best when the limit allows
    up = choose(bowl(10.0), 14.0, 6.0, RULES, 0.1, 12.0)
    assert up is not None and up.proposed == 10.0 and not up.unverified
    assert up.gain_pct == pytest.approx(100 * (1 - 1 / 1.16)) and up.loss_pct == pytest.approx(16)
    # past the limit: raised only to it, and said
    capped = choose(bowl(10.0), 14.0, 6.0, RULES, 0.1, 8.37)
    assert capped is not None and capped.proposed == 8.3 and capped.unverified
    # nothing verified: no raise at all
    none = choose(bowl(10.0), 14.0, 6.0, RULES, 0.1, None)
    assert none is not None and none.proposed is None and none.unverified
    # faster than the band: lowered, whatever the limit
    down = choose(bowl(10.0), 14.0, 13.5, RULES, 0.1, None)
    assert down is not None and down.proposed == 10.0 and not down.unverified


def test_past_what_was_flown_only_a_rising_cost_lowers_it() -> None:
    # flown to 14 with the best at 10: past 14 the cost only rises further
    down = choose(bowl(10.0), 14.0, 15.0, RULES, 0.1, None)
    assert down is not None and down.proposed == 10.0
    assert down.gain_pct == pytest.approx(100 * (1 - 1 / 1.16))  # read at 14: at least this
    # the best at the end of what was flown: past it cannot be judged
    assert choose(bowl(20.0), 14.0, 15.0, RULES, 0.1, None) is None
    # a best slower than the least cruise speed is a hover
    assert choose(bowl(1.0), 14.0, 8.0, RULES, 0.1, None) is None


def plan(*findings: dict[str, Any], limits: dict[str, float] | None = None,
         **params: float) -> Plan:  # fmt: skip
    dialect = Dialect(REG.pack, "copter", "4.7.2")
    return build_plan("fl", list(findings), BASE | params, dialect, REG, limits)


def test_a_speed_rises_only_up_to_the_measured_limit() -> None:
    raised = finding(CRUISE, ("WP_SPD", 12.0), severity="info", confidence="low")
    assert one(plan(raised, WP_SPD=10.0), "WP_SPD").status == "blocked"
    assert one(plan(raised, limits={"WP_SPD": 12.0}, WP_SPD=10.0), "WP_SPD").status != "blocked"
    assert one(plan(raised, limits={"WP_SPD": 11.9}, WP_SPD=10.0), "WP_SPD").status == "blocked"
    lowered = finding(CRUISE, ("WP_SPD", 8.0), severity="info", confidence="low")
    assert one(plan(lowered, WP_SPD=10.0), "WP_SPD").status != "blocked"


def test_rtl_speed_at_zero_flies_at_wp_spd() -> None:
    def home(value: float) -> str:
        f = finding(HOME, ("RTL_SPEED_MS", value), severity="info", confidence="low")
        return one(plan(f, WP_SPD=10.0, RTL_SPEED_MS=0.0), "RTL_SPEED_MS").status

    assert home(8.0) != "blocked"  # slower than the 10 it flies at now
    assert home(12.0) == "blocked"


def test_the_battery_failsafe_charge_is_never_lowered() -> None:
    def low(value: float) -> tuple[str, list[str]]:
        f = finding("params.battery_no_low", ("BATT_LOW_MAH", value))
        rec = one(plan(f, BATT_MONITOR=4.0, BATT_LOW_MAH=6000.0), "BATT_LOW_MAH")
        return rec.status, [n.reason for n in rec.notes]

    assert low(4000.0) == ("blocked", ["never_lower"])
    assert low(8000.0)[0] != "blocked"


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return make_fleet(tmp_path_factory)


@pytest.fixture(scope="module")
def light(fleet: dict[str, Any]) -> tuple[str, dict[str, float], dict[str, Any]]:
    store, cache = fleet["store"], fleet["cache"]
    [flight] = [r[0] for r in store.conn.execute(
        "SELECT id FROM flight WHERE airframe_id = ? AND takeoff_kg = 32.5",
        (fleet["airframe"],))]  # fmt: skip
    analysis = run_analysis(store, cache, PACK, flight)
    snap = load_snapshot(store, store.get("flight", flight)["param_snapshot_id"])
    params = dict(snap.dialect(PACK).to_canonical(snap.values).values)
    return flight, params, analysis


def test_on_the_simulator_nothing_past_what_was_flown(fleet: dict[str, Any], light: Any) -> None:
    flight, params, analysis = light
    got = speed_advice(fleet["store"], fleet["cache"], PACK, REG, flight, params, analysis)
    # WP_SPD 15 is past the 12 m/s flown, where the energy per km is still falling
    assert params["WP_SPD"] == 15.0 and got.findings == []
    # 12 m/s verified, less the typical wind, for each speed setting
    assert set(got.raise_limits) == {"WP_SPD", "LOIT_SPEED_MS", "RTL_SPEED_MS"}
    assert 11.0 < got.raise_limits["WP_SPD"] <= 12.0


def test_on_the_simulator_a_slow_setting_rises_to_the_limit(
    fleet: dict[str, Any], light: Any
) -> None:
    flight, params, analysis = light
    got = speed_advice(fleet["store"], fleet["cache"], PACK, REG, flight,
                       params | {"WP_SPD": 6.0, "RTL_SPEED_MS": 0.0}, analysis)  # fmt: skip
    by = {f["code"]: f for f in got.findings}
    assert set(by) == {CRUISE, UNVERIFIED, HOME}
    limit = got.raise_limits["WP_SPD"]
    cruise, home = by[CRUISE], by[HOME]
    assert cruise["changes"][0]["param"] == "WP_SPD"
    assert 6.0 < cruise["params"]["proposed_ms"] <= limit < by[UNVERIFIED]["params"]["best_ms"]
    assert home["changes"][0]["param"] == "RTL_SPEED_MS"
    assert 6.0 < home["params"]["proposed_ms"] <= limit
    for f in got.findings:
        assert f["evidence"] == ["energy:flight"] and f["confidence"] == "low"
        assert f["params"]["gain_pct"] > 0 if f["changes"] else True
        text = REG.findings[f["code"]]
        for lang in LANGUAGES:
            for words in (text.title[lang], text.action[lang]):
                assert set(PLACEHOLDER.findall(words)) <= set(f["params"])
