"""The payload estimate: hover thrust at standard air from each motor's output, the
battery's measured voltage, the tilt and the air's density, on flights whose weight is
known; and the simulator's heavy-lift octocopter flown light and loaded."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import flight, table

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.model import Result
from flight_insights.analyzers.payload import PAYLOAD
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
HZ = 10.0
ARM, DISARM = 1_000_000, 61_000_000
HOVER = (10_000_000, 50_000_000)
EXPO, SPIN_MIN, SPIN_MAX, VOLT_MAX = 0.65, 0.15, 0.95, 25.2
STANDARD = 101_325.0, 15.0  # Pa and degC: 1.225 kg/m3
UNIT = {
    "V": {b"unit": b"V", b"multiplier": b"1"},
    "deg": {b"unit": b"deg", b"multiplier": b"1"},
    "Pa": {b"unit": b"Pa", b"multiplier": b"1"},
    "degC": {b"unit": b"degC", b"multiplier": b"1"},
    "m": {b"unit": b"m", b"multiplier": b"1"},
    "us": {b"unit": b"us", b"multiplier": b"1"},
}
PARAMS = {
    **{f"SERVO{i}_FUNCTION": 32.0 + i for i in range(1, 5)},
    "MOT_PWM_MIN": 1000.0, "MOT_PWM_MAX": 2000.0, "MOT_SPIN_MIN": SPIN_MIN,
    "MOT_SPIN_MAX": SPIN_MAX, "MOT_THST_EXPO": EXPO, "MOT_BAT_VOLT_MAX": VOLT_MAX,
}  # fmt: skip


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def _pwm(thrust: np.ndarray[Any, Any], volts: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    """The output that makes a share of full thrust at a battery voltage: ArduPilot's
    thrust curve, inverted."""
    x = ((EXPO - 1) + np.sqrt((1 - EXPO) ** 2 + 4 * EXPO * thrust)) / (2 * EXPO)
    out = x / (volts / VOLT_MAX)
    return 1000.0 + 1000.0 * (SPIN_MIN + (SPIN_MAX - SPIN_MIN) * out)


def payload_flight(
    weight: float = 0.30,
    volts: float = 24.0,
    air: tuple[float, float] = STANDARD,
    roll_deg: float = 0.0,
    spread: float = 0.0,
    height_m: float = 12.0,
    hover: tuple[int, int] = HOVER,
    stuck: tuple[float, float] | None = None,
    vehicle: str = "copter",
    cells: int | None = None,
    count: int = 4,
    **params: float,
) -> FlightData:
    """A quad (or `count` motors) holding `weight` (its thrust at standard air, as a share
    of full thrust) in steady hover, in air of (Pa, degC), rolled steadily, at a battery
    voltage. Odd motors make `spread` more thrust than even ones, as holding attitude
    would ask; motor 1 sits at the top of its range over the span `stuck` (seconds)."""
    t = np.arange(ARM, DISARM, int(1e6 / HZ), dtype=np.int64)
    s = (t - ARM) / 1e6
    density = air[0] / (287.05 * (air[1] + 273.15)) / 1.225
    needed = weight / (np.cos(np.radians(roll_deg)) * density)
    v = np.full(len(t), volts)
    pwm = [_pwm(np.full(len(t), needed + (spread if i % 2 == 0 else -spread)), v)
           for i in range(count)]  # fmt: skip
    if stuck is not None:
        pwm[0] = np.where((s >= stuck[0]) & (s < stuck[1]), 1000 + 1000 * SPIN_MAX, pwm[0])
    n = len(t)
    climb = [0, ARM + 2_000_000, ARM + 4_000_000, DISARM]

    def f32(values: Any) -> tuple[pa.DataType, list[float]]:
        return pa.float32(), np.broadcast_to(values, n).tolist()

    first = (pa.uint8(), [0] * n)
    tables = {
        "RCOU": table(t.tolist(), {f"C{i + 1}": f32(pwm[i]) for i in range(count)},
                      {f"C{i + 1}": UNIT["us"] for i in range(count)}),
        "BAT": table(t.tolist(), {"Inst": first, "Volt": f32(v)}, {"Volt": UNIT["V"]}),
        "ATT": table(t.tolist(), {"Roll": f32(roll_deg), "Pitch": f32(0.0)},
                     {"Roll": UNIT["deg"], "Pitch": UNIT["deg"]}),
        "BARO": table(t.tolist(), {"I": first, "Press": f32(air[0]), "Temp": f32(air[1])},
                      {"Press": UNIT["Pa"], "Temp": UNIT["degC"]}),
        "CTUN": table(climb, {"Alt": (pa.float32(), [0.0, 0.0, height_m, height_m])},
                      {"Alt": UNIT["m"]}),
    }  # fmt: skip
    fingerprint: dict[str, Any] = {
        "vehicle": vehicle,
        "board": {"hal": "ChibiOS"},
        "hover_windows": [{"start_us": hover[0], "end_us": hover[1]}],
    }
    if cells is not None:
        fingerprint["cells"] = {"value": cells}
    servos = {f"SERVO{i}_FUNCTION": 32.0 + i for i in range(1, count + 1)}
    return flight(tables, ARM, DISARM, params=PARAMS | servos | params, fingerprint=fingerprint)


def thrust(out: Result) -> float:
    [m] = out.metrics
    assert m.metric == "payload.hover_thrust" and m.segment == "hover"
    return m.value


# --- the measure ---------------------------------------------------------------------------


def test_the_thrust_that_holds_the_aircraft_is_measured(reg: Registry) -> None:
    out = PAYLOAD.run(payload_flight(weight=0.30), reg)
    assert thrust(out) == pytest.approx(30.0, abs=0.01)
    [ev] = out.evidence
    assert ev.id == "payload:hover" and ev.values["seconds"] == pytest.approx(40.0, abs=0.2)
    assert ev.values["motors"] == 4 and ev.values["windows"] == [pytest.approx(30.0, abs=0.01)]


def test_a_sagging_battery_does_not_move_it(reg: Registry) -> None:
    """The motors see the voltage the battery measures: at a lower one they need more
    output for the same thrust, and the estimate allows for it."""
    full, sagging = (PAYLOAD.run(payload_flight(volts=v), reg) for v in (25.0, 21.5))
    assert thrust(full) == pytest.approx(thrust(sagging), abs=0.01)


def test_thin_warm_air_does_not_move_it(reg: Registry) -> None:
    """A high, hot field needs more thrust from the motors for the same weight."""
    sea = PAYLOAD.run(payload_flight(), reg)
    high = PAYLOAD.run(payload_flight(air=(84_000.0, 32.0)), reg)
    assert thrust(high) == pytest.approx(thrust(sea), abs=0.01)
    assert high.evidence[0].values["density_ratio"] == pytest.approx(
        0.7828, abs=0.0005
    )  # 0.959 kg/m3


def test_only_the_vertical_thrust_holds_the_weight(reg: Registry) -> None:
    level = PAYLOAD.run(payload_flight(), reg)
    leaning = PAYLOAD.run(payload_flight(roll_deg=8.0), reg)
    assert thrust(leaning) == pytest.approx(thrust(level), abs=0.01)
    assert leaning.evidence[0].values["tilt_deg"] == pytest.approx(8.0, abs=0.1)


def test_motors_working_unevenly_count_by_their_thrust(reg: Registry) -> None:
    """Thrust is not linear in output, so the thrust of the mean output would be wrong."""
    out = PAYLOAD.run(payload_flight(spread=0.06), reg)
    assert thrust(out) == pytest.approx(30.0, abs=0.01)


def test_heavier_is_proportionally_higher(reg: Registry) -> None:
    light, loaded = (PAYLOAD.run(payload_flight(weight=w), reg) for w in (0.30, 0.375))
    assert thrust(loaded) / thrust(light) == pytest.approx(1.25, abs=0.001)


def test_with_voltage_compensation_off_a_full_pack_is_the_reference(reg: Registry) -> None:
    """MOT_BAT_VOLT_MAX 0: the curve's full output is taken at 4.2 V a cell."""
    on = PAYLOAD.run(payload_flight(), reg)
    off = PAYLOAD.run(payload_flight(cells=6, MOT_BAT_VOLT_MAX=0.0), reg)
    assert thrust(off) == pytest.approx(thrust(on), abs=0.01)
    assert off.evidence[0].values["reference_volts"] == pytest.approx(25.2)


