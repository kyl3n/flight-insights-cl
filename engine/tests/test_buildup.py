"""The speed build-up as plan items, when the setup's speed goal is "fastest safe": the
next step on ArduPilot's simulator flying Callisto's speed card (12 m/s verified, 13 next,
every margin holding), each limit binding alone, each prediction missing, where to start,
the cruise advice yielding to it, the plan letting this one raise through, and the checks
the next flight is graded by, though no analyzer made the step's finding."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_perf_report import SETUP
from test_perf_store import PACK, airframe, flown

from flight_insights.analyzers import Registry
from flight_insights.analyzers.registry import LANGUAGES, PLACEHOLDER
from flight_insights.analyzers.run import run_analysis
from flight_insights.history import save_snapshot
from flight_insights.history.persist import load_snapshot
from flight_insights.params import Dialect
from flight_insights.performance import advice
from flight_insights.performance.buildup import (
    LEAN,
    LIMITED,
    MISSING,
    NOT_READY,
    START,
    STEP,
    buildup,
    gate,
)
from flight_insights.recommend import build_plan, grade_flight, history, record_plan, to_json
from flight_insights.recommend.plan import (
    REVERT,
    _never,  # pyright: ignore[reportPrivateUsage]
    needed_again,
    plan_keeping_tests,
)
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
    # the step's test-flight logging is graded on the WP_SPD flown, not its own value
    flown_at = reached | {"param": "WP_SPD"}
    assert _check(flown_at, False, flown_fast, {"WP_SPD": 12.9}, None, 180223.0) is True
    assert _check(flown_at, False, flown_fast, {"WP_SPD": 13.5}, None, 180223.0) is False
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


def test_a_lean_limit_just_short_rises_by_its_step_with_the_step(light: Any) -> None:
    ctx, params = light
    base = params | {"WP_SPD": 10.0}
    [step] = buildup(ctx, base, REG).findings
    p = step["params"]
    lean_deg = p["thrust_value"]  # the lean the step needs, degrees
    assert p["thrust_limit"] > lean_deg + 4  # the thrust allows more than 2 degrees past it
    # a lean limit a degree under the one whose 80% the step's lean needs
    short = round(lean_deg / 0.8) - 1.0
    got = buildup(ctx, base | {"PSC_ANGLE_MAX": short}, REG)
    by = {f["code"]: f for f in got.findings}
    assert set(by) == {STEP, LEAN}
    lean = by[LEAN]
    assert lean["changes"] == [{"param": "PSC_ANGLE_MAX", "proposed": short + 2.0,
                                "stage": "navigation"}]  # fmt: skip
    assert lean["params"]["from_deg"] == short and lean["params"]["to_deg"] <= p["thrust_limit"]
    assert got.raise_limits == {"WP_SPD": by[STEP]["params"]["proposed_ms"],
                                "PSC_ANGLE_MAX": short + 2.0}  # fmt: skip
    texts_filled(lean)
    dialect = Dialect(REG.pack, "copter", "4.7.0")
    plan = build_plan("fl", got.findings, base | {"PSC_ANGLE_MAX": short}, dialect, REG,
                      got.raise_limits)  # fmt: skip
    [rec] = [r for r in plan.recommendations if r.param == "PSC_ANGLE_MAX"]
    assert rec.status != "blocked" and rec.proposed == short + 2.0
    held = build_plan("fl", got.findings, base | {"PSC_ANGLE_MAX": short}, dialect, REG)
    [rec] = [r for r in held.recommendations if r.param == "PSC_ANGLE_MAX"]
    assert rec.status == "blocked"  # never raised without the step's limit
    # far short: 2 degrees do not reach, so the lean binds as before
    far = buildup(ctx, base | {"PSC_ANGLE_MAX": short - 6.0}, REG).findings
    assert [f["code"] for f in far] == [LIMITED + "lean"]
    # held back while a finding that matters at speed is open: both, and both limits
    out, limits = gate([*got.findings, planted("gnss.jamming", "caution")], got.raise_limits,
                       REG)  # fmt: skip
    assert [f["code"] for f in out if f["code"].startswith("speed.")] == [NOT_READY]
    assert limits == {}


@pytest.mark.parametrize(("param", "current", "value", "raised"), [
    ("PSC_ANGLE_MAX", 0.0, 40.0, True),   # 0 flies at ATC_ANGLE_MAX's 30
    ("PSC_ANGLE_MAX", 0.0, 30.0, False),
    ("LOIT_ANG_MAX", 0.0, 25.0, True),    # 0 flies at 2/3 of 30
    ("LOIT_ANG_MAX", 0.0, 20.0, False),
    ("RTL_SPEED_MS", 0.0, 12.0, True),    # 0 flies at WP_SPD's 10
])  # fmt: skip
def test_a_zero_is_read_as_what_it_flies_at(param: str, current: float, value: float,
                                            raised: bool) -> None:  # fmt: skip
    params = {"ATC_ANGLE_MAX": 30.0, "PSC_ANGLE_MAX": 0.0, "LOIT_ANG_MAX": 0.0, "WP_SPD": 10.0,
              "RTL_SPEED_MS": 0.0, param: current}  # fmt: skip
    note = _never(REG.rules["recommend"], param, current, value, params)
    assert (note is not None and note.reason == "never_raise") == raised


def test_the_step_and_its_lean_raise_are_graded_by_their_checks(tmp_path: Path) -> None:
    """The plan makes the step's finding from the airframe's models, so no analyzer judges
    the next flight for it: its own checks grade it. The loaded card flown with the step
    and the lean raise holds 12 m/s, short of a 13 m/s step (no effect), while the lean
    raise's checks, the fast segment measured and in target, pass (confirmed)."""
    keys = StoreKeys.generate()
    store = open_store(tmp_path / "g.db", keys, "e" * 32)
    cache = ColumnCache(tmp_path / "cache", keys)
    af = airframe(store, "Callisto", SETUP | {"speed_goal": "fastest"})
    flight = flown(store, cache, "copter-heavylift-sweep", af, 32.5)
    later = flown(store, cache, "copter-heavylift-sweep-loaded", af, 40.5)
    ctx = advice.context(store, cache, PACK, REG, flight, run_analysis(store, cache, PACK, flight))
    assert ctx is not None
    row = store.get("flight", flight)
    assert row is not None
    snap = load_snapshot(store, str(row["param_snapshot_id"]))
    params = dict(snap.dialect(PACK).to_canonical(snap.values).values) | {"WP_SPD": 10.0}
    [step] = buildup(ctx, params, REG).findings
    params["PSC_ANGLE_MAX"] = round(step["params"]["thrust_value"] / 0.8) - 1.0  # just short
    got = buildup(ctx, params, REG)
    dialect = Dialect(REG.pack, "copter", "4.7.0")
    plan = build_plan(flight, got.findings, params, dialect, REG, got.raise_limits)
    record_plan(store, to_json(plan, REG))
    proposed = {r.native: r.native_value for r in plan.offered()}
    assert set(proposed) == {"WP_SPD", "PSC_ANGLE_MAX"}
    # the later flight, as if flown with both
    later_row = store.get("flight", later)
    assert later_row is not None
    old = load_snapshot(store, str(later_row["param_snapshot_id"]))
    new = save_snapshot(store, replace(old, values=old.values | proposed), PACK)
    store.update("flight", later, later_row["version"], {"param_snapshot_id": new})
    graded = grade_flight(store, PACK, later, run_analysis(store, cache, PACK, later))
    status = {g["param"]: g["status"] for g in graded}
    assert status == {"WP_SPD": "no effect", "PSC_ANGLE_MAX": "confirmed"}
    rows = {h["param"]: h for h in history(store, af)}
    checks = {c["check"]: c["passed"] for c in rows["WP_SPD"]["detail"]["grade"]["checks"]}
    assert checks == {"has_metric": True, "reaches_proposed": False, "in_target": True}


