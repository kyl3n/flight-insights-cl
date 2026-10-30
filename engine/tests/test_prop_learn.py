"""Flown propellers calibrating their family, as the report gathers them
(performance/report.py): two airframes of one platform, each flown on two builds'
propellers of the carbon family, their hovers planted as their models hold them (the
family's starting theory, with each aircraft's own losses). The platform's aircraft
calibrate the family together; grading an airframe's own change leaves its flights out."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from test_perf_report import SETUP
from test_perf_store import PACK, airframe

from flight_insights.analyzers import Registry
from flight_insights.analyzers.flight import build_answers
from flight_insights.performance import airframe as perf
from flight_insights.performance.points import FlightPoints
from flight_insights.performance.propeller import INCH, Anchor
from flight_insights.performance.propfamily import Prop, coefficients, starting
from flight_insights.performance.report import Inputs, _families, _installed, _track, gather  # pyright: ignore[reportPrivateUsage]
from flight_insights.platforms.records import create_airframe, record_change
from flight_insights.platforms.schema import load_schema
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store, open_store
from flight_insights.store.keys import StoreKeys

REG = Registry.load(PACK)
PROPS: dict[str, Any] = dict(REG.rules["props"])
PERF: dict[str, Any] = dict(REG.rules["performance"])
RHO = 1.2


def hover(store: Store, airframe_id: str, n: int, kg: float, kt: float) -> FlightPoints:
    """A flight on the airframe's current build, hovering at `kg` on its propeller as the
    family's starting theory gives it, its thrust coefficient times the aircraft's `kt`."""
    row = store.get("airframe", airframe_id)
    assert row is not None
    with store.transaction():
        fid = store.insert("flight", {"airframe_id": airframe_id,
                                      "build_version_id": row["current_build_id"],
                                      "boot_utc_us": n * 10**9, "arm_us": 0})  # fmt: skip
    build = store.get("build_version", str(row["current_build_id"]))
    assert build is not None
    a = build_answers(store, str(build["id"]))
    prop = Prop("carbon", float(a["prop_diameter_in"]), float(a["prop_pitch_in"]))
    thrust, d, rpm = kg * 9.80665 / 8, prop.diameter_in * INCH, 2000.0
    mean, _, _ = starting(PROPS)
    for _ in range(40):
        ct = kt * float(coefficients(mean, PROPS, prop, rpm, RHO)[0][0])
        rpm = 60 * (thrust / (ct * RHO * d**4)) ** 0.5
    return FlightPoints(fid, (), kg, "weighed", "none", "standard",
                        anchor=Anchor(rpm, thrust, 500.0, "packs", RHO, 60.0, 8, 0.0))  # fmt: skip


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("learn")
    store = open_store(folder / "l.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    schema = load_schema(PACK)
    af = airframe(store, "Callisto", SETUP | {"prop_pitch_in": 10, "prop_family": "carbon"})
    row = store.get("airframe", af)
    assert row is not None
    twin = create_airframe(store, str(row["platform_id"]), "Callisto 2")
    # the twin: 30x10, then 32x11; its model kept as an earlier visit made it
    kept = [hover(store, twin, 1, 32.5, 0.9), hover(store, twin, 2, 40.5, 0.9)]
    record_change(store, schema, twin, {"prop_diameter_in": 32, "prop_pitch_in": 11}, "test")
    kept += [hover(store, twin, 3, 32.5, 0.9), hover(store, twin, 4, 40.5, 0.9)]
    model = perf.AirframeModel(twin, None, None, True, tuple(kept), {}, "planted")
    with store.transaction() as conn:
        conn.execute("INSERT INTO performance_fit (airframe_id, basis, model_json) "
                     "VALUES (?, ?, ?)", (twin, "planted", perf.to_json(model)))  # fmt: skip
    # this airframe: 30x10, then 28x9; its own model as gathered now
    own = [hover(store, af, 5, 32.5, 0.8), hover(store, af, 6, 40.5, 0.8)]
    record_change(store, schema, af, {"prop_diameter_in": 28, "prop_pitch_in": 9}, "test")
    own += [hover(store, af, 7, 32.5, 0.8), hover(store, af, 8, 40.5, 0.8)]
    i = gather(store, cache, PACK, af)
    i = replace(i, model=replace(i.model, flights=tuple(own)))
    return {"store": store, "af": af, "twin": twin, "i": i}


def test_the_platforms_aircraft_and_their_propellers(fleet: dict[str, Any]) -> None:
    store, af, i = fleet["store"], fleet["af"], fleet["i"]
    got = _installed(store, af, i, PROPS, PERF, [])
    assert sorted((s.aircraft == af, s.prop.diameter_in, len(s.points)) for s in got) == [
        (False, 30.0, 2), (False, 32.0, 2), (True, 28.0, 2), (True, 30.0, 2)]  # fmt: skip
    # no motor model: the thrust coefficient only, as the hover measured it
    assert all(p.cp is None and p.ct > 0 for s in got for p in s.points)


def test_the_family_learns_from_both_and_grading_leaves_its_own_out(
    fleet: dict[str, Any],
) -> None:
    store, af, i = fleet["store"], fleet["af"], fleet["i"]
    both = _families(store, af, i, PROPS, PERF, [])["carbon"]
    alone = _families(store, af, i, PROPS, PERF, [], leave_out=True)["carbon"]
    assert (both.aircraft, alone.aircraft) == (2, 1) and both.tables == alone.tables == 0
    assert not both.calibrated  # no table: nothing is advised on flights alone
    # the change to 28x9 graded from the twin's flights only, on the same theory: near exact
    t = _track(store, af, i, PROPS, PERF, [])
    [change] = t["changes"]
    assert (change["before"], change["after"], change["graded"]) == (
        "carbon:30x10x2", "carbon:28x9x2", True)  # fmt: skip
    assert change["speed_error"] < 0.005 and "power_error" not in change
    assert t["record"]["changes"] == 1


def test_a_kept_model_an_older_engine_wrote_is_passed_over(fleet: dict[str, Any]) -> None:
    store, af, twin = fleet["store"], fleet["af"], fleet["twin"]
    i: Inputs = fleet["i"]
    with store.transaction() as conn:
        old = conn.execute("SELECT model_json FROM performance_fit WHERE airframe_id = ?",
                           (twin,)).fetchone()[0]  # fmt: skip
        conn.execute("UPDATE performance_fit SET model_json = '{}' WHERE airframe_id = ?", (twin,))
    try:
        got = _installed(store, af, i, PROPS, PERF, [])
        assert {s.aircraft for s in got} == {af}
    finally:
        with store.transaction() as conn:
            conn.execute("UPDATE performance_fit SET model_json = ? WHERE airframe_id = ?",
                         (old, twin))  # fmt: skip