def test_a_motor_at_the_end_of_its_range_is_left_out(reg: Registry) -> None:
    out = PAYLOAD.run(payload_flight(stuck=(20.0, 25.0)), reg)
    assert thrust(out) == pytest.approx(30.0, abs=0.01)
    assert out.evidence[0].values["seconds"] == pytest.approx(35.0, abs=0.2)


# --- what it needs ------------------------------------------------------------------------


def test_hovering_in_ground_effect_does_not_count(reg: Registry) -> None:
    out = PAYLOAD.run(payload_flight(height_m=2.5), reg)
    assert out.skipped == "too little steady hover, clear of the ground, to weigh the aircraft"


def test_a_short_hover_is_not_enough(reg: Registry) -> None:
    out = PAYLOAD.run(payload_flight(hover=(10_000_000, 18_000_000)), reg)
    assert out.skipped == "too little steady hover, clear of the ground, to weigh the aircraft"


def test_without_a_voltage_reference_it_does_not_guess(reg: Registry) -> None:
    out = PAYLOAD.run(payload_flight(MOT_BAT_VOLT_MAX=0.0), reg)
    assert out.skipped == "MOT_BAT_VOLT_MAX is off and the cell count is not known"


def test_it_needs_the_barometer_and_the_motor_outputs(reg: Registry) -> None:
    f = payload_flight()
    no_baro = flight({k: v for k, v in f.cache.tables.items() if k != "BARO"}, ARM, DISARM,  # type: ignore[union-attr]
                     params=f.params, fingerprint=f.fingerprint)  # fmt: skip
    assert PAYLOAD.run(no_baro, reg).skipped == "the log has no BARO"
    unmapped = payload_flight(SERVO1_FUNCTION=0.0, SERVO2_FUNCTION=0.0, SERVO3_FUNCTION=0.0,
                              SERVO4_FUNCTION=0.0)  # fmt: skip
    assert PAYLOAD.run(unmapped, reg).skipped == "the motor outputs are not in RCOU"


