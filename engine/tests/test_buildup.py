"""The speed build-up as plan items, when the setup's speed goal is "fastest safe": the
next step on ArduPilot's simulator flying Callisto's speed card (12 m/s verified, 13 next,
every margin holding), each limit binding alone, each prediction missing, where to start,
the cruise advice yielding to it, the plan letting this one raise through, and the checks
the next flight is graded by."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from test_perf_report import SETUP
from test_perf_store import PACK, airframe, flown

from flight_insights.analyzers import Registry
from flight_insights.analyzers.registry import LANGUAGES, PLACEHOLDER
from flight_insights.analyzers.run import run_analysis
from flight_insights.history.persist import load_snapshot
from flight_insights.params import Dialect
from flight_insights.performance import advice
from flight_insights.performance.buildup import (
    LIMITED,
    MISSING,
    NOT_READY,
    START,
    STEP,
    buildup,
    gate,
)
from flight_insights.recommend import build_plan
from flight_insights.recommend.records import _check  # pyright: ignore[reportPrivateUsage]
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

REG = Registry.load(PACK)


@pytest.fixture(scope="module")
def light(tmp_path_factory: pytest.TempPathFactory) -> tuple[advice.Context, dict[str, float]]:
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("buildup")
    store = open_store(folder / "b.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    af = airframe(store, "Callisto", SETUP | {"speed_goal": "fastest"})
    flight = flown(store, cache, "copter-heavylift-sweep", af, 32.5)
    flown(store, cache, "copter-heavylift-sweep-loaded", af, 40.5)
    analysis = run_analysis(store, cache, PACK, flight)
    row = store.get("flight", flight)
    assert row is not None
    snap = load_snapshot(store, str(row["param_snapshot_id"]))
    params = dict(snap.dialect(PACK).to_canonical(snap.values).values)
    ctx = advice.context(store, cache, PACK, REG, flight, analysis)
    assert ctx is not None
    return ctx, params


def texts_filled(f: dict[str, Any]) -> None:
    text = REG.findings[f["code"]]
    for lang in LANGUAGES:
        for words in (text.title[lang], text.action[lang]):
            assert set(PLACEHOLDER.findall(words)) <= set(f["params"]), (f["code"], words)


def test_the_next_step_is_offered_with_its_margins(light: Any) -> None:
    ctx, params = light
    got = buildup(ctx, params | {"WP_SPD": 10.0}, REG)
    [f] = got.findings
    assert f["code"] == STEP and f["stage"] == "navigation" and f["confidence"] == "low"
    p = f["params"]
    assert p["verified_ms"] == pytest.approx(12.0, abs=0.3)
    assert p["next_ms"] == pytest.approx(p["verified_ms"] + 1.0, abs=0.1)
    assert p["proposed_ms"] <= p["next_ms"] and p["proposed_ms"] * 10 == int(p["proposed_ms"] * 10)
    for name in ("lean", "thrust", "current"):
        assert p[f"{name}_value"] < p[f"{name}_limit"]
    assert f["changes"] == [{"param": "WP_SPD", "proposed": p["proposed_ms"],
                             "stage": "navigation"}]  # fmt: skip
    assert got.raise_limits == {"WP_SPD": p["proposed_ms"]}
    texts_filled(f)
    # set faster than the step already: lowered to it, so the next flight flies the step
    [faster] = buildup(ctx, params | {"WP_SPD": 15.0}, REG).findings
    assert faster["changes"][0]["proposed"] == p["proposed_ms"]
    # set to it: nothing to change, the next flight verifies it
    assert buildup(ctx, params | {"WP_SPD": p["proposed_ms"]}, REG).findings == []


def test_only_with_the_fastest_goal(light: Any) -> None:
    ctx, params = light
    answers = {**ctx.inputs.answers, "speed_goal": "efficient"}
    efficient = replace(ctx, inputs=replace(ctx.inputs, answers=answers))
    assert buildup(efficient, params, REG).findings == []
    # with the fastest goal the cruise speed is the build-up's: no Priority advice on WP_SPD
    slow = params | {"WP_SPD": 6.0, "RTL_SPEED_MS": 0.0}
    codes = {f["code"] for f in advice.speeds(ctx, slow).findings}
    assert "performance.cruise_speed" not in codes and "performance.cruise_unverified" not in codes
    assert "performance.cruise_speed" in {
        f["code"] for f in advice.speeds(efficient, slow).findings
    }


@pytest.mark.parametrize(("binds", "planted"), [
    ("current", {"MOT_BAT_CURR_MAX": 88.0}),  # 89.6 A at the step, 87.1 A at 12 m/s
    ("lean", {"PSC_ANGLE_MAX": 3.0}),
])  # fmt: skip
def test_a_margin_failing_binds(light: Any, binds: str, planted: dict[str, float]) -> None:
    ctx, params = light
    [f] = buildup(ctx, params | planted, REG).findings
    assert f["code"] == LIMITED + binds and f["changes"] == []
    p = f["params"]
    assert p["value"] > p["limit"]
    # what the limit allows now falls short of the step; each solution moves it on
    assert 0 < p["allows_ms"] < p["next_ms"]
    gained = {k: v for k, v in p.items() if k.endswith("_gain_ms")}
    assert gained and all(v > 0 for v in gained.values())
    texts_filled(f)


def test_each_missing_prediction_says_what_to_get(light: Any) -> None:
    ctx, params = light
    i = ctx.inputs
    assert i.basis is not None
    cases = {
        "lean": replace(i, basis=None),
        "current": replace(i, pack=None),
        "thrust": replace(i, basis=replace(i.basis, throttle=None)),
    }
    for what, inputs in cases.items():
        [f] = buildup(replace(ctx, inputs=inputs), params, REG).findings
        assert f["code"] == MISSING + what and f["changes"] == []
        texts_filled(f)
    [start] = buildup(replace(ctx, inputs=replace(i, fast=())), params, REG).findings
    assert start["code"] == START
    texts_filled(start)


def test_the_plan_lets_the_one_step_through(light: Any) -> None:
    ctx, params = light
    got = buildup(ctx, params | {"WP_SPD": 10.0}, REG)
    dialect = Dialect(REG.pack, "copter", "4.7.0")
    plan = build_plan("fl", got.findings, params | {"WP_SPD": 10.0}, dialect, REG,
                      got.raise_limits)  # fmt: skip
    [rec] = [r for r in plan.recommendations if r.param == "WP_SPD"]
    assert rec.status != "blocked" and rec.proposed == got.raise_limits["WP_SPD"]
    blocked = build_plan("fl", got.findings, params | {"WP_SPD": 10.0}, dialect, REG)
    [rec] = [r for r in blocked.recommendations if r.param == "WP_SPD"]
    assert rec.status == "blocked"  # without the step's limit, never raised
    proc = REG.procedures[STEP]
    kinds = [s["kind"] for s in proc["steps"]]
    assert {"ground_check", "fly", "abort", "verify"} <= set(kinds)
    assert proc["add_temporary"][0]["set_bits"] == [1, 4, 5, 7, 9, 10, 17]


def test_the_next_flight_is_graded_on_its_fast_segment() -> None:
    def metric(name: str, value: float, zone: str | None, segment: str = "fast") -> dict[str, Any]:
        return {"metric": name, "segment": segment, "value": value, "zone": zone}

    reached = {"check": "reaches_proposed", "metric": "fast.speed_ms", "share": 0.97}
    target = {"check": "in_target", "segment": "fast"}
    flown_fast = {"metrics": [metric("fast.speed_ms", 12.8, None),
                              metric("fast.vibe_p95", 12.0, "target"),
                              metric("vibe.p95", 40.0, "caution", "flight")]}  # fmt: skip
    assert _check(reached, False, flown_fast, {}, None, 12.9) is True  # 12.8 >= 0.97 x 12.9
    assert _check(reached, False, flown_fast, {}, None, 13.5) is False
    assert _check(reached, False, flown_fast, {}, None, None) is None
    assert _check(target, False, flown_fast, {}, None) is True  # the flight's own zones aside
    hot = {"metrics": [*flown_fast["metrics"], metric("fast.esc_temp_max_c", 95.0, "caution")]}
    assert _check(target, False, hot, {}, None) is False
    assert _check(target, False, {"metrics": []}, {}, None) is None
    assert _check(reached, False, {"metrics": []}, {}, None, 12.9) is None


def planted(code: str, severity: str) -> dict[str, Any]:
    return {"id": f"x:{code}", "analyzer": code.split(".")[0], "code": code,
            "severity": severity, "confidence": "high", "params": {}, "evidence": [],
            "changes": [], "stage": REG.findings[code].stage, "origin": "measured"}  # fmt: skip


AT_SPEED = ["gnss.jamming", "motor_out.short", "performance.pack_worn",
            "performance.capacity_setting", "esc.hot", "vibration.high"]  # fmt: skip


@pytest.mark.parametrize("code", AT_SPEED)
def test_the_step_waits_while_a_finding_that_matters_at_speed_is_open(
    light: Any, code: str
) -> None:
    ctx, params = light
    got = buildup(ctx, params | {"WP_SPD": 10.0}, REG)
    findings = [*got.findings, planted(code, "caution"), planted("link.fade_margin", "caution")]
    out, limits = gate(findings, got.raise_limits, REG)
    [held] = [f for f in out if f["code"] == NOT_READY]
    assert not any(f["code"] == STEP for f in out) and held["changes"] == []
    assert held["params"]["blocking"] == code and held["params"]["count"] == 1
    assert held["severity"] == "caution" and "WP_SPD" not in limits
    texts_filled(held)
    # with it held, the plan raises nothing past the verified speed
    dialect = Dialect(REG.pack, "copter", "4.7.0")
    plan = build_plan("fl", out, params | {"WP_SPD": 10.0}, dialect, REG, limits)
    assert not [r for r in plan.offered() if r.param == "WP_SPD" and r.proposed and r.proposed > 10]


def test_an_info_finding_or_one_that_does_not_matter_at_speed_holds_nothing(light: Any) -> None:
    ctx, params = light
    got = buildup(ctx, params | {"WP_SPD": 10.0}, REG)
    findings = [*got.findings, planted("gnss.yaw_reset", "info"),
                planted("link.fade_margin", "caution")]  # fmt: skip
    out, limits = gate(findings, got.raise_limits, REG)
    assert [f["code"] for f in out if f["code"].startswith("speed.")] == [STEP]
    assert limits == got.raise_limits
    # nothing to hold without a step
    assert gate([planted("gnss.jamming", "caution")], {}, REG) == (
        [planted("gnss.jamming", "caution")], {})  # fmt: skip
