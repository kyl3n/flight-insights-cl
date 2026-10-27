"""The Navigation and GNSS view (spec: User interface, "The Navigation and GNSS view") on
the ArduCopter 4.7.0 SITL GNSS flights: jamming, a spoofed step and a spoofed drift, a
GNSS-off drill with no other position source and one on optical flow. Each event and
outage says how the aircraft coped, with a status only where the engine has a verdict;
the reply holds no position and no number the analysis did not store."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
FLIGHTS = {"copter-quad-gnss-jam": "Q-01", "copter-quad-gnss-step": "Q-01",
           "copter-quad-gnss-ramp": "Q-01", "copter-quad-nognss-drill": "Q-01",
           "copter-quad-nognss-flow": "Q-02"}  # fmt: skip
POSITION_KEYS = {"lat", "lng", "lon", "alt", "latitude", "longitude", "altitude", "relhomealt",
                 "reloriginalt", "position_m", "east_m", "north_m"}  # fmt: skip

Views = dict[str, tuple[dict[str, Any], dict[str, Any]]]


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, Any, Views, dict[str, Any]]:
    """Each flight's view and its analysis, and each airframe's recovery card."""
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.knowledge import load_pack
    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, load_pack())
    data = tmp_path_factory.mktemp("navigation")
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(data), keys=keys())
    pl = call(server, out, 2, "platform.create", name="Quad", vehicle="copter")["result"]
    frames = {name: call(server, out, 3 + n, "airframe.create", platform_id=pl["id"],
                         name=name)["result"]["id"]
              for n, name in enumerate(sorted(set(FLIGHTS.values())))}  # fmt: skip
    views: Views = {}
    for n, (log, frame) in enumerate(FLIGHTS.items()):
        scanned = call(server, out, 10 + n, "import.scan", paths=[str(FIXTURES / f"{log}.bin")])
        [done] = call(
            server, out, 20 + n, "import.commit", batch=scanned["result"]["batch"],
            decisions=[{"log": "f1", "airframe_id": frames[frame]}],
        )["result"]  # fmt: skip
        fid = done["flights"][0]
        if n == 0:  # a flight not analysed yet has no view
            early = call(server, out, 29, "flight.navigation", flight_id=fid)
            assert "not been analysed" in early["error"]["message"]
        analysis = call(server, out, 30 + n, "analysis.run", flight_id=fid)["result"]
        view = call(server, out, 40 + n, "flight.navigation", flight_id=fid)["result"]
        views[log] = (view, analysis)
    cards = {name: call(server, out, 50 + n, "airframe.recovery_card", airframe_id=af)["result"]
             for n, (name, af) in enumerate(frames.items())}  # fmt: skip
    return server, out, views, cards


def rows(coping: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """Each row's status and verdict, by its id."""
    return {r["id"]: (r["status"], r["verdict"]) for r in coping}


def row(coping: list[dict[str, Any]], name: str) -> dict[str, Any]:
    [found] = [r for r in coping if r["id"] == name]
    return found


def test_jamming_and_how_the_aircraft_coped(fleet: tuple[Any, Any, Views, Any]) -> None:
    view = fleet[2]["copter-quad-gnss-jam"][0]
    [event] = view["events"]
    assert (event["class"], event["confidence"], event["receivers"]) == ("jamming", "medium", [1])
    assert event["finding"]["code"] == "gnss.jamming"
    assert (event["start_s"], event["end_s"]) == (23.5, 59.2)  # after arming
    assert event["peaks"]["fewest_sats"] == 3 and event["peaks"]["highest_test_ratio"] > 1
    # the largest gap and reset, as whole relative metres, as the evidence holds them
    assert set(event["position"]) == {"largest_track_gap_m", "estimator_resets", "largest_reset_m"}
    assert event["outages"] == [1, 2]  # GNSS went twice while it was jammed
    got = rows(event["coping"])
    assert got["estimator"] == ("info", "rejected")
    assert got["source"] == ("out", "none")  # no source set keeps a position without GNSS
    assert got["hold"] == ("out", "lost")  # the shorter: 5.5 s, under the 10 s the zone allows
    assert row(event["coping"], "hold")["values"]["kept_s"] == 5.5
    assert got["resets"] == ("info", "reset")
    failsafe = row(event["coping"], "failsafe")
    assert failsafe["status"] == "ok" and failsafe["verdict"] == "judged"  # as configured
    assert {j["finding"]["code"] for j in failsafe["values"]["judged"]} == {
        "failsafe.as_configured"
    }
    assert row(event["coping"], "mode")["values"] == {"mode": "LAND", "reason": "EKF_FAILSAFE"}
    assert got["heading"] == ("ok", "ready")  # the compass, which GNSS does not drive
    assert [o["events"] for o in view["outages"]] == [[1], [1]]


