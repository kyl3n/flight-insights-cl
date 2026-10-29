"""Performance trends: like-with-like series, the airframe's own baseline from its good
flights, drift flagged inside the fixed zones, the event lane, and the platform view."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from flight_insights.analyzers import Registry
from flight_insights.knowledge import load_pack
from flight_insights.payload_bands import Flown, bands
from flight_insights.platforms.records import create_airframe, create_platform
from flight_insights.platforms.schema import load_schema
from flight_insights.store import Store, StoreKeys, open_store
from flight_insights.trends import (
    Conditions,
    baseline,
    events,
    fleet_overview,
    flight_list,
    overview,
    payloads,
    platform,
    series,
)

VIBE = "vibration.p95"  # lower is better; target below 30 m/s/s
T0 = 1_760_000_000_000_000


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with open_store(tmp_path / "f.db", StoreKeys.generate(), "e" * 32) as s:
        yield s


def airframe(store: Store, name: str = "HL1", platform_id: str | None = None) -> str:
    pid = platform_id or create_platform(store, load_schema(load_pack()), "HL", "copter")
    return create_airframe(store, pid, name)


def fly(
    store: Store,
    airframe_id: str,
    n: int,
    values: dict[tuple[str, str], float],
    *,
    payload: str | None = None,
    firmware: str = "4.7.0",
    bad: bool = False,
    thrust: float | None = None,
    takeoff_kg: float | None = None,
) -> str:
    """Flight number n of the airframe, analysed, with these metric values; `thrust` is
    its hover thrust at standard air (payload.hover_thrust), `takeoff_kg` its weight."""
    fid = store.insert(
        "flight",
        {"airframe_id": airframe_id, "firmware": firmware, "payload": payload,
         "boot_utc_us": T0 + n * 86_400_000_000, "arm_us": 1_000_000,
         "takeoff_kg": takeoff_kg},
    )  # fmt: skip
    if thrust is not None:
        values = values | {("payload.hover_thrust", "hover"): thrust}
    run = store.insert(
        "analysis_run",
        {"flight_id": fid, "engine_version": "t", "knowledge_version": "t",
         "analyzers_json": "{}", "status": "complete"},
    )  # fmt: skip
    for (metric, segment), value in values.items():
        store.insert(
            "metric_value",
            {"run_id": run, "flight_id": fid, "metric": metric, "segment": segment,
             "value": value, "zone": "target" if value < 30 else "caution"},
        )  # fmt: skip
    if bad:
        store.insert(
            "finding",
            {"run_id": run, "flight_id": fid, "analyzer": "vibration", "code": "vibration.high",
             "severity": "out", "confidence": "high", "evidence_json": "[]"},
        )  # fmt: skip
    return fid


def hover(value: float) -> dict[tuple[str, str], float]:
    return {(VIBE, "hover"): value}


def test_the_baseline_needs_enough_flights() -> None:
    rules = {"baseline_min_flights": 5, "baseline_max_flights": 20,
             "baseline_percentiles": [10, 90]}  # fmt: skip
    assert baseline([10, 11, 12, 13], rules) is None
    b = baseline([10, 11, 12, 13, 14], rules)
    assert b is not None and b["median"] == 12 and 10 < b["low"] < b["high"] < 14


def test_a_series_reads_against_zones_and_its_own_baseline(store: Store, reg: Registry) -> None:
    af = airframe(store)
    for n, v in enumerate([12, 13, 12.5, 14, 13, 12, 13.5]):
        fly(store, af, n, hover(v))
    s = series(store, reg, af, VIBE, "hover")
    assert [p["value"] for p in s["points"]] == [12, 13, 12.5, 14, 13, 12, 13.5]
    assert s["baseline"] is not None and s["baseline"]["flights"] == 7
    assert s["zones"]["target"] == [[None, 30]] and s["drift"] is None
    assert s["source"]["kind"] == "ardupilot"


def test_each_point_carries_its_flights_weather(store: Store, reg: Registry) -> None:
    """From the record kept for the flight, so the chart can be read at like wind and
    density; none for a flight with no record, or one the downloads did not cover."""
    af = airframe(store)
    ids = [fly(store, af, n, hover(v)) for n, v in enumerate([12, 13, 12.5])]
    kept = {"quality": "reanalysis", "coverage": 0.9, "wind_ms": 4.2, "gust_max_ms": 7.0,
            "density_altitude_m": 640.0, "temperature_c": 21.5, "pressure_hpa": 950.0,
            "kp_max": 3.0}  # fmt: skip
    for fid, rec in ((ids[0], kept), (ids[1], {"quality": "none", "coverage": 0.0})):
        store.conn.execute(
            "INSERT INTO flight_weather (flight_id, basis, record_json) VALUES (?, 'b', ?)",
            (fid, json.dumps(rec)),
        )
    points = series(store, reg, af, VIBE, "hover")["points"]
    assert points[0]["weather"] == {
        "quality": "reanalysis", "coverage": 0.9, "wind_ms": 4.2, "gust_max_ms": 7.0,
        "density_altitude_m": 640.0, "temperature_c": 21.5,
    }  # fmt: skip
    assert points[1]["weather"] is None and points[2]["weather"] is None


def keep_weather(store: Store, fid: str, wind: float, da: float, coverage: float = 0.9) -> None:
    rec = {"quality": "reanalysis", "coverage": coverage, "wind_ms": wind, "gust_max_ms": wind + 3,
           "density_altitude_m": da, "temperature_c": 15.0}  # fmt: skip
    store.conn.execute(
        "INSERT INTO flight_weather (flight_id, basis, record_json) VALUES (?, 'b', ?)",
        (fid, json.dumps(rec)),
    )


def test_a_series_narrowed_to_like_wind_and_density(store: Store, reg: Registry) -> None:
    """Calm flights hold 12-13; the windy ones read 20 and would make a calm flight's
    baseline wrong. Up to 6 m/s keeps the calm ones; like density keeps those within
    300 m of the shown flight's density altitude (attribution's threshold); a flight
    with no record, or one the downloads barely covered, is counted and left out."""
    af = airframe(store)
    flown = [(12, 3.0, 400.0), (20, 9.0, 450.0), (13, 4.0, 900.0), (12.5, 2.0, 500.0),
             (21, 10.0, 420.0), (13, 5.0, 380.0), (12, 1.0, 410.0), (12.5, None, None),
             (13, 2.0, 430.0)]  # fmt: skip
    ids: list[str] = []
    for n, (value, wind, da) in enumerate(flown):
        ids.append(fly(store, af, n, hover(value)))
        if wind is not None and da is not None:
            keep_weather(store, ids[-1], wind, da, coverage=0.2 if n == 8 else 0.9)
    whole = series(store, reg, af, VIBE, "hover")
    assert whole["conditions"] | {"wind_choices": None} == {
        "wind_max_ms": None, "like_density": False, "density_altitude_m": None,
        "reference": None, "total": 9, "matched": 9, "no_record": 2, "wind_choices": None,
    }  # fmt: skip
    calm = series(store, reg, af, VIBE, "hover", conditions=Conditions(wind_max_ms=6))
    assert [p["value"] for p in calm["points"]] == [12, 13, 12.5, 13, 12]
    assert calm["conditions"]["matched"] == 5 and calm["conditions"]["no_record"] == 2
    assert calm["baseline"] is not None and calm["baseline"]["high"] <= 13
    assert whole["baseline"] is not None and whole["baseline"]["high"] > 15
    like = series(store, reg, af, VIBE, "hover", flight_id=ids[0],
                  conditions=Conditions(like_density=True))  # fmt: skip
    assert like["conditions"]["reference"] == ids[0]
    assert like["conditions"]["density_altitude_m"] == [100, 700]
    assert [p["flight"] for p in like["points"]] == [ids[i] for i in (0, 1, 3, 4, 5, 6)]
    both = series(store, reg, af, VIBE, "hover",
                  conditions=Conditions(wind_max_ms=3, like_density=True))  # fmt: skip
    assert both["conditions"]["reference"] == ids[6]  # the latest with a usable record
    assert [p["flight"] for p in both["points"]] == [ids[0], ids[3], ids[6]]
    assert whole["conditions"]["wind_choices"] == [3, 6, 9]


def test_like_density_with_no_record_matches_nothing(store: Store, reg: Registry) -> None:
    af = airframe(store)
    for n, v in enumerate([12, 13]):
        fly(store, af, n, hover(v))
    s = series(store, reg, af, VIBE, "hover", conditions=Conditions(like_density=True))
    assert s["points"] == [] and s["baseline"] is None
    assert (s["conditions"]["matched"], s["conditions"]["no_record"]) == (0, 2)


def test_drift_is_flagged_inside_the_target_zone(store: Store, reg: Registry) -> None:
    af = airframe(store)
    for n, v in enumerate([12, 13, 12.5, 14, 13, 12, 22, 24]):  # the last two still target
        fly(store, af, n, hover(v))
    s = series(store, reg, af, VIBE, "hover")
    assert s["drift"] is not None and len(s["drift"]["flights"]) == 2
    assert all(p["zone"] == "target" for p in s["points"])
    [card] = overview(store, reg, af)
    assert card["drift"] and card["since_previous"] == 2 and card["value"] == 24


def test_one_bad_flight_is_not_drift_and_better_is_never_drift(store: Store, reg: Registry) -> None:
    af = airframe(store)
    for n, v in enumerate([12, 13, 12.5, 14, 13, 12, 13, 22]):
        fly(store, af, n, hover(v))
    assert series(store, reg, af, VIBE, "hover")["drift"] is None
    other = airframe(store, "HL2")
    for n, v in enumerate([12, 13, 12.5, 14, 13, 12, 5, 4]):  # lower vibration is better
        fly(store, other, n, hover(v))
    assert series(store, reg, other, VIBE, "hover")["drift"] is None


def test_flights_with_problems_stay_out_of_the_baseline(store: Store, reg: Registry) -> None:
    af = airframe(store)
    for n, v in enumerate([12, 13, 12.5, 14, 13]):
        fly(store, af, n, hover(v))
    fly(store, af, 5, hover(70), bad=True)
    s = series(store, reg, af, VIBE, "hover")
    assert s["baseline"] is not None and s["baseline"]["flights"] == 5
    assert s["baseline"]["high"] < 15


def test_like_with_like_by_payload_and_segment(store: Store, reg: Registry) -> None:
    af = airframe(store)
    for n in range(6):
        fly(store, af, 2 * n, hover(12) | {(VIBE, "flight"): 18}, payload="none")
        fly(store, af, 2 * n + 1, hover(25), payload="10 kg")
    # No hover thrust measured: each flight is compared within its payload label.
    light = series(store, reg, af, VIBE, "hover", "label:none")
    heavy = series(store, reg, af, VIBE, "hover", "label:10 kg")
    assert {p["value"] for p in light["points"]} == {12} and len(heavy["points"]) == 6
    assert heavy["payload"] == {"label": "10 kg"}
    assert len(series(store, reg, af, VIBE, "hover", any_payload=True)["points"]) == 12
    cards = {(c["segment"], c["group"], c["payload"]) for c in overview(store, reg, af)}
    assert cards == {("hover", "label:10 kg", "10 kg")}  # the latest flight's payload only


def test_hover_thrust_bands_flights_whatever_their_label(store: Store, reg: Registry) -> None:
    """Three loads: none, 10% and 25% heavier, labelled loosely. The band follows the
    weight, so the same label on two loads splits and two labels for one load join."""
    af = airframe(store)
    plan = [(30.0, "none"), (30.6, "camera"), (33.1, "camera"), (37.6, "camera"),
            (29.7, None), (33.0, "10 percent"), (37.4, "heavy"), (30.2, "none")]  # fmt: skip
    flights = [fly(store, af, n, hover(10 + n), payload=label, thrust=thrust)
               for n, (thrust, label) in enumerate(plan)]  # fmt: skip
    groups = payloads(store, reg, af)
    assert [g["group"] for g in groups["groups"]] == ["band:0", "band:1", "band:2"]
    by = {g["group"]: g for g in groups["groups"]}
    assert by["band:0"]["flights"] == 4 and by["band:0"]["above_lightest"] == 0
    assert (
        by["band:1"]["labels"] == ["camera", "10 percent"] and by["band:1"]["above_lightest"] == 10
    )
    assert by["band:2"]["above_lightest"] == 21 and by["band:2"]["kg"] is None  # nothing weighed
    assert groups["latest"] == "band:0" and groups["lightest"] == pytest.approx(30.1, abs=0.01)
    s = series(store, reg, af, VIBE, "hover")  # the latest flight's band
    assert s["group"] == "band:0" and [p["value"] for p in s["points"]] == [10, 11, 14, 17]
    assert s["payload"]["band"] == 0 and s["payload"]["low"] < 30.0 < s["payload"]["high"]
    heavy = series(store, reg, af, VIBE, "hover", "band:2")
    assert [p["value"] for p in heavy["points"]] == [13, 16]
    third = series(store, reg, af, VIBE, "hover", flight_id=flights[3])  # the report's view
    assert third["group"] == "band:2" and third["points"] == heavy["points"]


def test_one_weighed_flight_turns_bands_into_kilograms(store: Store, reg: Registry) -> None:
    af = airframe(store)
    fly(store, af, 0, hover(10), thrust=30.0, takeoff_kg=24.0)  # 0.8 kg a percent
    fly(store, af, 1, hover(11), thrust=36.0)
    groups = payloads(store, reg, af)
    assert groups["kg_per_percent"] == pytest.approx(0.8)
    by = {g["group"]: g for g in groups["groups"]}
    low, high = by["band:2"]["kg"]
    assert low < 36.0 * 0.8 < high
    assert groups["check"] == []  # one weighed flight checks nothing
    [_, heavy] = series(store, reg, af, VIBE, "hover", any_payload=True)["points"]
    assert heavy["kg"] == pytest.approx(28.8) and heavy["takeoff_kg"] is None


def test_weighed_flights_check_the_estimate_against_the_scale(store: Store, reg: Registry) -> None:
    """Weighed light and loaded: each weight is estimated from the other's."""
    af = airframe(store)
    light = fly(store, af, 0, hover(10), thrust=30.0, takeoff_kg=24.0)
    loaded = fly(store, af, 1, hover(11), thrust=37.5, takeoff_kg=29.4)  # the scale says 29.4
    check = {c["flight"]: c for c in payloads(store, reg, af)["check"]}
    assert check[loaded]["estimated_kg"] == pytest.approx(30.0)  # 37.5 x 0.8
    assert check[loaded]["error_pct"] == pytest.approx(2.0)
    assert check[light]["estimated_kg"] == pytest.approx(30.0 * 29.4 / 37.5, abs=0.01)


