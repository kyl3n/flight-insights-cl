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
from flight_insights.platforms.records import create_airframe, create_platform
from flight_insights.platforms.schema import load_schema
from flight_insights.store import Store, StoreKeys, open_store
from flight_insights.trends import baseline, events, flight_list, overview, platform, series

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
) -> str:
    """Flight number n of the airframe, analysed, with these metric values."""
    fid = store.insert(
        "flight",
        {"airframe_id": airframe_id, "firmware": firmware, "payload": payload,
         "boot_utc_us": T0 + n * 86_400_000_000, "arm_us": 1_000_000},
    )  # fmt: skip
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
    light = series(store, reg, af, VIBE, "hover", "none")
    heavy = series(store, reg, af, VIBE, "hover", "10 kg")
    assert {p["value"] for p in light["points"]} == {12} and len(heavy["points"]) == 6
    assert len(series(store, reg, af, VIBE, "hover", any_payload=True)["points"]) == 12
    cards = {(c["segment"], c["payload"]) for c in overview(store, reg, af)}
    assert cards == {("hover", "10 kg")}  # the latest flight's payload only


def test_the_event_lane(store: Store, reg: Registry) -> None:
    af = airframe(store)
    first = fly(store, af, 0, hover(12))
    second = fly(store, af, 1, hover(12))
    third = fly(store, af, 2, hover(12), firmware="4.7.2")
    store.insert(
        "change_set",
        {"airframe_id": af, "kind": "parameter", "origin": "loaded file",
         "before_flight_id": first, "after_flight_id": second,
         "details_json": json.dumps({"changes": [{"name": "INS_HNTCH_FREQ"}]})},
    )  # fmt: skip
    found: list[dict[str, Any]] = events(store, af)
    assert [(e["kind"], e["flight"]) for e in found] == [
        ("parameter change", second), ("firmware", third),
    ]  # fmt: skip
    assert found[0]["params"] == ["INS_HNTCH_FREQ"]
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
        decisions=[{"log": "f1", "airframe_id": af["id"]}],
    )["result"]  # fmt: skip
    call(server, out, 6, "analysis.run", flight_id=done["flights"][0])
    cards = call(server, out, 7, "trend.overview", airframe_id=af["id"])["result"]
    assert cards and all(c["baseline"] is None for c in cards)  # one flight: no baseline yet
    card = cards[0]
    s = call(server, out, 8, "trend.series", airframe_id=af["id"], metric=card["metric"],
             segment=card["segment"], payload=card["payload"])["result"]  # fmt: skip
    assert [p["value"] for p in s["points"]] == [card["value"]]
    assert call(server, out, 9, "trend.events", airframe_id=af["id"])["result"] == []
    view = call(server, out, 10, "trend.platform", platform_id=pl["id"], metric=card["metric"],
                segment=card["segment"])["result"]  # fmt: skip
    assert [r["airframe"] for r in view["airframes"]] == [af["id"]]
    [listed] = call(server, out, 13, "flight.list", airframe_id=af["id"])["result"]
    assert listed["id"] == done["flights"][0] and listed["analysis"]["status"] == "complete"
    bad = call(server, out, 11, "trend.series", airframe_id=af["id"], metric="no.such")
    assert bad["error"]["code"] == -32602
    call(server, out, 12, "engine.shutdown")