def test_a_spoofed_step_and_a_drift_the_estimator_followed(
    fleet: tuple[Any, Any, Views, Any],
) -> None:
    [step] = fleet[2]["copter-quad-gnss-step"][0]["events"]
    assert step["class"] == "spoofing" and step["finding"]["code"] == "gnss.spoofing"
    got = rows(step["coping"])
    assert got["estimator"] == ("info", "rejected")
    assert row(step["coping"], "resets")["values"] == {"count": 2, "largest_m": 60}
    assert "hold" not in got  # GNSS never went
    heading = row(step["coping"], "heading")
    assert heading["values"]["no_reset"] is True and heading["status"] == "ok"
    assert row(step["coping"], "mode")["values"]["reason"] == "GCS_COMMAND"

    drift, jump = fleet[2]["copter-quad-gnss-ramp"][0]["events"]
    assert drift["receivers"] == [] and drift["peaks"] == {"largest_innovation_mps": 0.53}
    assert rows(drift["coping"])["estimator"] == ("out", "followed")  # never rejected
    assert rows(jump["coping"])["estimator"] == ("info", "rejected")


def test_drills_with_and_without_another_position_source(
    fleet: tuple[Any, Any, Views, Any],
) -> None:
    alone = fleet[2]["copter-quad-nognss-drill"][0]
    assert alone["events"] == []  # a drill is not a disturbance
    [o] = alone["outages"]
    assert o["drill"] and o["finding"]["code"] == "no_gnss.position_lost"
    got = rows(o["coping"])
    assert got["estimator"] == ("ok", "stopped")  # 3.9 s, inside the 5 s target
    assert got["hold"] == ("out", "lost")
    assert got["failsafe"] == ("ok", "judged")
    assert got["resumed"] == ("info", "resumed")

    flow = fleet[2]["copter-quad-nognss-flow"][0]
    [o] = flow["outages"]
    assert o["finding"]["code"] == "no_gnss.position_held"
    got = rows(o["coping"])
    assert got["estimator"] == ("info", "not_using")
    assert row(o["coping"], "source")["values"] == {"set": 2}
    assert got["source"] == ("ok", "used")
    assert got["hold"] == ("ok", "relative")  # 30.2 s on optical flow
    assert row(o["coping"], "drift")["values"] == {"drift_m": 0.1, "rate": 0.2, "minutes": 43.0,
                                                   "dead_reckoning": False}  # fmt: skip
    assert got["drift"] == ("ok", "measured")
    assert got["failsafe"] == ("info", "none")


def test_receivers_readiness_and_what_would_get_it_home(
    fleet: tuple[Any, Any, Views, Any],
) -> None:
    views = fleet[2]
    jam = views["copter-quad-gnss-jam"][0]
    assert jam["receivers"] == [{
        "instance": 1, "param": "GPS1_TYPE", "type": "AUTO",
        "logged": {"ublox_status": False, "fix": True, "accuracy": True, "gps_time": True},
    }]  # fmt: skip
    assert jam["independent"] == {"estimator": True, "barometer": True, "second_receiver": False}
    says = {r["area"]: (r["status"], r["say"]) for r in jam["readiness"]}
    assert list(says) == [
        "position", "switching", "heading", "height", "wind", "failsafes", "pilot_fallback"
    ]  # fmt: skip
    assert says == {
        "position": ("none", "none"), "switching": ("none", "none"),
        "heading": ("ready", "ready"), "height": ("ready", "barometer"),
        "wind": ("gap", "off"), "failsafes": ("ready", "ekf"),
        "pilot_fallback": ("ready", "modes"),
    }  # fmt: skip
    drill = {r["area"]: r for r in views["copter-quad-nognss-drill"][0]["readiness"]}
    # RC8 selects source sets, but none of them keeps a position without GNSS
    assert (drill["switching"]["say"], drill["switching"]["fill"]) == (
        "switch_nothing", {"channel": 8}
    )  # fmt: skip
    flow = {r["area"]: r for r in views["copter-quad-nognss-flow"][0]["readiness"]}
    assert (flow["switching"]["say"], flow["switching"]["fill"]) == ("switch", {"channel": 8})
    assert flow["position"]["status"] == "ready"
    assert flow["position"]["values"]["usable_sets"] == [2]

    [action] = jam["actions"]
    assert action["finding"]["code"] == "no_gnss.drag_fusion_off"
    assert action["mitigations"] == ["drag_fusion"]  # the mitigation that answers it
    assert [s["kind"] for s in action["steps"]] == ["ground_check", "fly", "abort", "verify"]
    assert all(s["text"]["en"] and s["text"]["uk"] for s in action["steps"])
    assert any("airspeed-estimation" in s for s in jam["sources"])


