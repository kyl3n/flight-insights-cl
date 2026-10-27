"""The Tune view (spec: User interface, "Tune"): a flight's tracking error against a
tune-check flight of the same payload group, flown before the last change that could
move the tune; the responses and stick inputs from both logs; and the plan's verdict."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_gate_grading import (
    AFTER_LIGHT,
    AFTER_LOADED,
    BASE_LIGHT,
    BASE_LOADED,
    LIGHT,
    LOADED,
    PARAMS,
    Check,
    Gate,
    baseline_and_autotune,
    gate,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # the fixture
    reg,  # noqa: F401  # pyright: ignore[reportUnusedImport]  # the fixture
)

from flight_insights.analyzers.flight import FlightData, FlightError
from flight_insights.history import save_snapshot
from flight_insights.history.snapshot import Snapshot
from flight_insights.recommend import grade_flight
from flight_insights.tune_view import TuneViewError, shown, tune

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
TUNED = {**PARAMS, "ATC_RAT_RLL_P": 0.21, "ATC_RAT_PIT_P": 0.22, "ATC_ANG_RLL_P": 12.0}


def no_log(flight_id: str) -> FlightData:
    raise FlightError(f"{flight_id}: no log in the cache")


def view(g: Gate, flight: str, **kw: Any) -> dict[str, Any]:
    return tune(g.store, g.pack, g.reg, flight, no_log, **kw)


def tuned(g: Gate) -> str:
    """AutoTune's gains, as the flights after it carry them."""
    return save_snapshot(g.store, Snapshot(TUNED, "copter", "4.7.0", "log start", "primary"),
                         g.pack)  # fmt: skip


def gate_flights(g: Gate) -> list[str]:
    """F1 light and F2 loaded before, F3 and F4 AutoTune, F5 light and F6 loaded after."""
    first, a1 = g.fly(LIGHT, BASE_LIGHT)
    g.plan(first, a1)
    second, a2 = g.fly(LOADED, BASE_LOADED)
    g.plan(second, a2)
    for axis in ("roll", "pitch"):
        f, a = g.fly(LIGHT, saved=axis)
        grade_flight(g.store, g.pack, f, a)
    after = tuned(g)
    f5, a5 = g.fly(LIGHT, AFTER_LIGHT, snapshot=after)
    grade_flight(g.store, g.pack, f5, a5)
    f6, a6 = g.fly(LOADED, AFTER_LOADED, snapshot=after)
    grade_flight(g.store, g.pack, f6, a6)
    return [first, second, f5, f6]


def test_a_flight_is_compared_within_its_payload_group_before_the_tune_changed(
    gate: Gate,  # noqa: F811
) -> None:
    f1, f2, f5, f6 = gate_flights(gate)
    v = view(gate, f5)
    assert v["group"] == v["own_group"] == "band:0"
    assert [g["group"] for g in v["groups"]] == ["band:0", "band:2"]
    assert [g["tune_checks"] for g in v["groups"]] == [2, 2]  # AutoTune flights measure none
    assert v["this"]["id"] == f5
    assert (v["compare"]["id"], v["compare"]["why"]) == (f1, "before_change")
    assert [c["id"] for c in v["candidates"]] == [f1]  # never the loaded flights
    assert v["candidates"][0]["before"] and not v["candidates"][0]["same_tune"]

    rows = {r["metric"]: r for r in v["rows"]}
    roll = rows["tune.attitude_error_roll"]
    assert roll["this"] == {"value": 28.0, "low": 26.0, "high": 30.0, "events": 12.0}
    assert roll["compare"]["value"] == 40.0
    assert roll["change"] == pytest.approx(-0.3) and roll["apart"] and roll["verdict"] == "better"
    rate = rows["tune.tracking_error_roll"]
    assert rate["change"] == 0.0 and rate["apart"] is False and rate["verdict"] == "unchanged"
    assert [r["family"] for r in v["rows"]][:2] == [
        "attitude_error"
    ] * 2  # the gate's measure first
    assert v["needs"] == {"attitude_error": {"check": "metric_improves", "by": 0.2},
                          "tracking_error": {"check": "metric_not_worse", "by": 0.2}}  # fmt: skip
    assert v["figures"]["rise_time"] == {"this": {"value": 0.09}, "compare": {"value": 0.1}}
    assert v["this"]["inputs"]["why"].endswith("no log in the cache")  # the metrics still show

    gate_view = v["gate"]
    assert gate_view["status"] == "confirmed" and gate_view["graded_flight"] == f6
    attitude = next(c for c in gate_view["checks"] if c.get("metric") == "tune.attitude_error_roll")
    assert attitude["passed"] and [b["verdict"] for b in attitude["bands"]] == ["better"] * 2
    assert {f1, f2, f5, f6} <= set(v["labels"])
    assert v["labels"][f2]["group"] == "band:2"
    assert [s["kind"] for s in v["card"]] == ["ground_check", "fly", "abort"]


