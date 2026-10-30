"""A candidate propeller for an airframe end to end (performance/report.py prop_candidate):
ArduPilot's simulator flying Callisto light and loaded, its setup naming 30x10 carbon
propellers. The simulator logs no ESC telemetry, so nothing anchors the prediction and no
table gives a motor model: the candidate is the family's theory alone, its share of the
shaft power scaling the airframe's own model, and it says so."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_perf_report import SETUP
from test_perf_store import PACK, airframe, flown

from flight_insights.performance.candidate import CandidateError
from flight_insights.performance.report import gather, prop_candidate, prop_findings, prop_search
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

FLOWN = {"family": "carbon", "diameter_in": 30, "pitch_in": 10}


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("candidate")
    store = open_store(folder / "c.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    af = airframe(store, "Callisto", SETUP | {"prop_pitch_in": 10, "prop_family": "carbon",
                                              "priority": "endurance"})  # fmt: skip
    flown(store, cache, "copter-heavylift-sweep", af, 32.5, ["P1"])
    flown(store, cache, "copter-heavylift-sweep-loaded", af, 40.5, ["P1"])
    return {"store": store, "cache": cache, "airframe": af}


def ask(fleet: dict[str, Any], values: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return prop_candidate(fleet["store"], fleet["cache"], PACK, fleet["airframe"], values, **kw)


def test_the_propeller_flown_changes_nothing(fleet: dict[str, Any]) -> None:
    got = ask(fleet, FLOWN)
    assert got["missing"] == {} and got["inputs"]["mass_kg"] == 32.5
    c = got["candidate"]
    assert c["at"]["share"] == pytest.approx(1.0) and c["at"]["share_from"] == "shaft"
    assert {"not_anchored", "drive_efficiency_equal"} <= set(c["notes"])
    assert got["motor"] is None and c["most"] is None
    assert got["without_anchor"] == {
        "no ESC speed telemetry: the propeller's speed is not known": 2
    }
    f = got["figures"]
    assert f["candidate"] == f["flown"]
    # 8 motors on 4 arms (coaxial pairs) round a 1325 mm wheelbase
    gap = c["clearance_mm"]
    assert gap["flown"] == pytest.approx(1325 * 2**-0.5 - 30 * 25.4, abs=0.1)
    assert c["hover"][0]["mass_kg"] == 32.5 and len(c["hover"]) > 2


def test_a_bigger_propeller_hovers_longer(fleet: dict[str, Any]) -> None:
    got = ask(fleet, FLOWN | {"diameter_in": 32, "pitch_in": 11}, mass_kg=40.5)
    c = got["candidate"]
    assert c["at"]["mass_kg"] == 40.5 and c["at"]["share"] < 1.0
    assert c["at"]["candidate"]["rpm"] < c["at"]["flown"]["rpm"]
    f = got["figures"]
    assert f["candidate"]["hover_s"] > f["flown"]["hover_s"]
    assert c["clearance_mm"]["candidate"] == pytest.approx(c["clearance_mm"]["flown"] - 2 * 25.4)


@pytest.mark.parametrize(
    ("values", "says"),
    [
        ({"family": "nope", "diameter_in": 30, "pitch_in": 10}, "families"),
        ({"diameter_in": 30}, "diameter_in and pitch_in"),
        (FLOWN | {"diameter_in": -1}, "above zero"),
        (FLOWN | {"blades": 9}, "2 to 8"),
        (FLOWN | {"motor": {"kv": 170}}, "kv and resistance_mohm"),
        (FLOWN | {"motor": 5}, "an object"),
    ],
)
def test_a_candidate_it_cannot_read(fleet: dict[str, Any], values: dict[str, Any],
                                    says: str) -> None:  # fmt: skip
    with pytest.raises(CandidateError, match=says):
        ask(fleet, values)


def test_over_rpc(tmp_path: Path) -> None:
    from test_rpc import LAPTOP, Output, call
    from test_rpc import keys as store_keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, PACK)
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path),
         keys=store_keys())  # fmt: skip
    e = call(server, out, 2, "airframe.prop_candidate", airframe_id="x", candidate=5)
    assert e["error"]["code"] == -32602
    e = call(server, out, 3, "airframe.prop_candidate", airframe_id="x",
             candidate={"family": "nope", "diameter_in": 30, "pitch_in": 10})  # fmt: skip
    assert e["error"]["code"] == -32003 and "families" in e["error"]["message"]
    e = call(server, out, 4, "airframe.prop_candidate", airframe_id="x", candidate=FLOWN,
             air="thin")  # fmt: skip
    assert e["error"]["code"] == -32602
    call(server, out, 5, "engine.shutdown")


def test_the_search_for_the_priority(fleet: dict[str, Any]) -> None:
    got = prop_search(fleet["store"], fleet["cache"], PACK, fleet["airframe"], mass_kg=40.5)
    assert got["missing"] == {} and got["flown_prop"]["diameter_in"] == 30
    found = got["search"]
    assert found["priority"] == "endurance" and found["objective"] == "hover_power"
    assert found["best"] is not None and found["best"]["gain"] > 0
    # labelled theory: the simulator's flights anchor nothing, and advice is off
    assert not found["advised"] and not found["best"]["anchored"]
    assert {o["diameter_in"] for o in found["options"]} == {28, 29, 30, 31, 32}


def test_the_plan_waits_for_the_validation(fleet: dict[str, Any]) -> None:
    from dataclasses import replace

    from flight_insights.analyzers import Registry

    reg = Registry.load(PACK)
    i = gather(fleet["store"], fleet["cache"], PACK, fleet["airframe"])
    args = (i, fleet["airframe"], "f1", 40.5, 1.2, ["energy:flight"])
    assert prop_findings(fleet["store"], reg, *args) == []
    # switched on, a choice still needs anchoring on this aircraft: the simulator's
    # flights have no ESC telemetry
    props = dict(reg.rules["props"]) | {"advice": {"enabled": True, "validated_error": 0.01,
                                                   "gain_over_error": 2.0}}  # fmt: skip
    on = replace(reg, rules=dict(reg.rules) | {"props": props})
    assert prop_findings(fleet["store"], on, *args) == []