def _walk(value: Any, key: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for k, v in value.items():  # pyright: ignore[reportUnknownVariableType]
            yield from _walk(v, str(k))  # pyright: ignore[reportUnknownArgumentType]
    elif isinstance(value, list):
        for v in value:  # pyright: ignore[reportUnknownVariableType]
            yield from _walk(v, key)
    yield key, value


def _numbers(value: Any) -> set[float]:
    return {float(v) for _, v in _walk(value)
            if isinstance(v, int | float) and not isinstance(v, bool)}  # fmt: skip


def test_the_reply_holds_no_position_and_only_stored_numbers(
    fleet: tuple[Any, Any, Views, Any],
) -> None:
    """The flight's own numbers come from its analysis; the mitigations' holds and drift
    from the platform's analyses, with counts of outages."""
    fleet_stored = _numbers([[a["evidence"], a["metrics"]] for _, a in fleet[2].values()])
    for log, (view, analysis) in fleet[2].items():
        keys = {k.lower() for k, _ in _walk(view)}
        assert not keys & POSITION_KEYS, log
        assert not any(k.endswith("_us") or "utc" in k for k in keys), log  # no absolute times
        stored = _numbers([analysis["evidence"], analysis["metrics"],
                           [f["params"] for f in analysis["findings"]]])  # fmt: skip
        counted = {float(n) for n in range(1, 1 + len(view["events"]) + len(view["outages"]))}
        own = {k: v for k, v in view.items() if k != "mitigations"}
        unknown = _numbers(own) - stored - counted
        assert not unknown, (log, unknown)
        outages = {float(n) for n in range(1, 1 + sum(len(v["outages"]) for v, _ in
                                                      fleet[2].values()))}  # fmt: skip
        unknown = _numbers(view["mitigations"]) - fleet_stored - outages
        assert not unknown, (log, unknown)


def test_the_goal_and_the_rpc_refusals(fleet: tuple[Any, Any, Views, dict[str, Any]]) -> None:
    from test_rpc import call

    server, out, _, cards = fleet
    for card in cards.values():
        goal = card["goal"]
        # the radio failsafe flies it home, but with GNSS lost too it lands where it is
        assert goal["section"] == "radio_lost" and goal["met"] is False
        assert goal["will"][-1]["key"] == "link.gnss_too"
        assert all("goal" not in s for s in card["sections"])
    missing = call(server, out, 60, "flight.navigation", flight_id="no-such-flight")
    assert "no such flight" in missing["error"]["message"]
    bad = call(server, out, 61, "flight.navigation")
    assert bad["error"]["code"] == -32602


def test_a_dead_reckoned_outage_and_the_soonest_drift() -> None:
    """In 4.7.0 the relative-position flag stays set while the estimator dead-reckons, so
    the hold says dead reckoning only from its own flag; of several drifts the one that
    reached the landing tolerance soonest is shown, however small its metres."""
    from flight_insights.analyzers.registry import Registry
    from flight_insights.knowledge import load_pack
    from flight_insights.navigation_view import _drift, _hold, _soonest  # pyright: ignore[reportPrivateUsage]

    reg = Registry.load(load_pack())
    outage = {"kept_s": 30.0, "absolute_s": 10.0, "duration_s": 30.0, "held": False,
              "kept_relative": True, "dead_reckoning_s": 10.0}  # fmt: skip
    hold = _hold(reg, outage)
    assert (hold["verdict"], hold["values"]["dead_reckoning_s"]) == ("dead_reckoning", 10.0)
    assert _hold(reg, outage | {"dead_reckoning_s": None})["verdict"] == "relative"
    flow = {"drift_m": 3.0, "drift_m_per_min": 6.0, "minutes_to_tolerance": 1.7,
            "dead_reckoning": False}  # fmt: skip
    reckoned = {"drift_m": 2.5, "minutes_to_tolerance": 0.8, "dead_reckoning": True}
    soonest = _soonest([flow, reckoned])
    assert soonest is reckoned
    row = _drift(reg, reckoned)
    assert (row["status"], row["values"]["rate"], row["values"]["dead_reckoning"]) == (
        "caution", None, True,
    )  # fmt: skip
    assert _soonest([{"drift_m": 0.4}, {"drift_m": 1.2}]) == {"drift_m": 1.2}
    assert _soonest([]) is None