def test_another_band_stands_for_the_flight_flown_on_the_same_tune(
    gate: Gate,  # noqa: F811
) -> None:
    _, f2, f5, f6 = gate_flights(gate)
    loaded = view(gate, f5, group="band:2")
    assert loaded["own_group"] == "band:0" and loaded["group"] == "band:2"
    assert loaded["this"]["id"] == f6 and loaded["compare"]["id"] == f2
    rows = {r["metric"]: r for r in loaded["rows"]}
    assert rows["tune.attitude_error_pitch"]["verdict"] == "better"
    assert view(gate, f6, group="band:0")["this"]["id"] == f5


def test_a_band_not_flown_on_this_tune_has_nothing_to_compare(gate: Gate) -> None:  # noqa: F811
    baseline_and_autotune(gate)
    f5, a5 = gate.fly(LIGHT, AFTER_LIGHT, snapshot=tuned(gate))
    grade_flight(gate.store, gate.pack, f5, a5)
    loaded = view(gate, f5, group="band:2")
    assert loaded["this"] is None and loaded["compare"] is None and loaded["rows"] == []
    assert loaded["gate"]["status"] == "applied" and loaded["gate"]["pending"]
    waiting = next(
        c for c in loaded["gate"]["checks"] if c.get("metric") == "tune.attitude_error_roll"
    )
    assert waiting["passed"] is None and waiting["bands"][1]["verdict"] == "waiting"


def test_a_person_picks_the_comparison_within_the_group(gate: Gate) -> None:  # noqa: F811
    f1, f2, f5, _ = gate_flights(gate)
    f7, _ = gate.fly(LIGHT, AFTER_LIGHT, snapshot=tuned(gate), offer=False)
    default = view(gate, f7)
    assert default["compare"]["id"] == f1 and default["compare"]["why"] == "before_change"
    same = next(c for c in default["candidates"] if c["id"] == f5)
    assert same["same_tune"] and same["before"]
    picked = view(gate, f7, compare_id=f5)
    assert picked["compare"]["id"] == f5 and picked["compare"]["why"] == "chosen"
    assert {r["verdict"] for r in picked["rows"]} == {"unchanged"}  # the same tune again
    with pytest.raises(TuneViewError, match="same payload group"):
        view(gate, f7, compare_id=f2)  # loaded: never against a light flight


def test_with_no_other_tune_check_there_is_nothing_to_compare(gate: Gate) -> None:  # noqa: F811
    f1, a1 = gate.fly(LIGHT, BASE_LIGHT)
    gate.plan(f1, a1)
    v = view(gate, f1)
    assert v["this"]["id"] == f1 and v["compare"] is None and v["candidates"] == []
    assert all(r["compare"] is None and "verdict" not in r for r in v["rows"])
    assert v["gate"]["status"] == "proposed" and not v["gate"]["pending"]
    assert all(c["passed"] is None for c in v["gate"]["checks"])  # nothing flown since


def test_a_flight_with_no_tune_check_still_shows_its_group(gate: Gate) -> None:  # noqa: F811
    f1, a1 = gate.fly(LIGHT, BASE_LIGHT)
    gate.plan(f1, a1)
    f2, _ = gate.fly(LIGHT, saved="roll")
    v = view(gate, f2)
    assert v["this"]["id"] == f2 and v["compare"]["id"] == f1
    assert all(r["this"] is None for r in v["rows"])


def test_what_the_view_refuses(gate: Gate) -> None:  # noqa: F811
    f1, _ = gate.fly(LIGHT, Check(roll=(1, 0, 2), pitch=(1, 0, 2)))
    with pytest.raises(TuneViewError, match="axis"):
        view(gate, f1, axis="throttle")
    with pytest.raises(TuneViewError, match="payload group"):
        view(gate, f1, group="band:9")
    with pytest.raises(TuneViewError, match="no such flight"):
        view(gate, "nope")