def test_the_lightest_flights_set_the_bands_not_one_light_outlier() -> None:
    rules = {"band_step": 0.1, "lightest_within": 0.05}
    b = bands([Flown("a", 29.0, None), Flown("b", 30.0, None), Flown("c", 30.5, None),
               Flown("d", 36.4, None), Flown("e", None, 20.0)], rules)  # fmt: skip
    assert b.lightest == pytest.approx(29.5)  # the median of 29 and 30, within 5% of 29
    assert b.band == {"a": 0, "b": 0, "c": 0, "d": 2}  # e has no hover thrust: no band
    assert bands([], rules).lightest is None


def test_the_event_lane(store: Store, reg: Registry) -> None:
    af = airframe(store)
    first = fly(store, af, 0, hover(12))
    second = fly(store, af, 1, hover(12))
    third = fly(store, af, 2, hover(12), firmware="4.7.2")
    store.insert(
        "change_set",
        {"airframe_id": af, "kind": "parameter", "origin": "loaded file",
         "before_flight_id": first, "after_flight_id": second,
         "details_json": json.dumps({"changes": [
             {"name": "INS_HNTCH_FREQ"},
             {"name": "INS_HNTCH_BW", "before": 40.0, "after": 20.0, "class": "tuning"},
         ]})},
    )  # fmt: skip
    found: list[dict[str, Any]] = events(store, af)
    assert [(e["kind"], e["flight"]) for e in found] == [
        ("parameter change", second), ("firmware", third),
    ]  # fmt: skip
    assert found[0]["params"] == ["INS_HNTCH_FREQ", "INS_HNTCH_BW"]
    # Each change with its values, for the history: a record with none still reads.
    assert found[0]["changes"] == [
        {"name": "INS_HNTCH_FREQ", "before": None, "after": None, "class": None},
        {"name": "INS_HNTCH_BW", "before": 40.0, "after": 20.0, "class": "tuning"},
    ]
    assert (found[1]["from"], found[1]["to"]) == ("4.7.0", "4.7.2")


