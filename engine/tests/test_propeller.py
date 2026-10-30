"""The propeller as the aircraft flies it (performance/propeller.py): a made-up quad whose
22-inch propellers have known coefficients, flown in steady hover with ESC telemetry from
a known motor. The anchor gives back the thrust, speed and power per motor; with the motor
model, the propeller's own power coefficient and the drive's efficiency; a maker's table
with speed and torque gives the same coefficients."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import ARM, DISARM, hover_flight, table

from flight_insights.analyzers import Registry
from flight_insights.knowledge import load_pack
from flight_insights.performance.bench import Bench, Row
from flight_insights.performance.motor import RPM_TO_RAD, Motor
from flight_insights.performance.propeller import (
    INCH,
    G,
    anchor_view,
    coefficients,
    flight_anchor,
    shaft,
    table_points,
)

REG = Registry.load(load_pack())
D_IN = 22.0
D = D_IN * INCH
CT, CP = 0.10, 0.035  # the propeller's (n-based) coefficients
RHO = 1.2
MASS = 16.0  # kg, a quad
MOTOR = Motor(kv=210.0, r0=0.08, i00=0.8)
VOLTS = 48.0
PARAMS = {f"SERVO{k}_FUNCTION": 32.0 + k for k in range(1, 5)}  # Motor1-4 on outputs 1-4


def hover_point(thrust_n: float) -> tuple[float, float, float]:
    """(rpm, shaft W, electrical W) for one motor giving this thrust."""
    n = math.sqrt(thrust_n / (CT * RHO * D**4))
    p_shaft = CP * RHO * n**3 * D**5
    omega = n * 2 * math.pi
    return n * 60, p_shaft, float(MOTOR.power(p_shaft / omega, omega))


def tables(power_columns: bool = True, tilt_deg: float = 0.0) -> dict[str, pa.Table]:
    rpm, _, p_e = hover_point(MASS * G / 4 / math.cos(math.radians(tilt_deg)))
    t = np.arange(ARM, DISARM, 10_000)
    rows = [(int(x), m) for x in t for m in range(4)]
    cols: dict[str, tuple[pa.DataType, list[Any]]] = {
        "Instance": (pa.uint8(), [r[1] for r in rows]),
        "RPM": (pa.float32(), [rpm] * len(rows)),
    }
    if power_columns:
        cols["Volt"] = (pa.float32(), [VOLTS] * len(rows))
        cols["Curr"] = (pa.float32(), [p_e / VOLTS] * len(rows))
    meta = {"RPM": {b"unit": b"rpm", b"multiplier": b"1.0"}, "Volt": {b"unit": b"V"},
            "Curr": {b"unit": b"A"}}  # fmt: skip
    esc = table([r[0] for r in rows], cols, meta)
    ta = np.arange(ARM, DISARM, 100_000)
    att = table(ta.tolist(), {"Roll": (pa.float32(), [tilt_deg] * len(ta)),
                              "Pitch": (pa.float32(), [0.0] * len(ta))},
                {"Roll": {b"unit": b"deg"}, "Pitch": {b"unit": b"deg"}})  # fmt: skip
    return {"ESC": esc, "ATT": att}


def air(starts: Any, ends: Any) -> Any:
    return np.full(len(starts), RHO)


def test_the_anchor_gives_the_propellers_operating_point() -> None:
    flight = hover_flight(tables(), PARAMS)
    a = flight_anchor(flight, REG, MASS, air, None)
    assert not isinstance(a, str)
    rpm, p_shaft, p_e = hover_point(MASS * G / 4)
    assert (a.motors, a.power_from, a.rpm_spread) == (4, "escs", 0.0)
    assert a.rpm == pytest.approx(rpm, rel=1e-4) and a.power_w == pytest.approx(p_e, rel=1e-4)
    assert a.thrust_n == pytest.approx(MASS * G / 4) and a.seconds == pytest.approx(30.0)
    v = anchor_view(a, D_IN, MOTOR)
    assert v["ct"] == pytest.approx(CT, rel=1e-3)
    # with the motor model, the propeller's own power coefficient and the drive's efficiency
    assert v["cp"] == pytest.approx(CP, rel=1e-3) and v["shaft_w"] == pytest.approx(
        p_shaft, rel=1e-3
    )
    assert v["efficiency"] == pytest.approx(p_shaft / p_e, rel=1e-3)
    assert v["cp_electric"] > v["cp"]  # the electrical power holds the motor's losses
    fm = (MASS * G / 4) ** 1.5 / math.sqrt(2 * RHO * math.pi * (D / 2) ** 2) / p_shaft
    assert v["figure_of_merit"] == pytest.approx(fm, rel=1e-3)
    # without the diameter, only the per-motor figures; without a motor, no shaft power
    assert "ct" not in anchor_view(a, None, MOTOR)
    assert "cp" not in anchor_view(a, D_IN, None)


def test_a_tilted_hover_holds_more_thrust_than_the_weight() -> None:
    a = flight_anchor(hover_flight(tables(tilt_deg=10.0), PARAMS), REG, MASS, air, None)
    assert not isinstance(a, str)
    assert a.thrust_n == pytest.approx(MASS * G / 4 / math.cos(math.radians(10.0)), rel=1e-3)


def test_without_the_escs_power_the_packs_stand_in() -> None:
    flight = hover_flight(tables(power_columns=False), PARAMS)
    t = np.arange(ARM, DISARM, 100_000)
    a = flight_anchor(flight, REG, MASS, air, (t, np.full(len(t), 2000.0)))
    assert not isinstance(a, str) and a.power_from == "packs" and a.power_w == pytest.approx(500.0)
    assert "cp" not in anchor_view(a, D_IN, MOTOR)  # the packs' power holds the avionics


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"ESC": None}, "no ESC speed telemetry"),
        ({"ATT": None}, "no attitude"),
    ],
)
def test_why_there_is_no_anchor(change: dict[str, Any], says: str) -> None:
    got = {k: v for k, v in (tables() | change).items() if v is not None}
    a = flight_anchor(hover_flight(got, PARAMS), REG, MASS, air, None)
    assert isinstance(a, str) and says in a
    short = hover_flight(tables(), PARAMS, {"hover_windows": [{"start_us": 5_000_000,
                                                               "end_us": 9_000_000}]})  # fmt: skip
    assert "quiet steady hover" in str(flight_anchor(short, REG, MASS, air, None))


def test_the_torque_that_draws_the_power() -> None:
    got = shaft(MOTOR, 3500.0, 600.0)
    assert got is not None
    q, w = got
    assert float(MOTOR.power(q, 3500.0 * RPM_TO_RAD)) == pytest.approx(600.0, rel=1e-6)
    assert w == pytest.approx(q * 3500.0 * RPM_TO_RAD)
    assert shaft(MOTOR, 3500.0, 1.0) is None  # less than turning nothing draws


def test_a_tables_steps_give_the_same_coefficients() -> None:
    rows: list[Row] = []
    for kg in (2.0, 3.0, 4.0, 5.0):
        rpm, p_shaft, p_e = hover_point(kg * G)
        q = p_shaft / (rpm * RPM_TO_RAD)
        rows.append(Row(10.0 * kg, kg * 1000.0, p_e, rpm=rpm, torque_nm=q))
    rho_bench = 101325.0 / (287.05287 * (20.0 + 273.15))
    # made at RHO: in the bench's air the same thrust needs a different speed, so the
    # coefficients read back scale by RHO / rho_bench
    b = Bench("t", 1, "none", "made-up", None, 210.0, D_IN, 7.0, VOLTS, None, None,
              tuple(rows), 20.0)  # fmt: skip
    points, air_from = table_points(b)
    assert air_from == "the bench's" and len(points) == 4
    for p in points:
        assert p.ct == pytest.approx(CT * RHO / rho_bench, rel=1e-6)
        assert p.cp == pytest.approx(CP * RHO / rho_bench, rel=1e-6)
    ct, cp = coefficients(rows[0].rpm or 0.0, 2.0 * G, rows[0].shaft_w or 0.0, RHO, D)
    assert (ct, cp) == (pytest.approx(CT), pytest.approx(CP))
    no_diameter = Bench("t", 1, "none", "made-up", None, None, None, None, None, None, None,
                        tuple(rows))  # fmt: skip
    assert table_points(no_diameter)[0] == []


def test_the_anchor_is_kept_with_the_airframes_model() -> None:
    from flight_insights.performance.airframe import AirframeModel, from_json, to_json
    from flight_insights.performance.points import FlightPoints

    a = flight_anchor(hover_flight(tables(), PARAMS), REG, MASS, air, None)
    assert not isinstance(a, str)
    kept = AirframeModel("af", None, 1.0, False,
                         (FlightPoints("f1", (), MASS, "weighed", "none", "isa", {}, a),
                          FlightPoints("f2", (), None, "unknown", "none", "isa", {}, None,
                                       "not weighed")),
                         {}, "no fit")  # fmt: skip
    back = from_json(to_json(kept))
    assert back.flights[0].anchor == a and back.flights[1].anchor_why == "not weighed"
