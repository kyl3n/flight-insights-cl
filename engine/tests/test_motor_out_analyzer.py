"""The motor-out analyzer on synthetic hovers: the weight from the motors' outputs, each
motor lost in turn against the frame's own mixer factors, and the target of its class."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_imports import airframe
from test_payload import EXPO, VOLT_MAX, payload_flight

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.model import Result
from flight_insights.analyzers.motor_out import MOTOR_OUT
from flight_insights.analyzers.payload import PAYLOAD
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.motor_out import output_for, thrust_at, worst
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

PACK = load_pack()
OCTA_X = {"FRAME_CLASS": 3.0, "FRAME_TYPE": 1.0}
HEXA_X = {"FRAME_CLASS": 2.0, "FRAME_TYPE": 1.0}


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(PACK)


def run(reg: Registry, weight: float, frame: dict[str, float], volts: float = 24.0,
        count: int = 8) -> Result:  # fmt: skip
    flight = payload_flight(weight=weight, volts=volts, count=count)
    return MOTOR_OUT.run(replace(flight, params={**flight.params, **frame}), reg)


def metric(r: Result, name: str) -> float:
    return next(m.value for m in r.metrics if m.metric == name)


def test_an_octa_at_a_light_hover_holds_with_room_to_spare(reg: Registry) -> None:
    r = run(reg, 0.30, OCTA_X)
    [ev] = r.evidence
    motors = PACK.frames("4.7.0")["3"]["types"]["1"]["motors"]
    peak = worst(motors, 8 * 0.30, hold_yaw=True)[0].peak
    assert peak is not None
    expected = output_for(peak, EXPO) * VOLT_MAX / 24.0
    assert ev.values["worst_output_pct"] == pytest.approx(100 * expected, abs=0.1)
    assert metric(r, "motor_out.margin") == pytest.approx(100 * (0.85 - expected), abs=0.1)
    ratio = thrust_at(0.85 * 24.0 / VOLT_MAX, EXPO) / peak
    assert metric(r, "motor_out.weight_ratio") == pytest.approx(ratio, abs=1e-3)
    assert r.findings == [] and ev.values["hold_yaw"] is True and ev.values["frame"] == "OCTA/X"


def test_a_heavy_octa_falls_short_and_says_how_much_lighter(reg: Registry) -> None:
    r = run(reg, 0.62, OCTA_X)
    [f] = r.findings
    assert f.code == "motor_out.short" and f.severity == "caution"
    assert f.params["needed"] > f.params["target"] == 85.0
    assert f.params["ratio"] < 1.0
    assert metric(r, "motor_out.margin") < 0 and metric(r, "motor_out.weight_ratio") < 1.0


def test_a_sagging_battery_asks_more_output_for_the_same_weight(reg: Registry) -> None:
    full, sagging = (metric(run(reg, 0.40, OCTA_X, volts=v), "motor_out.margin")
                     for v in (25.0, 21.5))  # fmt: skip
    assert sagging < full


def test_a_hexa_is_held_level_against_its_own_target(reg: Registry) -> None:
    r = run(reg, 0.40, HEXA_X, count=6)
    [ev] = r.evidence
    assert ev.values["hold_yaw"] is False and ev.values["target_pct"] == 90.0
    # the opposite motor stops too: the four left carry 1.5 times their hover thrust
    assert ev.values["worst_output_pct"] == pytest.approx(
        100 * output_for(0.40 * 1.5, EXPO) * VOLT_MAX / 24.0, abs=0.1
    )


def test_what_has_no_margin_is_skipped(reg: Registry) -> None:
    assert "no margin is possible" in (run(reg, 0.3, {"FRAME_CLASS": 1.0}, count=4).skipped or "")
    assert "not a frame" in (run(reg, 0.3, {"FRAME_CLASS": 6.0}).skipped or "")  # heli
    assert "motor outputs for a" in (run(reg, 0.3, OCTA_X, count=4).skipped or "")
    unknown = run(reg, 0.3, OCTA_X | {"FRAME_TYPE": 16.0})  # no-yaw-torque: quads only
    assert unknown.skipped == "FRAME_TYPE 16 is not one of the OCTA frames Copter sets up"


# --- the simulator's heavy-lift X8, flown light and loaded --------------------------------


@pytest.fixture(scope="module")
def callisto(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Result, Result]]:
    """Each flight's motor-out and payload results."""
    fixtures = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
    reg = Registry.load(PACK)
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("motor_out")
    out: dict[str, tuple[Result, Result]] = {}
    with open_store(folder / "m.db", keys, "e" * 32) as store:
        cache = ColumnCache(folder / "cache", keys)
        for name in ("copter-heavylift-light", "copter-heavylift-loaded"):
            batch = scan(store, PACK, bytes(32), [fixtures / f"{name}.bin"])
            af = airframe(store, PACK, name)
            [res] = commit(store, PACK, cache, batch, propose(store, PACK, batch),
                           [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
            flight = FlightData.load(store, cache, PACK, res.flights[0])
            out[name] = (MOTOR_OUT.run(flight, reg), PAYLOAD.run(flight, reg))
    return out


def test_the_loaded_x8_has_less_margin_and_the_same_ceiling(
    callisto: dict[str, tuple[Result, Result]],
) -> None:
    """Loaded, the X8 needs more from its busiest motor after losing one: at 40.5 kg, 85.7%
    from ArduPilot's mixer, just short of the 85% target (the best split would ask 79%).
    The simulator agrees: flown at 40.5 kg losing each motor in turn, its busiest motor
    ran at 84 to 86%. The heaviest take-off each flight allows is the same aircraft's
    ceiling: light's ratio and loaded's, with the payload estimate's 1.25 between them,
    agree within 3%."""
    (light, light_payload), (loaded, loaded_payload) = (
        callisto["copter-heavylift-light"], callisto["copter-heavylift-loaded"],
    )  # fmt: skip
    assert metric(loaded, "motor_out.margin") < metric(light, "motor_out.margin")
    assert light.findings == [] and [f.code for f in loaded.findings] == ["motor_out.short"]
    values = loaded.evidence[0].values
    assert values["worst_output_pct"] == pytest.approx(85.7, abs=0.2)
    assert values["best_split_output_pct"] == pytest.approx(79.0, abs=0.2)
    assert light.evidence[0].values["frame"] == "OCTAQUAD/X"
    heavier = metric(loaded_payload, "payload.hover_thrust") / metric(
        light_payload, "payload.hover_thrust"
    )
    ceiling_light = metric(light, "motor_out.weight_ratio")
    ceiling_loaded = metric(loaded, "motor_out.weight_ratio") * heavier
    assert ceiling_loaded == pytest.approx(ceiling_light, rel=0.03)
