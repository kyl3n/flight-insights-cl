"""What the Range & speed screen and the range card get for an airframe, from its setup, its
parameters and its flights: ArduPilot's simulator flying Callisto's speed card light
(32.5 kg) and 8 kg heavier, with a 12S 30 Ah pack in the setup. Every section is worked out
and agrees with itself. An airframe that has only hovered gets its hover time alone, once
its pack's size is entered, and is told what to fly for the rest."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_perf_store import CALLISTO, PACK, airframe, flown

from flight_insights.performance.report import range_card, report
from flight_insights.platforms.records import platform_view, save_answers
from flight_insights.platforms.schema import load_schema
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

SETUP = CALLISTO | {"cells": 12, "capacity_mah": 30000, "battery_chemistry": "LiPo",
                    "empty_kg": 32.5, "typical_takeoff_kg": 32.5, "max_takeoff_kg": 45,
                    "max_current_a": 300, "reserve_pct": 20, "pack_mass_kg": 8}  # fmt: skip


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("report")
    store = open_store(folder / "r.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    af = airframe(store, "Callisto", SETUP)
    flown(store, cache, "copter-heavylift-sweep", af, 32.5)
    flown(store, cache, "copter-heavylift-sweep-loaded", af, 40.5)
    bare = airframe(store, "Callisto, no pack", CALLISTO | {"typical_takeoff_kg": 32.5})
    flown(store, cache, "copter-heavylift-discharge", bare, 32.5)
    return {"store": store, "cache": cache, "airframe": af, "bare": bare}


@pytest.fixture(scope="module")
def got(fleet: dict[str, Any]) -> dict[str, Any]:
    return report(fleet["store"], fleet["cache"], PACK, fleet["airframe"], wind_ms=5.0)


def test_every_input_is_found_and_says_where_from(got: dict[str, Any]) -> None:
    assert got["missing"] == {} and got["level"] == "A"
    assert "12S LiPo of 30000 mAh" in got["sources"]["pack"]
    assert "own drag" in got["sources"]["drag"]
    i = got["inputs"]
    assert i["mass_kg"] == 32.5 and i["mass_from"] == "the setup's typical take-off"
    assert i["flown_kg"] == [32.5, 40.5] and 11.5 < i["top_ms"] < 12.5


def test_the_figures_agree_with_themselves(got: dict[str, Any]) -> None:
    s, f, sp = got["speeds"], got["figures"], got["spread"]
    assert 0 < s["endurance"] <= s["range"] <= got["inputs"]["top_ms"] + 1e-9
    assert f["endurance_ms"] == s["endurance"] and f["range_ms"] == s["range"]
    # the power and energy per km as the packs give them, not the pack-weighted ranking
    assert s["range_wh_per_km"] == pytest.approx(f["efficiency"]["wh_per_km"], rel=1e-6)
    for k in ("hover_s", "endurance_s", "range_m", "radius_m"):
        assert sp[k]["low"] <= sp[k]["median"] <= sp[k]["high"]
    assert sp["hover_s"]["median"] == pytest.approx(f["hover_s"], rel=1e-6)
    assert f["hover_s"] <= f["endurance_s"] and f["measured"]
    weights = [x["mass_kg"] for x in got["against_weight"]]
    assert weights[0] == 32.5 and weights[-1] == 45.0
    hovers = [x["hover_s"] for x in got["against_weight"]]
    assert hovers == sorted(hovers, reverse=True)


def test_the_wind_costs_radius_and_the_headwind_ground_speed(got: dict[str, Any]) -> None:
    r = got["radius"]
    assert r["wind_ms"] == 5.0 and len(r["by_direction"]) == 72
    assert r["worst_m"] < got["figures"]["radius_m"]["out_and_back"]
    for h in got["headwind"]:
        assert h["ground"] == pytest.approx(h["airspeed"] - h["wind_ms"], abs=1e-6)
    gains = {w["lever"]: w for w in got["what_ifs"]}
    assert gains["mass"]["hover_s"] > 0 and gains["capacity"]["range_m"] > 0
    assert all(w["change"] != 0 for w in got["what_ifs"])


def test_the_top_speed_and_the_next_step(got: dict[str, Any]) -> None:
    ts, up = got["top_speed"], got["build_up"]
    assert ts["by"] in ("lean", "thrust", "current", "search")
    assert ts["extrapolated"] == (ts["airspeed"] > got["inputs"]["top_ms"])
    # the sweep held 12 m/s for 40 s with every watched metric in target: the next is 13
    assert up["verified_ms"] == pytest.approx(12.0, abs=0.3)
    assert up["next_ms"] == pytest.approx(up["verified_ms"] + 1.0)
    assert {c["name"] for c in up["checks"]} >= {"lean", "thrust", "current"}
    assert up["offered"] == (up["binding"] is None)


def test_a_hover_only_airframe_gets_its_hover_time(fleet: dict[str, Any]) -> None:
    store, cache, bare = fleet["store"], fleet["cache"], fleet["bare"]
    r = report(store, cache, PACK, bare)
    assert "cells and capacity" in r["missing"]["pack"] and "speed card" in r["missing"]["top"]
    assert not {"hover", "figures", "speeds"} & set(r)
    # the pack entered: its own hovers tell the hover time, and nothing faster
    schema = load_schema(PACK)
    platform = store.get("airframe", bare)["platform_id"]
    version = platform_view(store, schema, platform)["reference_build"]["version"]
    save_answers(store, schema, platform, version, {k: SETUP[k] for k in (
        "cells", "capacity_mah", "battery_chemistry", "reserve_pct")})  # fmt: skip
    r = report(store, cache, PACK, bare)
    assert "pack" not in r["missing"] and "top" in r["missing"]
    assert not {"figures", "speeds", "radius", "top_speed"} & set(r)
    h = r["hover"]
    assert h["mass_kg"] == 32.5 and h["measured"]
    assert 0 < h["hover_s"]["low"] <= h["hover_s"]["median"] <= h["hover_s"]["high"]
    [row] = range_card(store, cache, PACK, bare)["rows"]
    assert row["hover_min"] == pytest.approx(h["hover_s"]["low"] / 60, rel=1e-3)
    assert "cruise_ms" not in row and "radius_km" not in row


def test_the_range_card(fleet: dict[str, Any]) -> None:
    card = range_card(fleet["store"], fleet["cache"], PACK, fleet["airframe"])
    rows = card["rows"]
    assert [r["mass_kg"] for r in rows] == [32.5, 45.0]
    light, heavy = rows
    assert light["measured"] and not heavy["measured"]  # 45 kg is past the 40.5 flown
    assert light["hover_min"] > heavy["hover_min"] > 0
    assert light["cruise_for"] == "range"  # the setup gives no Priority: best range
    assert set(light["radius_km"]) == {"0.0", "5.0", "10.0"}
    assert light["radius_km"]["0.0"] > light["radius_km"]["5.0"] > light["radius_km"]["10.0"]
    assert light["radius_still_low_km"] <= light["radius_km"]["0.0"]
    assert light["verified_ms"] == pytest.approx(12.0, abs=0.3)
    assert card["valid"]["flown_kg"] == [32.5, 40.5]


def test_over_rpc(fleet: dict[str, Any], tmp_path: Path) -> None:
    from test_rpc import LAPTOP, Output, call
    from test_rpc import keys as store_keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, PACK)
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path),
         keys=store_keys())  # fmt: skip
    for n, bad in enumerate(({"mass_kg": -1}, {"air": "thin"}, {"trip": "route"},
                             {"wind_ms": 99})):  # fmt: skip
        e = call(server, out, 10 + n, "airframe.performance", airframe_id="x", **bad)
        assert e["error"]["code"] == -32602
    for n, method in enumerate(("airframe.performance", "airframe.range_card")):
        e = call(server, out, 15 + n, method, airframe_id="none")
        assert e["error"]["code"] == -32003 and "no such airframe" in e["error"]["message"]
    pl = call(server, out, 17, "platform.create", name="Empty", vehicle="copter")["result"]
    af = call(server, out, 18, "airframe.create", platform_id=pl["id"], name="E-1")["result"]["id"]
    nothing = call(server, out, 20, "airframe.performance", airframe_id=af)["result"]
    assert "flights" in nothing["missing"]
    card = call(server, out, 21, "airframe.range_card", airframe_id=af)["result"]
    assert card["rows"] == []
    call(server, out, 22, "engine.shutdown")
