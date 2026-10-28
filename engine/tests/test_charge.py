"""The battery failsafe's charge against the measured way home: the trip's charge against
its worked parts, ArduPilot's simulator flying Callisto's speed card (the failsafe off at
0 mAh: it is set to the way home and the reserve), shared between parallel packs, a way
home past what the packs can keep back, and the distance itself kept out of the findings."""

from __future__ import annotations

from typing import Any

import pytest
from test_perf_report import make_fleet
from test_perf_store import PACK

from flight_insights.analyzers import Registry
from flight_insights.analyzers.registry import LANGUAGES, PLACEHOLDER
from flight_insights.analyzers.run import run_analysis
from flight_insights.history.persist import load_snapshot
from flight_insights.performance import advice
from flight_insights.performance.charge import LOW, SHORT, charge_findings, farthest, return_mah
from flight_insights.recommend import flight_plan

REG = Registry.load(PACK)


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return make_fleet(tmp_path_factory)


@pytest.fixture(scope="module")
def light(fleet: dict[str, Any]) -> tuple[advice.Context, dict[str, float], dict[str, Any]]:
    store, cache = fleet["store"], fleet["cache"]
    [flight] = [r[0] for r in store.conn.execute(
        "SELECT id FROM flight WHERE airframe_id = ? AND takeoff_kg = 32.5",
        (fleet["airframe"],))]  # fmt: skip
    analysis = run_analysis(store, cache, PACK, flight)
    snap = load_snapshot(store, store.get("flight", flight)["param_snapshot_id"])
    params = dict(snap.dialect(PACK).to_canonical(snap.values).values)
    ctx = advice.context(store, cache, PACK, REG, flight, analysis)
    assert ctx is not None
    return ctx, params, analysis


def test_the_way_home_is_its_climb_its_flight_back_and_its_descent(light: Any) -> None:
    ctx, params, _ = light
    p = params | {"RTL_SPEED_MS": 0.0, "WP_SPD": 10.0, "RTL_ALT_M": 30.0, "WP_SPD_UP": 2.0,
                  "WP_SPD_DN": 1.0, "BATT_LOW_VOLT": 42.0}  # fmt: skip

    def power(v: float, vz: float) -> float:
        return float(ctx.model.power(ctx.weight, v, vz, ctx.density, ctx.rules)[()])

    got = return_mah(ctx, p, 2000.0, 3.0)
    assert got is not None
    mah, air = got
    # RTL_SPEED_MS 0 flies at WP_SPD: 10 over the ground, 13 through the air into 3
    assert air == 13.0
    joules = power(0, 2.0) * 30 / 2.0 + power(13.0, 0) * 2000 / 10 + power(0, 0) * 30 / 1.0
    assert mah == pytest.approx(joules / 42.0 / 3.6)  # at the failsafe's voltage
    # farther, or a stronger headwind, takes more
    more = return_mah(ctx, p, 3000.0, 3.0)
    windier = return_mah(ctx, p, 2000.0, 6.0)
    assert more is not None and windier is not None and more[0] > mah and windier[0] > mah
    assert return_mah(ctx, p | {"WP_SPD": 0.0}, 2000.0, 3.0) is None


def test_on_the_simulator_the_failsafe_off_is_set_to_the_way_home(
    fleet: dict[str, Any], light: Any
) -> None:
    ctx, params, analysis = light
    store = fleet["store"]
    assert params["BATT_LOW_MAH"] == 0.0 and params["BATT_CAPACITY"] == 44000.0
    [f] = charge_findings(ctx, store, REG, params, analysis)
    assert f["code"] == LOW and f["severity"] == "safety" and f["confidence"] == "low"
    assert f["evidence"] == ["energy:flight", "no_gnss:range"]
    p = f["params"]
    need = p["return_mah"] + 0.2 * 44000
    assert need <= p["proposed_mah"] < need + 100 and p["proposed_mah"] % 100 == 0
    assert f["changes"] == [{"param": "BATT_LOW_MAH", "proposed": p["proposed_mah"],
                             "stage": "operations"}]  # fmt: skip
    # the distance stays on the laptop: nothing in the finding is it
    far = farthest(store, fleet["airframe"])
    assert far and not any(isinstance(v, float | int) and any(abs(v - d) < 1 for d in far)
                           for v in p.values())  # fmt: skip
    assert not any("distance" in k or k.endswith("_m") for k in p)
    for lang in LANGUAGES:
        text = REG.findings[LOW]
        for words in (text.title[lang], text.action[lang]):
            assert set(PLACEHOLDER.findall(words)) <= set(p)
    # kept back already: nothing; never lowered
    assert charge_findings(ctx, store, REG, params | {"BATT_LOW_MAH": 20000.0}, analysis) == []


def test_parallel_packs_share_the_way_home_by_capacity(fleet: dict[str, Any], light: Any) -> None:
    ctx, params, analysis = light
    two = params | {"BATT_CAPACITY": 30000.0, "BATT2_MONITOR": 4.0, "BATT2_CAPACITY": 10000.0,
                    "BATT2_LOW_MAH": 0.0}  # fmt: skip
    found = {f["params"]["param"]: f for f in charge_findings(ctx, fleet["store"], REG, two,
                                                              analysis)}  # fmt: skip
    assert set(found) == {"BATT_LOW_MAH", "BATT2_LOW_MAH"}
    first, second = found["BATT_LOW_MAH"]["params"], found["BATT2_LOW_MAH"]["params"]
    assert first["pack_return_mah"] + second["pack_return_mah"] == pytest.approx(
        first["return_mah"], abs=1
    )
    assert first["pack_return_mah"] == pytest.approx(3 * second["pack_return_mah"], abs=2)


def test_a_way_home_past_what_the_packs_can_keep_back(fleet: dict[str, Any], light: Any) -> None:
    ctx, params, analysis = light
    small = params | {"BATT_CAPACITY": 2500.0}
    [f] = charge_findings(ctx, fleet["store"], REG, small, analysis)
    assert f["code"] == SHORT and f["changes"] == [] and f["severity"] == "safety"
    assert f["params"]["share_pct"] > 60
    for lang in LANGUAGES:
        text = REG.findings[SHORT]
        for words in (text.title[lang], text.action[lang]):
            assert set(PLACEHOLDER.findall(words)) <= set(f["params"])


def test_the_plan_offers_it_at_any_stage(fleet: dict[str, Any], light: Any) -> None:
    ctx, _, analysis = light
    plan = flight_plan(fleet["store"], fleet["cache"], PACK, ctx.flight_id, analysis)
    [rec] = [r for r in plan.recommendations if r.param == "BATT_LOW_MAH"]
    assert rec.status != "blocked" and rec.proposed is not None and rec.proposed >= 8800
    assert plan.stage != "operations"  # a safety finding is on offer whatever the stage
