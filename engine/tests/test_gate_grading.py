"""The gate's grading (spec: Recommendation engine, "Closing the loop"): a tune check's
AutoTune offer, marked applied by the AutoTune flights that saved roll and pitch, then
graded on the next tune checks, light and loaded, each against its own payload band."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from flight_insights.analyzers import Registry
from flight_insights.analyzers.run import analysis_results
from flight_insights.history import save_snapshot
from flight_insights.history.snapshot import Snapshot
from flight_insights.knowledge import Pack, load_pack
from flight_insights.platforms.records import create_airframe, create_platform
from flight_insights.platforms.schema import load_schema
from flight_insights.recommend import flight_plan, grade_flight, history, record_plan, set_status
from flight_insights.recommend.measured import Measured, improves, versus_lightest
from flight_insights.recommend.plan import to_json
from flight_insights.store import Store, StoreKeys, open_store

T0 = 1_760_000_000_000_000
DAY = 86_400_000_000
LIGHT, LOADED = 30.0, 37.5  # hover thrust at standard air, %: 1.25 times, two bands apart
OFFER = "tune.autotune_offer"
PARAMS = {
    "ATC_RAT_RLL_P": 0.135, "ATC_RAT_RLL_D": 0.0036, "ATC_RAT_PIT_P": 0.135,
    "ATC_RAT_PIT_D": 0.0036, "ATC_RAT_RLL_FLTT": 20.0, "ATC_RAT_RLL_FLTD": 20.0,
    "ATC_RAT_PIT_FLTT": 20.0, "ATC_RAT_PIT_FLTD": 20.0, "AUTOTUNE_AXES": 7.0,
    "AUTOTUNE_GMBK": 0.25,
}  # fmt: skip


@dataclass
class Check:
    """A tune check's measurements on roll and pitch: attitude tracking error (%) with
    its 95% interval (the tune's measure), rate tracking error, rise time (s) and
    sensitivity peak."""

    roll: tuple[float, float, float]
    pitch: tuple[float, float, float]
    rise: tuple[float, float] | None = (0.10, 0.11)
    peak: tuple[float, float] | None = (1.5, 1.5)
    rate: tuple[float, float] = (26.0, 26.0)  # roll, pitch; intervals of +-0.5

    def metrics(self) -> dict[str, tuple[float, dict[str, float]]]:
        out: dict[str, tuple[float, dict[str, float]]] = {}
        for axis, (value, low, high) in (("roll", self.roll), ("pitch", self.pitch)):
            out[f"tune.attitude_error_{axis}"] = (value, {"low": low, "high": high, "events": 12})
        for axis, value in zip(("roll", "pitch"), self.rate, strict=True):
            spread = {"low": value - 0.5, "high": value + 0.5, "events": 12}
            out[f"tune.tracking_error_{axis}"] = (value, spread)
        for name, pair in (("rise_time", self.rise), ("sensitivity_peak", self.peak)):
            if pair is not None:
                out[f"tune.{name}_roll"], out[f"tune.{name}_pitch"] = (pair[0], {}), (pair[1], {})
        return out


@dataclass
class Gate:
    store: Store
    pack: Pack
    reg: Registry
    airframe: str
    snapshot: str
    n: int = 0

    def fly(
        self,
        thrust: float,
        check: Check | None = None,
        *,
        saved: str | None = None,
        offer: bool | None = None,
        changes: tuple[Mapping[str, Any], ...] = (),
        build: str | None = None,
        snapshot: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """The next flight and its analysis: a tune check (which offers AutoTune unless
        told not to), or an AutoTune flight that saved the named axes."""
        self.n += 1
        af = self.store.get("airframe", self.airframe)
        assert af is not None
        fid = self.store.insert(
            "flight",
            {"airframe_id": self.airframe, "firmware": "4.7.0", "boot_utc_us": T0 + self.n * DAY,
             "arm_us": 1_000_000, "param_snapshot_id": snapshot or self.snapshot,
             "build_version_id": build or af["current_build_id"]},
        )  # fmt: skip
        analyzers = {
            "payload": {"version": 1, "skipped": None},
            "tune": {"version": 2, "skipped": None if check else "no stick inputs"},
            "autotune": {"version": 1, "skipped": None if saved else "no AutoTune"},
        }
        run = self.store.insert(
            "analysis_run",
            {"flight_id": fid, "engine_version": "t", "knowledge_version": "t",
             "analyzers_json": json.dumps(analyzers), "status": "complete"},
        )  # fmt: skip
        values: dict[str, tuple[float, dict[str, float]]] = {"payload.hover_thrust": (thrust, {})}
        values |= check.metrics() if check else {}
        for metric, (value, spread) in values.items():
            segment = "hover" if metric.startswith("payload.") else "inputs"
            self.store.insert(
                "metric_value",
                {"run_id": run, "flight_id": fid, "metric": metric, "segment": segment,
                 "value": value, "spread_json": json.dumps(spread), "zone": "target"},
            )  # fmt: skip
        found: list[tuple[str, str, dict[str, Any], tuple[Mapping[str, Any], ...]]] = []
        if check is not None and offer is not False:
            found.append(("tune", OFFER, {"axes": "roll, pitch", "large_props": bool(changes)},
                          changes))  # fmt: skip
        if saved is not None:
            found.append(("autotune", "autotune.saved", {"axes": saved}, ()))
        for analyzer, code, params, ch in found:
            self.store.insert(
                "finding",
                {"run_id": run, "flight_id": fid, "analyzer": analyzer, "code": code,
                 "severity": "info", "confidence": "medium", "params_json": json.dumps(params),
                 "evidence_json": "[]", "changes_json": json.dumps(list(ch))},
            )  # fmt: skip
        analysis = analysis_results(self.store, fid, self.reg)
        assert analysis is not None
        return fid, analysis

    def plan(self, fid: str, analysis: Mapping[str, Any]) -> dict[str, Any]:
        """Grades earlier recommendations against the flight, then keeps its own plan."""
        grade_flight(self.store, self.pack, fid, analysis)
        plan = flight_plan(self.store, None, self.pack, fid, analysis)
        return record_plan(self.store, to_json(plan, self.reg))

    def offer(self) -> dict[str, Any]:
        [rec] = [
            r for r in history(self.store, self.airframe)
            if r["finding_code"] == OFFER and r["param"] is None
        ]  # fmt: skip
        return rec


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


@pytest.fixture
def gate(tmp_path: Path, reg: Registry) -> Iterator[Gate]:
    pack = load_pack()
    with open_store(tmp_path / "f.db", StoreKeys.generate(), "e" * 32) as store:
        platform = create_platform(store, load_schema(pack), "HL", "copter")
        airframe = create_airframe(store, platform, "HL1")
        snap = Snapshot(PARAMS, "copter", "4.7.0", "log start", "primary")
        yield Gate(store, pack, reg, airframe, save_snapshot(store, snap, pack))


BASE_LIGHT = Check(roll=(40.0, 37.0, 43.0), pitch=(42.0, 39.0, 45.0))
BASE_LOADED = Check(roll=(46.0, 43.0, 49.0), pitch=(48.0, 45.0, 51.0), rise=(0.14, 0.15))
AFTER_LIGHT = Check(roll=(28.0, 26.0, 30.0), pitch=(30.0, 28.0, 32.0), rise=(0.09, 0.10),
                    peak=(1.4, 1.45))  # fmt: skip
AFTER_LOADED = Check(roll=(34.0, 32.0, 36.0), pitch=(35.0, 33.0, 37.0), rise=(0.12, 0.13),
                     peak=(1.5, 1.55))  # fmt: skip


def baseline_and_autotune(gate: Gate) -> tuple[str, str]:
    """F1 light and F2 loaded tune checks, then AutoTune: F3 roll, F4 pitch."""
    gate.plan(*gate.fly(LIGHT, BASE_LIGHT))
    rec = gate.offer()
    assert rec["status"] == "proposed" and rec["detail"]["graded_by"] == "checks"
    assert rec["detail"]["applied_when"] == {"finding": "autotune.saved", "axes": ["roll", "pitch"]}
    gate.plan(*gate.fly(LOADED, BASE_LOADED))
    assert gate.offer()["id"] == rec["id"]  # offered again by the loaded check: the same one
    f3, a3 = gate.fly(LIGHT, saved="roll")
    assert grade_flight(gate.store, gate.pack, f3, a3) == []  # pitch still to tune
    assert gate.offer()["status"] == "proposed"
    assert gate.offer()["detail"]["applied_axes"] == ["roll"]
    f4, a4 = gate.fly(LIGHT, saved="pitch")
    assert grade_flight(gate.store, gate.pack, f4, a4) == [
        {"id": rec["id"], "param": None, "status": "applied"}
    ]
    applied = gate.offer()
    assert applied["status"] == "applied" and applied["detail"]["applied_flight"] == f4
    return f3, f4


def test_autotune_is_graded_on_both_payload_bands(gate: Gate) -> None:
    _, f4 = baseline_and_autotune(gate)
    f5, a5 = gate.fly(LIGHT, AFTER_LIGHT)
    [graded] = grade_flight(gate.store, gate.pack, f5, a5)
    assert graded["status"] == "applied"  # the loaded band has not been flown again
    pending = gate.offer()["detail"]["grade_pending"]
    roll = next(c for c in pending["checks"] if c["metric"] == "tune.attitude_error_roll")
    assert roll["passed"] is None
    assert [(b["group"], b["verdict"]) for b in roll["bands"]] == [
        ("band:0", "better"), ("band:2", "waiting")
    ]  # fmt: skip
    # The light check offers AutoTune again; it is held back, AutoTune was saved on this build.
    plan = to_json(flight_plan(gate.store, None, gate.pack, f5, a5), gate.reg)
    assert plan["held_back"] == [
        {"code": OFFER, "flight": f4, "reason": "held_once_done",
         "text": plan["held_back"][0]["text"]}
    ]  # fmt: skip
    assert "flight" in plan["held_back"][0]["text"]["en"]
    assert all(r["source"] != f"finding:{OFFER}" for r in plan["recommendations"])

    f6, a6 = gate.fly(LOADED, AFTER_LOADED)
    assert grade_flight(gate.store, gate.pack, f6, a6)[0]["status"] == "confirmed"
    rec = gate.offer()
    assert rec["status"] == "confirmed" and rec["graded_flight_id"] == f6
    assert "grade_pending" not in rec["detail"]
    checks = {c["metric"]: c for c in rec["detail"]["grade"]["checks"]}
    pitch = checks["tune.attitude_error_pitch"]
    assert [b["verdict"] for b in pitch["bands"]] == ["better", "better"]
    assert pitch["bands"][1]["change"] == pytest.approx((48 - 35) / 48, abs=1e-4)
    rise = checks["tune.rise_time_roll"]
    assert rise["passed"] and (rise["light"], rise["heavy"]) == (0.09, 0.12)
    assert checks["tune.tracking_error_roll"]["passed"]  # the rate loop no worse
    assert checks["tune.sensitivity_peak_roll"]["passed"]  # under 2 on the graded flight


def test_a_rate_loop_clearly_worse_is_regressed_whatever_the_attitude(gate: Gate) -> None:
    """The attitude may follow better with a rate loop that got worse (gains pushed into
    a slower, busier loop): the rate tracking error guards against it."""
    baseline_and_autotune(gate)
    gate.plan(*gate.fly(LIGHT, AFTER_LIGHT))
    worse_rate = Check(AFTER_LOADED.roll, AFTER_LOADED.pitch, rise=(0.12, 0.13),
                       peak=(1.5, 1.55), rate=(34.0, 26.0))  # fmt: skip
    f6, a6 = gate.fly(LOADED, worse_rate)
    assert grade_flight(gate.store, gate.pack, f6, a6)[0]["status"] == "regressed"


def test_worse_in_one_band_is_regressed(gate: Gate) -> None:
    baseline_and_autotune(gate)
    gate.plan(*gate.fly(LIGHT, AFTER_LIGHT))
    worse = Check(roll=(56.0, 53.0, 59.0), pitch=(35.0, 33.0, 37.0), rise=(0.12, 0.13))
    f6, a6 = gate.fly(LOADED, worse)
    assert grade_flight(gate.store, gate.pack, f6, a6)[0]["status"] == "regressed"


def test_worse_light_is_regressed_without_waiting_for_loaded(gate: Gate) -> None:
    """No loaded flight is needed to learn that the light one got worse."""
    baseline_and_autotune(gate)
    worse = Check(roll=(50.0, 47.0, 53.0), pitch=(30.0, 28.0, 32.0))
    f5, a5 = gate.fly(LIGHT, worse)
    assert grade_flight(gate.store, gate.pack, f5, a5)[0]["status"] == "regressed"


def test_an_autotune_flight_that_adds_no_axis_changes_nothing(gate: Gate) -> None:
    gate.plan(*gate.fly(LIGHT, BASE_LIGHT))
    f2, a2 = gate.fly(LIGHT, saved="roll")
    grade_flight(gate.store, gate.pack, f2, a2)
    version = gate.offer()["version"]
    f3, a3 = gate.fly(LIGHT, saved="roll")  # roll again
    assert grade_flight(gate.store, gate.pack, f3, a3) == []
    assert gate.offer()["version"] == version


@pytest.mark.parametrize(
    "loaded",
    [
        # Better, but by less than a fifth: roll 46 -> 40.
        Check(roll=(40.0, 37.0, 43.0), pitch=(35.0, 33.0, 37.0), rise=(0.12, 0.13)),
        # A fifth better, with intervals that overlap: noise, not a result.
        Check(roll=(36.0, 30.0, 44.0), pitch=(35.0, 33.0, 37.0), rise=(0.12, 0.13)),
        # Tracking better, but loaded rise time more than 1.5 times the light flight's.
        Check(roll=(34.0, 32.0, 36.0), pitch=(35.0, 33.0, 37.0), rise=(0.20, 0.13)),
        # Tracking better, but loaded much less stable than light.
        Check(roll=(34.0, 32.0, 36.0), pitch=(35.0, 33.0, 37.0), rise=(0.12, 0.13),
              peak=(1.9, 1.5)),
    ],
    ids=["small", "overlapping", "slow-loaded", "less-stable-loaded"],
)  # fmt: skip
def test_short_of_the_mark_is_no_effect(gate: Gate, loaded: Check) -> None:
    baseline_and_autotune(gate)
    gate.plan(*gate.fly(LIGHT, AFTER_LIGHT))
    f6, a6 = gate.fly(LOADED, loaded)
    assert grade_flight(gate.store, gate.pack, f6, a6)[0]["status"] == "no effect"


def test_checks_not_required_do_not_hold_the_grade(gate: Gate) -> None:
    """Rise time and the sensitivity peak need a step response; without one, tracking
    error still grades."""
    baseline_and_autotune(gate)
    gate.plan(*gate.fly(LIGHT, Check(AFTER_LIGHT.roll, AFTER_LIGHT.pitch, None, None)))
    f6, a6 = gate.fly(LOADED, Check(AFTER_LOADED.roll, AFTER_LOADED.pitch, None, None))
    assert grade_flight(gate.store, gate.pack, f6, a6)[0]["status"] == "confirmed"
    checks = gate.offer()["detail"]["grade"]["checks"]
    assert {c["metric"]: c["passed"] for c in checks if c["check"] == "metric_vs_lightest"} == {
        "tune.rise_time_roll": None, "tune.rise_time_pitch": None,
        "tune.sensitivity_peak_roll": None, "tune.sensitivity_peak_pitch": None,
    }  # fmt: skip


def test_a_tune_check_that_misses_a_band_flown_before_leaves_it_pending(gate: Gate) -> None:
    baseline_and_autotune(gate)
    flights = [gate.fly(LIGHT, AFTER_LIGHT) for _ in range(3)]  # light only, again and again
    for f, a in flights:
        grade_flight(gate.store, gate.pack, f, a)
    rec = gate.offer()
    assert rec["status"] == "applied" and rec["graded_flight_id"] is None
    assert rec["detail"]["grade_pending"]["flight"] == flights[-1][0]


def test_a_flight_that_cannot_judge_changes_nothing(gate: Gate) -> None:
    baseline_and_autotune(gate)
    f5, a5 = gate.fly(LIGHT, AFTER_LIGHT)
    grade_flight(gate.store, gate.pack, f5, a5)
    before = gate.offer()
    f6, a6 = gate.fly(LOADED)  # no stick inputs: the tune check did not run
    assert grade_flight(gate.store, gate.pack, f6, a6) == []
    assert gate.offer()["version"] == before["version"]


def test_a_person_marks_it_applied_after_the_flights_so_far(gate: Gate) -> None:
    """With no AutoTune review in the logs (older logs, AutoTune flown elsewhere), the
    flights after the one a person marked it applied on are the ones with it."""
    gate.plan(*gate.fly(LIGHT, BASE_LIGHT))
    f2, a2 = gate.fly(LOADED, BASE_LOADED)
    gate.plan(f2, a2)
    rec = gate.offer()
    assert set_status(gate.store, rec["id"], "applied", rec["version"])["status"] == "applied"
    assert gate.offer()["detail"]["applied_flight"] == f2  # F2 is a "before", not an "after"
    gate.plan(*gate.fly(LIGHT, AFTER_LIGHT))
    f4, a4 = gate.fly(LOADED, AFTER_LOADED)
    assert grade_flight(gate.store, gate.pack, f4, a4)[0]["status"] == "confirmed"


def test_a_person_taking_it_back_clears_when_it_was_done(gate: Gate) -> None:
    gate.plan(*gate.fly(LIGHT, BASE_LIGHT))
    rec = gate.offer()
    v = set_status(gate.store, rec["id"], "applied", rec["version"])["version"]
    set_status(gate.store, rec["id"], "accepted", v)
    assert "applied_flight" not in gate.offer()["detail"]


def test_a_new_build_is_offered_autotune_again(gate: Gate) -> None:
    """New motors are a new build: its AutoTune is still to do."""
    baseline_and_autotune(gate)
    build = gate.store.insert(
        "build_version",
        {"airframe_id": gate.airframe, "number": 2, "reason_kind": "upgrade",
         "reason": "170 KV motors", "spec_json": "{}", "effective_date": "2026-11-01"},
    )  # fmt: skip
    f, a = gate.fly(LIGHT, AFTER_LIGHT, build=build)
    plan = to_json(flight_plan(gate.store, None, gate.pack, f, a), gate.reg)
    assert plan["held_back"] == []
    assert any(r["source"] == f"finding:{OFFER}" for r in plan["recommendations"])


def test_large_props_prepare_with_changes_and_still_track_the_autotune(gate: Gate) -> None:
    """Rate filters to 10 Hz and more gain back-off are loaded first; AutoTune itself
    stays an action the AutoTune review marks done."""
    changes = tuple(
        {"param": p, "current": PARAMS[p], "proposed": v, "stage": "autotune"}
        for p, v in (("ATC_RAT_RLL_FLTT", 10.0), ("ATC_RAT_RLL_FLTD", 10.0),
                     ("ATC_RAT_PIT_FLTT", 10.0), ("ATC_RAT_PIT_FLTD", 10.0),
                     ("AUTOTUNE_GMBK", 0.35))
    )  # fmt: skip
    plan = gate.plan(*gate.fly(LIGHT, BASE_LIGHT, changes=changes))
    mine = [r for r in plan["recommendations"] if r["source"] == f"finding:{OFFER}"]
    assert sorted(str(r["canonical"]) for r in mine) == [
        "ATC_RAT_PIT_FLTD", "ATC_RAT_PIT_FLTT", "ATC_RAT_RLL_FLTD", "ATC_RAT_RLL_FLTT",
        "AUTOTUNE_GMBK", "None",
    ]  # fmt: skip
    assert all(r["status"] == "next" for r in mine)
    # The procedure's steps are on the session card once, not once per change.
    kinds = [s["kind"] for s in plan["session"]]
    assert kinds.count("abort") == len({s["text"]["en"] for s in plan["session"]
                                        if s["kind"] == "abort"})  # fmt: skip
    assert gate.offer()["detail"]["applied_when"]["finding"] == "autotune.saved"


def test_changes_made_ready_for_autotune_are_graded_after_it(gate: Gate) -> None:
    """The rate filters and back-off are flown on the AutoTune flights, which measure no
    tracking: they wait for the AutoTune, then grade with it."""
    prepared = {"ATC_RAT_RLL_FLTT": 10.0, "ATC_RAT_PIT_FLTT": 10.0, "AUTOTUNE_GMBK": 0.35}
    changes = tuple(
        {"param": p, "current": PARAMS[p], "proposed": v, "stage": "autotune"}
        for p, v in prepared.items()
    )
    gate.plan(*gate.fly(LIGHT, BASE_LIGHT, changes=changes))
    gate.plan(*gate.fly(LOADED, BASE_LOADED, changes=changes))
    ready = save_snapshot(
        gate.store, Snapshot(PARAMS | prepared, "copter", "4.7.0", "log start", "primary"),
        gate.pack,
    )  # fmt: skip

    def prep() -> list[dict[str, Any]]:
        return [
            r for r in history(gate.store, gate.airframe)
            if r["finding_code"] == OFFER and r["param"] is not None
        ]  # fmt: skip

    for axis in ("roll", "pitch"):  # one axis a pack, both with the changes loaded
        f, a = gate.fly(LIGHT, saved=axis, snapshot=ready)
        grade_flight(gate.store, gate.pack, f, a)
        assert len(prep()) == 3 and all(r["status"] == "applied" for r in prep())
        assert not any("grade" in r["detail"] or "grade_pending" in r["detail"] for r in prep())
    assert gate.offer()["status"] == "applied"
    f5, a5 = gate.fly(LIGHT, AFTER_LIGHT, snapshot=ready)
    grade_flight(gate.store, gate.pack, f5, a5)
    assert all("grade_pending" in r["detail"] for r in [*prep(), gate.offer()])
    f6, a6 = gate.fly(LOADED, AFTER_LOADED, snapshot=ready)
    graded = grade_flight(gate.store, gate.pack, f6, a6)
    assert sorted(str(g["param"]) for g in graded) == [*sorted(prepared), "None"]
    assert {g["status"] for g in graded} == {"confirmed"}


# --- the measurements on their own -----------------------------------------------------


def m(flight: str, group: str, after: bool, value: float, spread: float = 1.0) -> Measured:
    return Measured(flight, group, after, value, value - spread, value + spread)


def test_improves_reads_the_latest_flight_on_each_side_of_each_band() -> None:
    check = {"check": "metric_improves", "metric": "x", "by": 0.2}
    ms = [
        m("a", "band:0", False, 50.0), m("b", "band:0", False, 40.0),  # b is "before"
        m("c", "band:0", True, 35.0), m("d", "band:0", True, 30.0),  # d is "after"
    ]  # fmt: skip
    passed, info = improves(check, ms, "lower")
    assert passed is True
    [band] = info["bands"]
    assert (band["before_flight"], band["after_flight"], band["change"]) == ("b", "d", 0.25)


def test_improves_on_a_higher_is_better_metric_and_with_no_intervals() -> None:
    check = {"check": "metric_improves", "metric": "x", "by": 0.1}
    ms = [Measured("a", "label:x", False, 10.0, None, None),
          Measured("b", "label:x", True, 11.5, None, None)]  # fmt: skip
    assert improves(check, ms, "higher")[0] is True
    assert improves(check, ms, "lower") == (
        False,
        {"bands": [{"group": "label:x", "before_flight": "a", "before": 10.0,
                    "after_flight": "b", "after": 11.5, "change": -0.15, "verdict": "worse"}],
         "worse": True},
    )  # fmt: skip


def test_nothing_before_the_change_cannot_judge() -> None:
    check = {"check": "metric_improves", "metric": "x", "by": 0.2}
    assert improves(check, [m("a", "band:0", True, 10.0)], "lower")[0] is None


def test_versus_lightest_needs_two_bands_and_takes_the_extremes() -> None:
    ratio = {"check": "metric_vs_lightest", "metric": "x", "max_ratio": 1.5}
    assert versus_lightest(ratio, [m("a", "band:0", True, 0.1)])[0] is None
    ms = [m("a", "band:0", True, 0.10), m("b", "band:1", True, 0.20),
          m("c", "band:3", True, 0.14), m("d", "label:heavy", True, 9.0)]  # fmt: skip
    passed, info = versus_lightest(ratio, ms)
    assert passed is True and (info["light_flight"], info["heavy_flight"]) == ("a", "c")
    above = {"check": "metric_vs_lightest", "metric": "x", "max_above": 0.02}
    assert versus_lightest(above, ms)[0] is False
