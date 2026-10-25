"""Closing the loop: recommendations kept once per airframe, a person's decisions, and
the next flight's grade (confirmed, no effect, regressed, or not judged)."""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from flight_insights.analyzers import run_analysis
from flight_insights.history import load_snapshot, save_snapshot
from flight_insights.imports import Decision, commit, propose, scan
from flight_insights.knowledge import Pack, load_pack
from flight_insights.platforms.records import create_platform, platform_view, save_answers
from flight_insights.platforms.schema import load_schema
from flight_insights.recommend import (
    RecommendationConflict,
    RecommendationError,
    flight_plan,
    grade_flight,
    history,
    plan_and_record,
    record_plan,
    set_status,
)
from flight_insights.recommend.records import _when  # pyright: ignore[reportPrivateUsage]
from flight_insights.store import Store, StoreKeys, open_store
from flight_insights.store.cache import ColumnCache

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"


@dataclasses.dataclass
class Fleet:
    store: Store
    cache: ColumnCache
    pack: Pack
    earlier: str
    later: str
    airframe: str


@pytest.fixture
def fleet(tmp_path: Path) -> Iterator[Fleet]:
    """The heavy lift's two fixture flights, imported as one airframe."""
    pack = load_pack()
    keys = StoreKeys.generate()
    with open_store(tmp_path / "fleet.db", keys, "e" * 32) as store:
        cache = ColumnCache(tmp_path / "cache", keys)
        schema = load_schema(pack)
        platform = create_platform(store, schema, "HL", "copter")
        version = platform_view(store, schema, platform)["reference_build"]["version"]
        answers = {"payload": "variable", "esc_firmware": "BLHeli_32"}
        save_answers(store, schema, platform, version, answers)
        flights: list[dict[str, Any]] = []
        for name in ("copter-heavylift-loaded.bin", "copter-heavylift-light.bin"):
            batch = scan(store, pack, bytes(32), [FIXTURES / name])
            decision = (
                Decision("f1", airframe_id=str(flights[0]["airframe_id"]))
                if flights
                else Decision("f1", new_airframe=(platform, "HL1"))
            )
            [res] = commit(store, pack, cache, batch, propose(store, pack, batch), [decision])
            row = store.get("flight", res.flights[0])
            assert row is not None
            flights.append(row)
        a, b = sorted(flights, key=_when)
        yield Fleet(store, cache, pack, a["id"], b["id"], str(a["airframe_id"]))


def fly_with(f: Fleet, flight: str, **values: float) -> None:
    """The flight as if flown with these parameter values."""
    row = f.store.get("flight", flight)
    assert row is not None
    snap = load_snapshot(f.store, row["param_snapshot_id"])
    new = save_snapshot(f.store, dataclasses.replace(snap, values=snap.values | values), f.pack)
    f.store.update("flight", flight, row["version"], {"param_snapshot_id": new})


def rec(plan: dict[str, Any], param: str) -> dict[str, Any]:
    return next(r for r in plan["recommendations"] if r["canonical"] == param)


def later_analysis(f: Fleet, **raised: str) -> dict[str, Any]:
    """The later flight's real analysis, with chosen findings raised at chosen severities
    (code with dots as double underscores)."""
    out = run_analysis(f.store, f.cache, f.pack, f.later, force=True)
    out["findings"] = [x for x in out["findings"] if x["code"] not in raised] + [
        {"code": code.replace("__", "."), "severity": sev, "analyzer": "options"}
        for code, sev in raised.items()
    ]
    return out


def test_a_plan_is_kept_once_per_airframe(fleet: Fleet) -> None:
    first = plan_and_record(fleet.store, fleet.cache, fleet.pack, fleet.earlier)
    again = plan_and_record(fleet.store, fleet.cache, fleet.pack, fleet.earlier)
    ids = [r["id"] for r in first["recommendations"] if r["status"] != "blocked"]
    assert ids and ids == [r["id"] for r in again["recommendations"] if r["status"] != "blocked"]
    assert len(history(fleet.store, fleet.airframe)) == len(ids)
    stored = {h["param"]: h for h in history(fleet.store, fleet.airframe)}
    assert stored["MOT_HOVER_LEARN"]["proposed_value"] == 2.0
    assert stored["MOT_HOVER_LEARN"]["detail"]["why"]["en"]  # what the person saw is kept


def test_a_person_decides_and_grades_are_the_engines(fleet: Fleet) -> None:
    plan = plan_and_record(fleet.store, fleet.cache, fleet.pack, fleet.earlier)
    rid = rec(plan, "MOT_HOVER_LEARN")["id"]
    assert set_status(fleet.store, rid, "accepted", 1) == {
        "id": rid, "status": "accepted", "version": 2
    }  # fmt: skip
    with pytest.raises(RecommendationConflict):
        set_status(fleet.store, rid, "applied", 1)  # read before the change above
    with pytest.raises(RecommendationError):
        set_status(fleet.store, rid, "confirmed", 2)