def test_the_view_shows_every_tune_metric_on_each_axis(gate: Gate) -> None:  # noqa: F811
    names = shown(gate.reg)
    assert {"tune.attitude_error_roll", "tune.tracking_error_yaw", "tune.rise_time_pitch",
            "tune.sensitivity_peak_yaw"} <= names  # fmt: skip
    assert "tune.attitude_error_yaw" not in names  # none for yaw


# --- the simulator's tune checks, over RPC -------------------------------------------------


@pytest.fixture(scope="module")
def pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, Any, dict[str, str]]:
    """The simulated quad's tune check on its default gains and on soft ones (x0.4)."""
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.knowledge import load_pack
    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, load_pack())
    data = tmp_path_factory.mktemp("tune")
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(data), keys=keys())
    pl = call(server, out, 2, "platform.create", name="Quad", vehicle="copter")["result"]
    af = call(server, out, 3, "airframe.create", platform_id=pl["id"], name="Q-1")["result"]
    flights: dict[str, str] = {}
    for n, name in enumerate(("tune-low", "tune-default")):
        log = FIXTURES / f"copter-quad-{name}.bin"
        scanned = call(server, out, 10 + n, "import.scan", paths=[str(log)])["result"]
        [done] = call(
            server, out, 20 + n, "import.commit", batch=scanned["batch"],
            decisions=[{"log": "f1", "airframe_id": af["id"]}],
        )["result"]  # fmt: skip
        flights[name] = done["flights"][0]
        call(server, out, 30 + n, "analysis.run", flight_id=flights[name])
    return server, out, flights


def test_the_simulator_pair_draws_both_flights(pair: tuple[Any, Any, dict[str, str]]) -> None:
    from test_rpc import call

    server, out, flights = pair
    views = [
        call(server, out, 40 + n, "flight.tune", flight_id=fid)["result"]
        for n, fid in enumerate(flights.values())
    ]
    # Whichever flew later is compared with the other, flown on different gains.
    [v] = [x for x in views if x["compare"] is not None]
    assert v["compare"]["why"] == "before_change"
    assert {v["this"]["id"], v["compare"]["id"]} == set(flights.values())
    roll = next(r for r in v["rows"] if r["metric"] == "tune.attitude_error_roll")
    values = {v["this"]["id"]: roll["this"]["value"], v["compare"]["id"]: roll["compare"]["value"]}
    assert values[flights["tune-default"]] < values[flights["tune-low"]]  # closer on its own gains
    assert roll["verdict"] in ("better", "worse")  # x0.4 gains: far apart either way

    for key in ("this", "compare"):
        inputs, curves = v[key]["inputs"], v[key]["curves"]
        assert inputs["rate_hz"] >= 200 and inputs["armed_s"] > 30
        roll = inputs["axes"]["roll"]
        assert sum(e["counted"] for e in roll) >= 6
        assert all(0 <= e["start_s"] < e["end_s"] <= inputs["armed_s"] for e in roll)
        assert all(e["reason"] is None for e in roll if e["counted"])
        freq = curves["frequency"]
        assert freq["f"] == sorted(freq["f"]) and max(freq["f"]) <= freq["measured_to_hz"]
        assert len(freq["f"]) == len(freq["db"]) > 5
    step = v["this"]["curves"]["step"]
    if step is not None:  # measured past the bandwidth
        assert len(step["t"]) == len(step["y"]) == len(step["low"]) == len(step["high"])
        assert all(lo <= hi for lo, hi in zip(step["low"], step["high"], strict=True))
        assert "low" not in (v["compare"]["curves"]["step"] or {})  # the interval: this flight only


def test_the_rpc_refuses_what_it_cannot_show(pair: tuple[Any, Any, dict[str, str]]) -> None:
    from test_rpc import call

    server, out, flights = pair
    bad_axis = call(server, out, 50, "flight.tune", flight_id=flights["tune-low"], axis="up")
    assert bad_axis["error"]["code"] == -32602
    missing = call(server, out, 51, "flight.tune", flight_id="no-such-flight")
    assert "no such flight" in missing["error"]["message"]
    wrong = call(server, out, 52, "flight.tune", flight_id=flights["tune-low"], compare_id="x")
    assert "same payload group" in wrong["error"]["message"]