def test_the_platform_view_marks_an_airframe_drifting_from_its_siblings(
    store: Store, reg: Registry
) -> None:
    pid = create_platform(store, load_schema(load_pack()), "HL", "copter")
    names = {"HL1": 12.0, "HL2": 13.0, "HL3": 12.5, "HL4": 26.0}
    ids = {name: airframe(store, name, pid) for name in names}
    for name, value in names.items():
        fly(store, ids[name], 0, hover(value))
    view = platform(store, reg, pid, VIBE, "hover")
    apart = {r["airframe"]: r["apart"] for r in view["airframes"]}
    assert apart == {ids["HL1"]: False, ids["HL2"]: False, ids["HL3"]: False, ids["HL4"]: True}


def test_the_flight_list_is_newest_first_with_its_worst_finding(store: Store) -> None:
    af = airframe(store)
    first = fly(store, af, 0, hover(12))
    second = fly(store, af, 1, hover(70), bad=True)
    listed = flight_list(store, airframe_id=af)
    assert [f["id"] for f in listed] == [second, first]
    assert listed[0]["analysis"]["worst"] == "out" and listed[1]["analysis"]["worst"] is None
    assert listed[0]["airframe"] == "HL1" and listed[0]["utc_us"] is not None
    assert flight_list(store, airframe_id="none") == []
    assert [f["id"] for f in flight_list(store, flight_id=first)] == [first]