def test_a_change_flown_and_cleared_is_confirmed(fleet: Fleet) -> None:
    plan_and_record(fleet.store, fleet.cache, fleet.pack, fleet.earlier)
    fly_with(fleet, fleet.later, MOT_HOVER_LEARN=2.0)
    graded = grade_flight(fleet.store, fleet.pack, fleet.later, later_analysis(fleet))
    assert {"param": "MOT_HOVER_LEARN", "status": "confirmed"} in [
        {k: g[k] for k in ("param", "status")} for g in graded
    ]
    [row] = [h for h in history(fleet.store, fleet.airframe) if h["param"] == "MOT_HOVER_LEARN"]
    assert row["graded_flight_id"] == fleet.later
    assert row["detail"]["grade"]["after"] == {}


@pytest.mark.parametrize(("severity", "grade"), [("info", "no effect"), ("out", "regressed")])
def test_a_finding_still_raised_is_no_effect_or_regressed(
    fleet: Fleet, severity: str, grade: str
) -> None:
    plan_and_record(fleet.store, fleet.cache, fleet.pack, fleet.earlier)
    fly_with(fleet, fleet.later, MOT_HOVER_LEARN=2.0)
    analysis = later_analysis(fleet, option__hover_learn__available=severity)
    graded = grade_flight(fleet.store, fleet.pack, fleet.later, analysis)
    assert next(g for g in graded if g["param"] == "MOT_HOVER_LEARN")["status"] == grade


def test_a_change_not_flown_is_not_graded(fleet: Fleet) -> None:
    plan_and_record(fleet.store, fleet.cache, fleet.pack, fleet.earlier)
    assert grade_flight(fleet.store, fleet.pack, fleet.later, later_analysis(fleet)) == []


def test_a_log_that_cannot_judge_leaves_it_applied(fleet: Fleet) -> None:
    plan_and_record(fleet.store, fleet.cache, fleet.pack, fleet.earlier)
    fly_with(fleet, fleet.later, MOT_HOVER_LEARN=2.0)
    analysis = later_analysis(fleet)
    analysis["analyzers"]["options"]["skipped"] = "no catalog"
    [g] = [
        g
        for g in grade_flight(fleet.store, fleet.pack, fleet.later, analysis)
        if g["param"] == "MOT_HOVER_LEARN"
    ]
    assert g["status"] == "applied"


def test_an_earlier_flight_never_grades_a_later_plan(fleet: Fleet) -> None:
    plan_and_record(fleet.store, fleet.cache, fleet.pack, fleet.later)
    fly_with(fleet, fleet.earlier, MOT_HOVER_LEARN=2.0)
    analysis = run_analysis(fleet.store, fleet.cache, fleet.pack, fleet.earlier, force=True)
    assert grade_flight(fleet.store, fleet.pack, fleet.earlier, analysis) == []


def test_a_new_value_supersedes_an_untouched_proposal(fleet: Fleet) -> None:
    plan = plan_and_record(fleet.store, fleet.cache, fleet.pack, fleet.earlier)
    learn, logging = rec(plan, "MOT_HOVER_LEARN"), rec(plan, "LOG_FILE_DSRMROT")
    set_status(fleet.store, logging["id"], "accepted", 1)
    later: dict[str, Any] = copy.deepcopy(plan) | {"flight": fleet.later}
    recs: list[dict[str, Any]] = later["recommendations"]
    for r in recs:  # a later plan wants other values for both
        if r["canonical"] in ("MOT_HOVER_LEARN", "LOG_FILE_DSRMROT"):
            r["proposed_canonical"] = 1.0 if r["canonical"] == "MOT_HOVER_LEARN" else 0.5
    record_plan(fleet.store, later)
    rows = {(h["param"], h["status"]) for h in history(fleet.store, fleet.airframe)}
    assert ("MOT_HOVER_LEARN", "superseded") in rows and ("MOT_HOVER_LEARN", "proposed") in rows
    assert ("LOG_FILE_DSRMROT", "accepted") in rows  # a person's decision is never overridden
    old = fleet.store.get("recommendation", learn["id"])
    assert old is not None and '"superseded_by_flight"' in old["detail_json"]


# --- procedures on the next flight ---------------------------------------------------------

NOTCH = {  # the earlier flight's plan: its notch onto ESC RPM
    "INS_HNTCH_BW": 70.0, "INS_HNTCH_FREQ": 140.0, "INS_HNTCH_MODE": 3.0,
    "INS_HNTCH_OPTS": 32.0, "INS_HNTCH_REF": 1.0,
}  # fmt: skip
TEST_LOGGING = 180222 | 1 << 19 | 1 << 21  # raw IMU and fast notch logging, for the test flight


def without(f: Fleet, *codes: str) -> dict[str, Any]:
    """The later flight's real analysis, with these findings cleared."""
    out = run_analysis(f.store, f.cache, f.pack, f.later, force=True)
    out["findings"] = [x for x in out["findings"] if x["code"] not in codes]
    return out


def messages(f: Fleet) -> list[str]:
    row = f.store.get("flight", f.later)
    assert row is not None
    return f.cache.names(row["flight_key"])


def statuses(f: Fleet) -> dict[str, str]:
    return {h["param"]: h["status"] for h in history(f.store, f.airframe) if h["param"]}