def test_the_steps_test_logging_stays_on_between_steps(light: Any) -> None:
    """The step's test-flight logging is not taken out while the next step is on offer and
    would set it straight back: no revert, then the same bit again, between steps. A bit
    the step does not log comes out at once, and with no step on offer it all comes out."""
    ctx, params = light
    on = int(params["LOG_BITMASK"]) | 1 << 5  # as the last step's test flight left it
    base = params | {"WP_SPD": 10.0, "LOG_BITMASK": float(on)}

    def revert(bit: int) -> dict[str, Any]:
        return {"id": f"followup:r{bit}", "analyzer": "followup", "code": REVERT,
                "severity": "info", "confidence": "high",
                "params": {"param": "LOG_BITMASK", "recommendation": "r", "flight": "f0"},
                "evidence": [], "stage": "evidence", "origin": "measured",
                "changes": [{"param": "LOG_BITMASK", "proposed": float(on & ~(1 << bit)),
                             "stage": "evidence"}]}  # fmt: skip

    got = buildup(ctx, base, REG)
    dialect = Dialect(REG.pack, "copter", "4.7.0")
    assert needed_again([*got.findings, revert(5)], REG) == {"followup:r5": STEP}
    plan, held = plan_keeping_tests("fl", [*got.findings, revert(5)], base, dialect, REG,
                                    got.raise_limits)  # fmt: skip
    assert [r.param for r in plan.offered()] == ["WP_SPD"]  # bit 5 on already: nothing to set
    assert held == [{"code": REVERT, "flight": "f0", "param": "LOG_BITMASK", "by": STEP,
                     "reason": "needed_again"}]  # fmt: skip
    # bit 3 (PM) is none of the step's: its revert comes with the step
    assert needed_again([*got.findings, revert(3)], REG) == {}
    # no step on offer (held back, not ready): nothing sets it again, so it comes out
    out, limits = gate([*got.findings, planted("gnss.jamming", "caution")], got.raise_limits,
                       REG)  # fmt: skip
    plan, held = plan_keeping_tests("fl", [*out, revert(5)], base, dialect, REG, limits)
    assert held == [] and any(r.lead.code == REVERT for r in plan.offered())