def test_planes_and_parameter_files_are_not_weighed(reg: Registry) -> None:
    assert PAYLOAD.run(payload_flight(vehicle="plane"), reg).skipped == (
        "the payload estimate is for Copter"
    )
    assert PAYLOAD.run(replace(payload_flight(), cache=None), reg).skipped == (
        "a parameter file has no flight to measure"
    )


def test_every_threshold_comes_from_the_rule_pack(reg: Registry) -> None:
    rules = reg.rules["payload"]
    assert rules["calibrated"] is False and rules["sources"]
    assert rules["min_height_m"] >= reg.pack.json("rules/hover.json")["min_height_m"]


# --- the simulator's heavy-lift octocopter --------------------------------------------------


def _loaded(tmp_path: Path, name: str, reg: Registry) -> Result:
    keys = StoreKeys.generate()
    pack = reg.pack
    with open_store(tmp_path / f"{name}.db", keys, "e" * 32) as store:
        cache = ColumnCache(tmp_path / f"{name}-cache", keys)
        batch = scan(store, pack, bytes(32), [FIXTURES / f"{name}.bin"])
        [res] = commit(store, pack, cache, batch, propose(store, pack, batch), [Decision("f1")])
        return PAYLOAD.run(FlightData.load(store, cache, pack, res.flights[0]), reg)


def test_the_simulated_octocopters_mass_ratio_is_recovered(reg: Registry, tmp_path: Path) -> None:
    """ArduPilot's Callisto model, 32.5 kg, then carrying 8 kg more (scenarios.py). The
    simulator's motors scale thrust with the voltage squared, ArduPilot's thrust curve
    with the voltage inside it; on these flights that differs by under 1%."""
    light = _loaded(tmp_path, "copter-heavylift-light", reg)
    loaded = _loaded(tmp_path, "copter-heavylift-loaded", reg)
    assert thrust(loaded) / thrust(light) == pytest.approx(40.5 / 32.5, rel=0.03)
    assert 0.3 < thrust(light) / 100 < thrust(loaded) / 100 < 0.5