def test_a_confirmed_filter_test_brings_the_revert_of_its_logging(fleet: Fleet) -> None:
    earlier = plan_and_record(fleet.store, fleet.cache, fleet.pack, fleet.earlier)
    assert rec(earlier, "LOG_BITMASK")["temporary"]
    fly_with(fleet, fleet.later, **NOTCH, LOG_BITMASK=float(TEST_LOGGING))
    analysis = without(fleet, "filter.better_source", "logging.notch_inputs_not_logged")
    grade_flight(fleet.store, fleet.pack, fleet.later, analysis, messages(fleet))
    assert statuses(fleet)["INS_HNTCH_MODE"] == statuses(fleet)["LOG_BITMASK"] == "confirmed"
    [mode] = [h for h in history(fleet.store, fleet.airframe) if h["param"] == "INS_HNTCH_MODE"]
    assert {"check": "has_message", "message": "GYR", "passed": True} in mode["detail"]["grade"][
        "checks"
    ]
    plan = flight_plan(fleet.store, fleet.cache, fleet.pack, fleet.later, analysis)
    [revert] = [r for r in plan.recommendations if r.param == "LOG_BITMASK"]
    assert revert.lead.code == "followup.revert_test_settings"
    assert revert.proposed == 180222.0  # only the test flight's bits come out


def test_the_revert_follows_when_a_later_plan_replaced_the_lasting_changes(
    fleet: Fleet,
) -> None:
    # A plan waits on an earlier stage; the next plan, after another flight, proposes the
    # same test-flight logging (it keeps its first record) but other notch values (new
    # records, the old ones superseded). The test flight confirms the new ones.
    earlier = plan_and_record(fleet.store, fleet.cache, fleet.pack, fleet.earlier)
    again: dict[str, Any] = copy.deepcopy(earlier)
    for r in again["recommendations"]:
        if r["canonical"] in NOTCH and r["canonical"] != "INS_HNTCH_MODE":
            r["proposed_canonical"] = float(r["proposed_canonical"]) + 5.0
    record_plan(fleet.store, again)
    replaced = {
        h["param"]: h["proposed_value"]
        for h in history(fleet.store, fleet.airframe)
        if h["param"] in NOTCH and h["status"] == "proposed"
    }
    assert set(replaced) == set(NOTCH)
    fly_with(fleet, fleet.later, **replaced, LOG_BITMASK=float(TEST_LOGGING))
    analysis = without(fleet, "filter.better_source", "logging.notch_inputs_not_logged")
    grade_flight(fleet.store, fleet.pack, fleet.later, analysis, messages(fleet))
    assert statuses(fleet)["LOG_BITMASK"] == "confirmed"
    plan = flight_plan(fleet.store, fleet.cache, fleet.pack, fleet.later, analysis)
    [revert] = [r for r in plan.recommendations if r.param == "LOG_BITMASK"]
    assert revert.lead.code == "followup.revert_test_settings"


def test_a_check_that_fails_makes_a_cleared_finding_no_effect(fleet: Fleet) -> None:
    plan_and_record(fleet.store, fleet.cache, fleet.pack, fleet.earlier)
    fly_with(fleet, fleet.later, **NOTCH, LOG_BITMASK=float(TEST_LOGGING))
    analysis = without(fleet, "filter.better_source", "logging.notch_inputs_not_logged")
    grade_flight(fleet.store, fleet.pack, fleet.later, analysis, messages=[])  # no GYR logged
    assert statuses(fleet)["INS_HNTCH_MODE"] == "no effect"
    plan = flight_plan(fleet.store, fleet.cache, fleet.pack, fleet.later, analysis)
    assert not [r for r in plan.recommendations if r.lead.code == "followup.revert_test_settings"]


def test_a_filter_diagnostic_is_not_proposed_again_while_nothing_changed(fleet: Fleet) -> None:
    run_analysis(fleet.store, fleet.cache, fleet.pack, fleet.earlier)  # checked with raw gyro
    analysis = run_analysis(fleet.store, fleet.cache, fleet.pack, fleet.later)
    analysis["findings"] = [
        *analysis["findings"],
        {
            "id": "diag", "analyzer": "logging", "code": "logging.no_prefilter_gyro",
            "severity": "caution", "confidence": "high", "params": {"proposed": 9},
            "evidence": ["log:gyro"], "stage": "evidence",
            "changes": [{"param": "INS_RAW_LOG_OPT", "current": 0, "proposed": 9.0,
                         "stage": "evidence"}],
        },
    ]  # fmt: skip
    plan = flight_plan(fleet.store, fleet.cache, fleet.pack, fleet.later, analysis)
    assert {"code": "logging.no_prefilter_gyro", "flight": fleet.earlier} in plan.held_back

    def proposed(plan: Any) -> bool:
        return any(
            s.code == "logging.no_prefilter_gyro" for r in plan.recommendations for s in r.sources
        )

    assert not proposed(plan)
    fly_with(fleet, fleet.later, INS_HNTCH_BW=60.0)  # a filter setting changed since
    plan = flight_plan(fleet.store, fleet.cache, fleet.pack, fleet.later, analysis)
    assert not plan.held_back and proposed(plan)