def test_the_fleet_overview_puts_the_most_urgent_first(store: Store, reg: Registry) -> None:
    pack = load_pack()
    pid = create_platform(store, load_schema(pack), "HL", "copter")
    quiet, steady, drifting, broken = (
        airframe(store, n, pid) for n in ("HL0", "HL1", "HL2", "HL3")
    )
    for n in range(6):
        fly(store, steady, n, hover(12) | {(VIBE, "flight"): 18})
    for n, v in enumerate([12, 13, 12.5, 14, 13, 12, 22, 24]):
        fly(store, drifting, n, hover(v))
    fly(store, broken, 0, hover(12))
    last = fly(store, broken, 1, hover(70), bad=True)
    row = store.get("airframe", broken)
    assert row is not None
    store.update("airframe", broken, row["version"], {"team": "Team B"})
    run = store.conn.execute("SELECT id FROM analysis_run WHERE flight_id = ?", (last,)).fetchone()
    store.insert(
        "finding",
        {"run_id": run[0], "flight_id": last, "analyzer": "esc", "code": "esc.hot",
         "severity": "caution", "confidence": "high", "evidence_json": "[]"},
    )  # fmt: skip
    view = fleet_overview(store, reg, pack)
    cards = view["airframes"]
    assert cards[quiet] == {"status": "none", "flights": 0, "team": None}
    assert cards[broken]["team"] == "Team B" and cards[steady]["team"] is None
    assert cards[steady]["status"] == "ok" and cards[steady]["flights"] == 6
    assert cards[drifting]["status"] == "inspect" and cards[broken]["status"] == "fix"
    assert cards[broken]["counts"] == {"out": 1, "caution": 1}
    # The key metrics read the hover segment first; a metric the flight lacks is left out.
    [vibe] = cards[steady]["metrics"]
    assert (vibe["metric"], vibe["segment"], vibe["value"]) == (VIBE, "hover", 12)
    assert cards[drifting]["metrics"][0]["drift"]
    assert cards[steady]["stage"] is None  # no parameters on these flights: no plan
    assert not cards[steady]["planned"]
    assert cards[broken]["last"]["flight"] == last
    order = [(a["airframe"], a["kind"], a["severity"]) for a in view["attention"]]
    assert order == [  # by severity, then newest first: HL2 flew last
        ("HL3", "finding", "out"), ("HL2", "drift", "caution"), ("HL3", "finding", "caution"),
    ]  # fmt: skip
    assert view["attention"][1]["metric"] == VIBE
    assert view["attention"][0]["team"] == "Team B"  # each item says whose aircraft


