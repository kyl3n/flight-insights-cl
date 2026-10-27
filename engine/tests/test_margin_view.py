"""flight.margin: the Propulsion view's payload and motor-out margin, end to end through
the engine on the simulator's heavy-lift X8, flown light (weighed at import) and loaded
(estimated from the light flight's weight), and on a quad, which has no margin."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_rpc import LAPTOP, Output, call, keys

from flight_insights.knowledge import load_pack
from flight_insights.rpc import build_server

PACK = load_pack()
FIX = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"


@pytest.fixture(scope="module")
def margins(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = Output()
    server = build_server(out, PACK)
    n = iter(range(1, 1000))

    def rpc(method: str, **params: object) -> Any:
        reply = call(server, out, next(n), method, **params)
        assert "result" in reply, reply
        return reply["result"]

    rpc("engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path_factory.mktemp("margin")),
        keys=keys())  # fmt: skip
    pl = rpc("platform.create", name="Callisto", vehicle="copter")
    x8 = rpc("airframe.create", platform_id=pl["id"], name="X8")
    quad = rpc("airframe.create", platform_id=pl["id"], name="quad")
    got: dict[str, Any] = {}
    flights: dict[str, str] = {}
    flown = (("copter-heavylift-light", x8, 32.5), ("copter-heavylift-loaded", x8, None),
             ("copter-quad-hover", quad, None))  # fmt: skip
    for name, af, kg in flown:
        batch = rpc("import.scan", paths=[str(FIX / f"{name}.bin")])["batch"]
        decision: dict[str, Any] = {"log": "f1", "airframe_id": af["id"]}
        if kg is not None:
            decision["takeoff_kg"] = kg
        [done] = rpc("import.commit", batch=batch, decisions=[decision])
        flights[name] = done["flights"][0]
        got[f"{name}:before"] = rpc("flight.margin", flight_id=flights[name])
        rpc("analysis.run", flight_id=flights[name])
    # once all are analysed: the loaded flight's weight comes from the light one's
    for name, flight in flights.items():
        got[name] = rpc("flight.margin", flight_id=flight)
    return got


def test_nothing_before_the_analysis(margins: dict[str, Any]) -> None:
    before = margins["copter-heavylift-light:before"]
    assert before["payload"] is None and before["motor_out"] is None and before["airframe"]


def test_the_loaded_x8s_margin_with_its_layout(margins: dict[str, Any]) -> None:
    """The analyzer's numbers, each motor's place in ArduPilot's mixer, and the heaviest
    take-off in kilograms from the light flight's weighing."""
    loaded = margins["copter-heavylift-loaded"]
    m = loaded["motor_out"]
    assert m["frame"] == "OCTAQUAD/X" and m["hold_yaw"] is True
    assert m["worst_output_pct"] == pytest.approx(85.7, abs=0.2)
    assert m["finding"] == {"code": "motor_out.short", "severity": "caution"}
    assert [x["motor"] for x in m["motors"]] == list(range(1, 9))
    assert all(abs(x["roll"]) == 0.5 and abs(x["pitch"]) == 0.5 for x in m["motors"])
    assert max(x["needed_pct"] for x in m["motors"]) == m["worst_output_pct"]
    assert m["motors"][m["worst_motor"] - 1]["needed_pct"] == m["worst_output_pct"]
    assert m["margin_pts"] == pytest.approx(m["target_pct"] - m["worst_output_pct"], abs=0.11)
    # the loaded flight's weight comes from the light one's: the simulator flew 40.5 kg
    p = loaded["payload"]
    assert p["takeoff_kg"] is None and p["estimated_kg"] == pytest.approx(40.5, rel=0.03)
    assert m["weight_from"] == "estimated"
    assert m["heaviest_kg"] == pytest.approx(m["weight_ratio"] * p["estimated_kg"], abs=0.11)
    light = margins["copter-heavylift-light"]
    assert light["motor_out"]["finding"] is None and light["motor_out"]["weight_from"] == "weighed"
    assert light["payload"]["takeoff_kg"] == 32.5
    # the same aircraft's ceiling from either flight, as the analyzer's test finds
    assert light["motor_out"]["heaviest_kg"] == pytest.approx(m["heaviest_kg"], rel=0.03)


def test_a_quad_says_why_it_has_no_margin(margins: dict[str, Any]) -> None:
    quad = margins["copter-quad-hover"]["motor_out"]
    assert set(quad) == {"skipped"} and "cannot fly on one motor fewer" in quad["skipped"]


def test_no_position_in_the_reply(margins: dict[str, Any]) -> None:
    text = json.dumps(margins)
    for key in ('"lat', '"lon', '"lng', '"alt', "amsl"):
        assert key not in text.lower()
