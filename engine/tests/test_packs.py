"""Each pack's capacity and resistance, from ArduPilot's simulator flying Callisto's
heavy-lift pack shrunk to 3 Ah (SIM_BATT_CAP_AH 3, 0.024 Ohm in Callisto.json) to its
capacity failsafe, and its 44 Ah pack through the speed card: the capacity comes back
whatever the pack is rated at, a short flight gives the resistance alone, and a rating far
off on a short flight reads nothing. Across flights: BATTn_CAPACITY lowered to what its
pack measured (a safety finding when the capacity failsafe acts), the setup's rated
capacity held back while the setting agrees with the measurement, a pack worn or its
resistance risen past the end-of-life conventions, and the projections flying the pack
as measured."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from test_perf_store import FIXTURES, PACK, airframe

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.pack import _pack  # pyright: ignore[reportPrivateUsage]
from flight_insights.analyzers.registry import LANGUAGES, PLACEHOLDER
from flight_insights.analyzers.run import run_analysis
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.performance import packs, report
from flight_insights.performance.packs import Fit, aging
from flight_insights.recommend import flight_plan
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store, open_store
from flight_insights.store.keys import StoreKeys

REG = Registry.load(PACK)
CAPACITY_MAH, RESISTANCE_MOHM = 3000.0, 24.0
SETUP = {"wheelbase_mm": 1325, "prop_diameter_in": 30, "cells": 12,
         "battery_chemistry": "LiPo", "capacity_mah": 4000}  # fmt: skip
SPEC: dict[str, Any] = dict(REG.rules["performance"]["packs"])


def one_flight_measures() -> Registry:
    """The registry with a single measuring flight enough: the fixtures have one each."""
    rules = dict(REG.rules)
    rules["performance"] = dict(rules["performance"]) | {"packs": SPEC | {"min_flights": 1}}
    return replace(REG, rules=rules)


def fly(store: Store, cache: ColumnCache, name: str, af: str, pack: str) -> str:
    batch = scan(store, PACK, bytes(32), [FIXTURES / f"{name}.bin"])
    decision = Decision(batch[0].id, airframe_id=af, takeoff_kg=32.5, packs=[pack])
    [res] = commit(store, PACK, cache, batch, propose(store, PACK, batch), [decision])
    run_analysis(store, cache, PACK, res.flights[0])
    return res.flights[0]


@pytest.fixture(scope="module")
def flown(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("packs")
    store = open_store(folder / "p.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    af = airframe(store, "Callisto", SETUP)
    sweep = fly(store, cache, "copter-heavylift-sweep", af, "BIG-1")
    discharge = fly(store, cache, "copter-heavylift-discharge", af, "SMALL-1")
    return {"store": store, "cache": cache, "airframe": af, "sweep": sweep,
            "discharge": discharge}  # fmt: skip


def data(flown: dict[str, Any], which: str) -> FlightData:
    return FlightData.load(flown["store"], flown["cache"], PACK, flown[which])


def texts_filled(f: dict[str, Any]) -> None:
    text = REG.findings[f["code"]]
    for lang in LANGUAGES:
        for words in (text.title[lang], text.action[lang]):
            assert set(PLACEHOLDER.findall(words)) <= set(f["params"]), (f["code"], words)


@pytest.mark.parametrize("rated", [1500.0, 3000.0, 4000.0, 8000.0, 44000.0])
def test_the_simulators_pack_comes_back_whatever_it_is_rated(flown: Any, rated: float) -> None:
    fd = data(flown, "discharge")
    got = _pack(replace(fd, params={**fd.params, "BATT_CAPACITY": rated}), REG)
    [e] = got.evidence
    v = e.values
    assert e.id == "pack:BATT" and v["follows"] and v["rated_mah"] == rated
    assert v["capacity_mah"] == pytest.approx(CAPACITY_MAH, rel=0.02)
    assert v["resistance_mohm"] + v["slow_resistance_mohm"] == pytest.approx(RESISTANCE_MOHM,
                                                                             rel=0.12)  # fmt: skip
    assert v["free"] == (not 0.5 * rated < CAPACITY_MAH < 1.5 * rated)  # searched freely
    assert v["label_mah"] == SETUP["capacity_mah"] and v["rest"] == "after arming"
    by = {m.metric: m.value for m in got.metrics}
    assert by["pack.capacity_mah"] == pytest.approx(CAPACITY_MAH, rel=0.02)


def test_a_short_flight_gives_the_resistance_and_a_rating_far_off_reads_nothing(
    flown: Any,
) -> None:
    fd = data(flown, "sweep")  # 5 Ah of a 44 Ah pack
    got = _pack(fd, REG)
    [e] = got.evidence
    assert e.values["follows"] and e.values["capacity_mah"] is None
    assert {m.metric for m in got.metrics} == {"pack.resistance_mohm"}
    assert got.metrics[0].value == pytest.approx(RESISTANCE_MOHM, rel=0.15)
    # rated at 4 Ah, the 5 Ah it gave cannot be: the voltage follows no capacity it shows
    wrong = _pack(replace(fd, params={**fd.params, "BATT_CAPACITY": 4000.0}), REG)
    [e] = wrong.evidence
    assert not e.values["follows"] and e.values["capacity_mah"] is None
    assert wrong.metrics == [] and "does not follow" in e.summary
    # without the cells nothing is fitted
    bare = _pack(replace(fd, answers={k: v for k, v in fd.answers.items() if k != "cells"}), REG)
    assert bare.skipped and not bare.evidence


def test_the_capacity_setting_comes_down_to_what_its_pack_holds(flown: Any) -> None:
    store, flight = flown["store"], flown["discharge"]
    reg = one_flight_measures()
    analysis = run_analysis(store, flown["cache"], PACK, flight)
    failsafe = {"BATT_LOW_MAH": 450.0, "BATT_FS_LOW_ACT": 1.0}
    [f], held = packs.capacity_findings(store, reg, flight, {"BATT_CAPACITY": 4000.0} | failsafe,
                                        analysis)  # fmt: skip
    assert f["code"] == packs.CAPACITY and f["severity"] == "safety" and held == []
    assert f["changes"] == [{"param": "BATT_CAPACITY", "proposed": 2900.0, "stage": "operations"}]
    assert f["evidence"] == ["pack:BATT"] and f["params"]["measured_mah"] == pytest.approx(
        CAPACITY_MAH, rel=0.02)  # fmt: skip
    texts_filled(f)
    # no capacity failsafe acts on it: it only misreads the charge left
    [f], _ = packs.capacity_findings(store, reg, flight, {"BATT_CAPACITY": 4000.0}, analysis)
    assert f["severity"] == "caution"
    # agreeing with the pack: the setup's rated capacity is held back for it
    found, [h] = packs.capacity_findings(store, reg, flight, {"BATT_CAPACITY": 3000.0}, analysis)
    assert found == [] and h["code"] == packs.AUDIT and h["reason"] == "measured_capacity"
    # set below the pack: it only acts early, and is left
    assert packs.capacity_findings(store, reg, flight, {"BATT_CAPACITY": 2500.0}, analysis) == (
        [], [])  # fmt: skip
    # one flight measures nothing until the rules count one enough
    assert packs.capacity_findings(store, REG, flight, {"BATT_CAPACITY": 4000.0}, analysis) == (
        [], [])  # fmt: skip


def test_the_plan_takes_the_measured_pack_over_its_rating(
    flown: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = one_flight_measures()

    def load(_pack: Any) -> Registry:
        return reg

    monkeypatch.setattr(Registry, "load", staticmethod(load))
    store, flight = flown["store"], flown["discharge"]
    plan = flight_plan(store, flown["cache"], PACK, flight)
    # BATT_CAPACITY 3000 is 25% under the setup's 4000, but its pack measured 2963
    [h] = [h for h in plan.held_back if h["code"] == packs.AUDIT]
    assert h["reason"] == "measured_capacity" and h["param"] == "BATT_CAPACITY"
    assert not [r for r in plan.recommendations if r.param == "BATT_CAPACITY"]
    # the 4000 mAh pack holds 74% of it: worn, an action for a person
    worn = [r for r in plan.recommendations if any(s.code == packs.WORN for s in r.sources)]
    assert worn and worn[0].param is None
    # the projections fly the pack as measured
    inputs = report.gather(store, flown["cache"], PACK, flown["airframe"])
    assert inputs.pack is not None
    assert inputs.pack.capacity_ah == pytest.approx(CAPACITY_MAH / 1000, rel=0.02)
    assert inputs.pack.resistance == pytest.approx(RESISTANCE_MOHM / 1000, rel=0.15)
    assert "measured" in inputs.sources["pack"]


def fits(capacities: list[float | None], ohms: list[float]) -> list[Fit]:
    pairs = zip(capacities, ohms, strict=True)
    return [Fit(f"f{k}", "BATT", c, r, 3000.0) for k, (c, r) in enumerate(pairs)]


def test_a_pack_ages_past_the_end_of_life_conventions() -> None:
    worn = fits([3000.0, 2950.0, 2350.0, 2300.0, 2250.0, 2300.0, 2280.0], [20.0] * 7)
    [(code, p)] = aging(worn, 3000.0, SPEC)
    assert code == packs.WORN and p["capacity_pct"] == 77 and p["measured"] == 5
    risen = fits([None] * 6, [20.0, 21.0, 20.0, 40.0, 42.0, 41.0])
    [(code, p)] = aging(risen, 3000.0, SPEC)
    assert code == packs.WORN_RESISTANCE and p["ratio"] == pytest.approx(41.0 / 20.0, abs=0.01)
    assert p["compared"] == 3
    # within the conventions, no rating, or too few flights: nothing
    assert aging(fits([2900.0] * 4, [20.0, 20.0, 29.0, 30.0]), 3000.0, SPEC) == []
    assert aging(fits([2000.0] * 4, [20.0] * 4), None, SPEC) == []
    assert aging(fits([2000.0], [20.0]), 3000.0, SPEC) == []
    for code in (packs.WORN, packs.WORN_RESISTANCE):
        params = {"pack": "P1", "flights": 7} | dict(
            aging(worn if code == packs.WORN else risen, 3000.0, SPEC)
        )[code]
        texts_filled({"code": code, "params": params})