def test_the_key_metrics_are_known_metrics(reg: Registry) -> None:
    rules = reg.rules["trends"]
    for metrics in rules["key_metrics"].values():
        assert len(metrics) == 3 and all(m in reg.metrics for m in metrics)


def test_trends_over_rpc(tmp_path: Path) -> None:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    fixture = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
    out = Output()
    server = build_server(out, load_pack())
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys())
    pl = call(server, out, 2, "platform.create", name="Quad", vehicle="copter")["result"]
    af = call(server, out, 3, "airframe.create", platform_id=pl["id"], name="Q-1")["result"]
    scanned = call(server, out, 4, "import.scan", paths=[str(fixture / "copter-quad-hover.bin")])[
        "result"
    ]
    [done] = call(
        server, out, 5, "import.commit", batch=scanned["batch"],
        decisions=[{"log": "f1", "airframe_id": af["id"], "takeoff_kg": 1.6}],
    )["result"]  # fmt: skip
    call(server, out, 6, "analysis.run", flight_id=done["flights"][0])
    cards = call(server, out, 7, "trend.overview", airframe_id=af["id"])["result"]
    assert cards and all(c["baseline"] is None for c in cards)  # one flight: no baseline yet
    card = cards[0]
    s = call(server, out, 8, "trend.series", airframe_id=af["id"], metric=card["metric"],
             segment=card["segment"], group=card["group"])["result"]  # fmt: skip
    assert [p["value"] for p in s["points"]] == [card["value"]]
    # The quad hovered long enough to be weighed: its group is the lightest band.
    groups = call(server, out, 15, "trend.payloads", airframe_id=af["id"])["result"]
    assert card["group"] == groups["latest"] == "band:0"
    assert groups["kg_per_percent"] == pytest.approx(1.6 / groups["lightest"], rel=1e-3)
    for bad_group in ("band:x", "heavy", 3):
        wrong = call(server, out, 16, "trend.series", airframe_id=af["id"], metric=card["metric"],
                     segment=card["segment"], group=bad_group)  # fmt: skip
        assert wrong["error"]["code"] == -32602
    # narrowed to like weather: the flight has no weather record, so nothing matches
    calm = call(server, out, 19, "trend.series", airframe_id=af["id"], metric=card["metric"],
                segment=card["segment"], group=card["group"], wind_max_ms=6,
                like_density=True)["result"]  # fmt: skip
    assert calm["points"] == [] and calm["conditions"]["no_record"] == 1
    assert (calm["conditions"]["matched"], calm["conditions"]["total"]) == (0, 1)
    for bad_weather in ({"wind_max_ms": "calm"}, {"wind_max_ms": -2}, {"wind_max_ms": True},
                        {"like_density": "yes"}):  # fmt: skip
        wrong = call(server, out, 20, "trend.series", airframe_id=af["id"], metric=card["metric"],
                     segment=card["segment"], **bad_weather)  # fmt: skip
        assert wrong["error"]["code"] == -32602
    assert call(server, out, 9, "trend.events", airframe_id=af["id"])["result"] == []
    assert call(server, out, 17, "trend.attribution", airframe_id=af["id"])["result"] == []
    bad = call(server, out, 18, "trend.attribution", airframe_id=af["id"], metric="tune.feel")
    assert bad["error"]["code"] == -32602
    view = call(server, out, 10, "trend.platform", platform_id=pl["id"], metric=card["metric"],
                segment=card["segment"])["result"]  # fmt: skip
    assert [r["airframe"] for r in view["airframes"]] == [af["id"]]
    [listed] = call(server, out, 13, "flight.list", airframe_id=af["id"])["result"]
    assert listed["id"] == done["flights"][0] and listed["analysis"]["status"] == "complete"
    assert listed["takeoff_kg"] == 1.6
    fleet = call(server, out, 14, "fleet.overview")["result"]
    mine = fleet["airframes"][af["id"]]
    assert mine["last"]["flight"] == done["flights"][0] and mine["metrics"]
    assert mine["planned"]  # the fixture has parameters: its stage is read from its plan
    assert mine["stage"] is None or isinstance(mine["stage"], str)
    bad = call(server, out, 11, "trend.series", airframe_id=af["id"], metric="no.such")
    assert bad["error"]["code"] == -32602
    call(server, out, 12, "engine.shutdown")
