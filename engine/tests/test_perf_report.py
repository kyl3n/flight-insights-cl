"""What the Range & speed screen and the range card get for an airframe, from its setup, its
parameters and its flights: ArduPilot's simulator flying Callisto's speed card light
(32.5 kg) and 8 kg heavier, with a 12S 30 Ah pack in the setup. Every section is worked out
and agrees with itself. An airframe that has only hovered gets its hover time alone, once
its pack's size is entered, and is told what to fly for the rest."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from test_perf_store import CALLISTO, PACK, airframe, flown

from flight_insights.analyzers import Registry
from flight_insights.performance import bench
from flight_insights.performance.report import range_card, report
from flight_insights.platforms.records import platform_view, save_answers
from flight_insights.platforms.schema import load_schema
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

SETUP = CALLISTO | {"cells": 12, "capacity_mah": 30000, "battery_chemistry": "LiPo",
                    "empty_kg": 32.5, "typical_takeoff_kg": 32.5, "max_takeoff_kg": 45,
                    "max_current_a": 300, "reserve_pct": 20, "pack_mass_kg": 8}  # fmt: skip


def make_fleet(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The simulator's Callisto flown light and loaded, and a hover-only airframe."""
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("report")
    store = open_store(folder / "r.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    af = airframe(store, "Callisto", SETUP)
    flown(store, cache, "copter-heavylift-sweep", af, 32.5, ["P1"])
    flown(store, cache, "copter-heavylift-sweep-loaded", af, 40.5, ["P1"])
    bare = airframe(store, "Callisto, no pack", CALLISTO | {"typical_takeoff_kg": 32.5})
    flown(store, cache, "copter-heavylift-discharge", bare, 32.5)
    return {"store": store, "cache": cache, "airframe": af, "bare": bare}


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return make_fleet(tmp_path_factory)


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


def test_each_figure_says_what_ends_it(got: dict[str, Any]) -> None:
    # the latest flight's BATT_CAPACITY is the pack's, with no BATT_LOW_MAH: the reserve
    assert got["ends"] == {"hover": "reserve", "endurance": "reserve", "range": "reserve"}
    assert got["failsafe"]["capacity_mah"] == 44000 and got["failsafe"]["low_mah"] is None


def test_the_curves_for_the_power_and_speed_charts(got: dict[str, Any]) -> None:
    c, f = got["curves"], got["figures"]
    v = c["airspeed_ms"]
    assert v[0] == 0 and v[-1] >= got["inputs"]["top_ms"] * 1.3 - 0.5
    assert [p["mass_kg"] for p in c["power"]] == [32.5, 45.0]  # empty (the typical) and heaviest
    for p in c["power"]:
        assert len(p["power_w"]) == len(v)
        assert all(
            lo <= m <= hi for lo, m, hi in zip(p["low_w"], p["power_w"], p["high_w"], strict=True)
        )
    light, heavy = c["power"]
    assert all(h > lt for lt, h in zip(light["power_w"], heavy["power_w"], strict=True))
    # within the speeds flown (the search's top), energy per km over the ground is least at
    # the best-range speed in still air; into a headwind it costs more at every speed, and
    # there is no way through a headwind at less than its speed
    flown = [k for k, s in enumerate(v) if s <= got["inputs"]["top_ms"] + 1e-9]
    still = c["wh_per_km"]["0.0"]
    i = min((k for k in flown if still[k] is not None), key=lambda k: still[k])
    assert abs(v[i] - f["range_ms"]) <= 0.5
    windy = c["wh_per_km"]["10.0"]
    assert windy[v.index(10.0)] is None
    assert all(w > s for s, w in zip(still, windy, strict=True) if w is not None)
    assert max(c["flight_s"]) == pytest.approx(f["endurance_s"], rel=0.05)
    assert {p["phase"] for p in c["points"]} <= {"hover", "level"} and len(c["points"]) > 5
    assert set(c["points"][0]) == {
        "flight",
        "mass_kg",
        "phase",
        "airspeed_ms",
        "power_w",
        "seconds",
    }
    assert got["wp_spd_ms"] is not None


def test_the_radius_falls_as_the_wind_rises(got: dict[str, Any]) -> None:
    rw = got["radius_wind"]
    assert [x["wind_ms"] for x in rw] == [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0]
    worst = [x["worst_m"] for x in rw]
    assert worst == sorted(worst, reverse=True)
    # in still air every way round is the same; across the wind it is never the worst
    assert rw[0]["across_m"] == pytest.approx(rw[0]["worst_m"], rel=1e-6)
    assert all(x["across_m"] >= x["worst_m"] - 1e-6 for x in rw)
    assert got["inputs"]["speed_goal"] == "efficient"


def test_the_wind_costs_radius_and_the_headwind_ground_speed(got: dict[str, Any]) -> None:
    r = got["radius"]
    assert r["wind_ms"] == 5.0 and len(r["by_direction"]) == 72
    assert r["worst_m"] < got["figures"]["radius_m"]["out_and_back"]
    for h in got["headwind"]:
        assert h["ground"] == pytest.approx(h["airspeed"] - h["wind_ms"], abs=1e-6)
    # in still air the best leg is the best-range speed, at the weight picked
    still = got["headwind"][0]
    assert still["wh_per_km"] == pytest.approx(got["speeds"]["range_wh_per_km"], rel=0.05)
    gains = {w["lever"]: w for w in got["what_ifs"]}
    assert gains["mass"]["hover_s"] > 0 and gains["capacity"]["range_m"] > 0
    assert all(w["change"] != 0 for w in got["what_ifs"])


def test_the_pack_against_load_and_each_pack_over_its_flights(got: dict[str, Any]) -> None:
    b = got["battery"]
    usable = b["usable"]
    powers = [x["power_w"] for x in usable]
    assert powers == sorted(powers) and len(powers) >= 10
    # a heavier load gets less of the pack: more is lost in its resistance
    energy = [x["energy_wh"] for x in usable]
    assert all(a >= b - 1e-6 for a, b in pairwise(energy))
    ends = {"reserve", "capacity_failsafe", "voltage_failsafe", "cutoff", "empty", "power",
            "current", "time"}  # fmt: skip
    assert {x["ends"] for x in usable} <= ends
    # the loads the figures fly at sit on the grid, and end as the figures say
    assert set(b["loads"]) == {"hover", "endurance", "range"}
    assert all(powers[0] <= v <= powers[-1] for v in b["loads"].values())
    assert b["loads"]["endurance"] <= b["loads"]["hover"]
    assert b["aged_capacity"] == 0.8 and b["aged_resistance"] == 2.0
    assert b["caution_resistance"] == 1.5
    # the one pack flew both sweeps alone: its resistance on each, its rated capacity
    [p] = b["packs"]
    assert p["code"] == "P1" and p["rated_mah"] == 30000
    assert len(p["flights"]) == 2 and all(f["resistance_mohm"] > 0 for f in p["flights"])


def test_the_figures_against_density_altitude(got: dict[str, Any]) -> None:
    d = got["density"]
    rules = Registry.load(PACK).rules["performance"]["report"]
    rows = d["rows"]
    assert [r["altitude_m"] for r in rows] == [float(h) for h in rules["density_altitudes_m"]]
    assert rows[0]["density"] == pytest.approx(1.225, abs=1e-3)
    hover = [r["hover_s"] for r in rows]
    reach = [r["range_m"] for r in rows]
    assert hover == sorted(hover, reverse=True) and reach == sorted(reach, reverse=True)
    # thin air costs induced power, most in hover: hover endurance falls faster than range
    assert hover[-1] / hover[0] < reach[-1] / reach[0] < 1
    # the airframe's usual air sits inside what it flew, on the laptop
    low, high = d["flown_m"]
    assert low <= d["usual_m"] <= high


def test_the_data_behind_the_figures(got: dict[str, Any]) -> None:
    d = got["data"]
    assert {c["mass_kg"] for c in d["grid"]} == {32.5, 40.5}
    # hover is airspeed 0, and the sweep's legs reach 12 m/s at both weights
    for kg in (32.5, 40.5):
        speeds = {c["airspeed_ms"] for c in d["grid"] if c["mass_kg"] == kg}
        assert 0 in speeds and 12 in speeds
    assert all(c["minutes"] > 0 for c in d["grid"])
    assert d["level"] == "A" and d["flights"] == 2 and len(d["lofo"]) == 2
    errors = [abs(x["error"]) for x in d["lofo"]]
    assert d["lofo_median"] == pytest.approx(sum(errors) / 2, rel=1e-3)
    assert d["targets"] == {"median": 0.05, "p90": 0.1}


def test_each_what_if_says_what_it_rests_on(got: dict[str, Any]) -> None:
    shares = {w["lever"]: w["share"] for w in got["what_ifs"]}
    rules = Registry.load(PACK).rules["performance"]["projections"]["what_ifs"]
    assert shares["mass"] == pytest.approx(rules["mass_share"], abs=1e-3)
    assert shares["drag"] == pytest.approx(rules["drag_share"], abs=1e-3)
    assert shares["capacity"] == pytest.approx(rules["capacity_share"], abs=1e-3)
    basis = {w["lever"]: w["basis"] for w in got["what_ifs"]}
    # flown at 32.5 and 40.5 kg: the weight's effect is measured, the rest the model's
    assert basis.pop("mass") == "measured"
    assert set(basis.values()) == {"theory"}


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


def test_two_builds_side_by_side(fleet: dict[str, Any], got: dict[str, Any]) -> None:
    """Made-up bench tables (power = c * thrust^1.5): the candidate takes 0.9 of the
    current's power at any thrust, and its motors weigh 20 g more each."""
    from test_bench import table

    b = got["builds"]
    assert b["tables"] == [] and b["missing"] == "current" and "compare" not in b
    store, cache, af = fleet["store"], fleet["cache"], fleet["airframe"]
    platform = store.get("airframe", af)["platform_id"]
    bench.save(store, platform, table(0.002, role="current", motor_mass_g=400))
    r = report(store, cache, PACK, af)["builds"]
    n = r["motors"]
    assert n == 8 and r["missing"] == "candidate"
    fvb = r["flown_vs_bench"]  # the flown hover power per motor against the table's
    assert fvb["thrust_g"] == pytest.approx(32500 / n, rel=1e-3)
    assert fvb["bench_w"] == pytest.approx(0.002 * (32500 / n) ** 1.5, rel=1e-3)
    assert (
        fvb["share"] == pytest.approx(fvb["flown_w"] / fvb["bench_w"], rel=1e-3)
        and fvb["share"] > 0
    )
    bench.save(store, platform, table(0.0018, role="candidate", motor_mass_g=420))
    r = report(store, cache, PACK, af)["builds"]
    c = r["compare"]
    assert c["delta_kg"] == pytest.approx(n * 0.020, rel=1e-3) and c["notes"] == []
    assert c["mass_kg"] == pytest.approx(32.5 + n * 0.020, rel=1e-3)
    assert c["share"] == pytest.approx(0.9, rel=1e-3)
    assert r["headroom"]["current"] == pytest.approx(12500 * n / 32500, rel=1e-3)
    assert r["headroom"]["candidate"] < r["headroom"]["current"]  # heavier, same top
    now, then = r["figures"]["current"], r["figures"]["candidate"]
    assert then["hover_s"] > now["hover_s"] and then["range_m"] > now["range_m"]
    power = r["power"]
    assert len(power["airspeed_ms"]) == len(power["current_w"]) == len(power["candidate_w"])
    assert all(b < a for a, b in zip(power["current_w"], power["candidate_w"], strict=True))
    rows = r["against_weight"]
    assert rows[0]["mass_kg"] == 32.5 and rows[-1]["mass_kg"] == 45.0
    assert all(x["range_m"] and x["range_m"] > 0 for x in rows)
    ranges = [x["range_m"] for x in rows]
    assert ranges == sorted(ranges, reverse=True)
    # a candidate whose table stops short of the hover thrust gives no figures
    bench.save(store, platform, table(0.0016, (1500.0, 2500.0, 3500.0), top=62,
                                      role="candidate"))  # fmt: skip
    r = report(store, cache, PACK, af)["builds"]
    assert r["compare"]["share"] is None and "figures" not in r
    assert {"candidate_out_of_range", "candidate_short"} <= set(r["compare"]["notes"])


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
    good = {"label": "made-up", "role": "current", "rows": [
        {"throttle_pct": t, "thrust_g": g, "power_w": w}
        for t, g, w in ((30, 1000, 50), (60, 3000, 250), (100, 6000, 700))]}  # fmt: skip
    e = call(server, out, 23, "bench.save", platform_id=pl["id"], values=good | {"rows": []})
    assert e["error"]["code"] == -32003 and "three steps" in e["error"]["message"]
    e = call(server, out, 24, "bench.save", platform_id=pl["id"], values="x")
    assert e["error"]["code"] == -32602
    saved = call(server, out, 25, "bench.save", platform_id=pl["id"], values=good)["result"]
    assert saved["role"] == "current" and saved["max_thrust_g"] == 6000
    [listed] = call(server, out, 26, "bench.list", platform_id=pl["id"])["result"]
    assert listed == saved
    e = call(server, out, 27, "bench.save", platform_id=pl["id"], values=good,
             id=saved["id"], version=saved["version"] + 5)  # fmt: skip
    assert e["error"]["code"] == -32004
    call(server, out, 22, "engine.shutdown")
