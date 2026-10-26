"""Motor and ESC health on synthetic flights: saturation, spread, desync, extended DShot
status, ESC temperature and supply against siblings, and motor load."""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import ARM, DISARM, HOVER, hover_flight, table

from flight_insights.analyzers import Finding, Registry
from flight_insights.analyzers.motors_health import MOTORS_HEALTH, motors
from flight_insights.knowledge import load_pack

QUAD = {f"SERVO{k}_FUNCTION": float(32 + k) for k in range(1, 5)} | {
    "MOT_PWM_MIN": 1000.0, "MOT_PWM_MAX": 2000.0, "MOT_SPIN_MAX": 0.95,
}  # fmt: skip
TOP = 1950.0  # MOT_PWM_MIN + range x MOT_SPIN_MAX
STEP = 100_000


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def rcou(hover: list[float], override: dict[int, tuple[int, int, float]] | None = None) -> pa.Table:
    """Motor outputs every 0.1 s; override: motor -> (from_us, to_us, pwm)."""
    t = np.arange(ARM, DISARM, STEP)
    cols: dict[str, tuple[pa.DataType, list[float]]] = {}
    for k, base in enumerate(hover, start=1):
        pwm = np.full(len(t), base)
        if override and k in override:
            a, b, v = override[k]
            pwm[(t >= a) & (t <= b)] = v
        cols[f"C{k}"] = (pa.uint16(), pwm.tolist())
    return table(t.tolist(), cols)


def esc(columns: dict[str, list[float]], rpm_dip: tuple[int, int, int] | None = None) -> pa.Table:
    """ESC telemetry every 0.1 s for 4 motors; columns hold one value per ESC.
    rpm_dip: (esc instance, from_us, to_us) where its RPM falls to a fifth."""
    t = np.arange(ARM, DISARM, STEP)
    rows = [(int(x), i) for x in t for i in range(4)]
    out: dict[str, tuple[pa.DataType, list[float]]] = {
        "Instance": (pa.uint8(), [r[1] for r in rows])
    }
    for name, per_esc in columns.items():
        values = [per_esc[i] for _, i in rows]
        if name == "RPM" and rpm_dip is not None:
            inst, a, b = rpm_dip
            values = [
                v / 5 if (i == inst and a <= x <= b) else v
                for (x, i), v in zip(rows, values, strict=True)
            ]
        out[name] = (pa.float32(), values)
    meta = {"Temp": {b"unit": b"degC", b"multiplier": b"1.0"}}
    return table([r[0] for r in rows], out, meta)


def edt2(status: int, instance: int) -> pa.Table:
    t = np.arange(ARM, DISARM, STEP)
    return table(
        t.tolist(),
        {
            "Instance": (pa.uint8(), [instance] * len(t)),
            "Stress": (pa.uint8(), [10] * len(t)),
            "MaxStress": (pa.uint8(), [3] * len(t)),
            "Status": (pa.uint8(), [0] * (len(t) - 5) + [status] * 5),
        },
    )


def found(tables: dict[str, pa.Table], reg: Registry, **fp: Any) -> list[Finding]:
    return MOTORS_HEALTH.run(hover_flight(tables, QUAD, fp or None), reg).findings


def test_motors_come_from_the_servo_functions() -> None:
    ms = motors({"SERVO9_FUNCTION": 34.0, "SERVO1_FUNCTION": 33.0, "SERVO2_FUNCTION": 82.0})
    assert [(m.number, m.channel) for m in ms] == [(1, 1), (2, 9), (9, 2)]


def test_a_well_balanced_hover_raises_nothing(reg: Registry) -> None:
    r = MOTORS_HEALTH.run(hover_flight({"RCOU": rcou([1550, 1560, 1545, 1555])}, QUAD), reg)
    assert r.findings == [] and {m.zone for m in r.metrics} == {"target"}


def test_saturation_in_hover_is_out_of_range(reg: Registry) -> None:
    sat = {3: (HOVER[0] + 1_000_000, HOVER[0] + 4_000_000, TOP)}
    [f] = found({"RCOU": rcou([1700, 1700, 1700, 1700], sat)}, reg)
    assert (f.code, f.severity, f.params["where"]) == ("motors.saturation", "out", "hover")


def test_one_motor_working_hard_names_it(reg: Registry) -> None:
    [f] = found({"RCOU": rcou([1500, 1760, 1500, 1500])}, reg)
    assert (f.code, f.severity) == ("motors.output_spread", "out")  # 26% of the range
    assert (f.params["highest"], f.params["percent"]) == (2, 26.0)


def test_rpm_collapsing_under_command_is_a_desync(reg: Registry) -> None:
    columns = {"RPM": [9000.0] * 4}
    dip = (2, HOVER[0] + 5_000_000, HOVER[0] + 5_600_000)  # motor 3, 0.6 s
    [f] = found({"RCOU": rcou([1550] * 4), "ESC": esc(columns, dip)}, reg)
    assert (f.code, f.severity, f.params["motors"]) == ("esc.desync", "out", "3")
    assert f.params["first_s"] == pytest.approx((HOVER[0] + 5_000_000 - ARM) / 1e6, abs=0.15)


def test_a_slow_spool_up_at_take_off_is_not_a_desync(reg: Registry) -> None:
    # 30-inch propellers take seconds to reach speed while take-off already asks for more
    # than hover: RPM below half the hover RPM under a high command, but never collapsing.
    t = np.arange(ARM, DISARM, STEP)
    rows = [(int(x), i) for x in t for i in range(4)]
    spool = [min(9000.0, 9000.0 * (x - ARM) / 3_000_000) for x, _ in rows]
    escs = table(
        [x for x, _ in rows],
        {"Instance": (pa.uint8(), [i for _, i in rows]), "RPM": (pa.float32(), spool)},
    )
    take_off = {k: (ARM, ARM + 3_000_000, 1700.0) for k in range(1, 5)}
    assert found({"RCOU": rcou([1550] * 4, take_off), "ESC": escs}, reg) == []


def test_a_spool_up_that_reads_in_bursts_is_not_a_desync(reg: Registry) -> None:
    # On the ground before lift-off, ArduPilot's simulator reports a spooling motor in
    # bursts: full speed for a reading, then almost nothing, under a take-off command.
    t = np.arange(ARM, DISARM, STEP)
    burst = [0.0, 5000.0, 9000.0, 1000.0, 9000.0, 500.0, 600.0, 700.0, 9000.0]
    rows = [(int(x), i) for x in t for i in range(4)]
    rpm = [burst[k] if k < len(burst) else 9000.0 for k, _ in enumerate(t) for _ in range(4)]
    escs = table(
        [x for x, _ in rows],
        {"Instance": (pa.uint8(), [i for _, i in rows]), "RPM": (pa.float32(), rpm)},
    )
    take_off = {k: (ARM, ARM + 1_000_000, 1700.0) for k in range(1, 5)}
    assert found({"RCOU": rcou([1550] * 4, take_off), "ESC": escs}, reg) == []
    # Once the speed has held, the same fall is a collapse.
    late = [9000.0] * 20 + [500.0, 600.0, 700.0] + [9000.0] * (len(t) - 23)
    rpm = [late[k] for k, _ in enumerate(t) for _ in range(4)]
    escs = table(
        [x for x, _ in rows],
        {"Instance": (pa.uint8(), [i for _, i in rows]), "RPM": (pa.float32(), rpm)},
    )
    [f] = found({"RCOU": rcou([1550] * 4), "ESC": escs}, reg)
    assert f.code == "esc.desync" and f.params["motors"] == "1, 2, 3, 4"


def test_extended_dshot_warnings_are_named_by_motor(reg: Registry) -> None:
    [f] = found({"RCOU": rcou([1550] * 4), "EDT2": edt2(1 << 3 | 1 << 1, 1)}, reg)
    assert (f.code, f.severity, f.params) == (
        "esc.edt_status",
        "out",
        {"kind": "warning", "motors": "2"},
    )
    [alert] = found({"RCOU": rcou([1550] * 4), "EDT2": edt2(1 << 2, 0)}, reg)
    assert alert.severity == "caution" and alert.params["kind"] == "alert"


def test_an_esc_hotter_than_its_siblings(reg: Registry) -> None:
    columns = {
        "RPM": [9000.0] * 4,
        "Temp": [60.0, 61.0, 59.0, 85.0],
        "Volt": [49.0] * 4,
        "Curr": [9.0] * 4,
    }
    r = MOTORS_HEALTH.run(hover_flight({"RCOU": rcou([1550] * 4), "ESC": esc(columns)}, QUAD), reg)
    [f] = r.findings
    assert (f.code, f.params) == ("esc.hot", {"motor": 4, "temperature": 85, "siblings": 60})
    got = {m.metric: m.value for m in r.metrics}
    assert got["esc.peak_temperature"] == 85.0
    assert got["esc.current_per_rpm2"] == pytest.approx(9.0 / 81.0)


def test_an_esc_short_of_voltage_points_at_its_wiring(reg: Registry) -> None:
    columns = {"RPM": [9000.0] * 4, "Volt": [49.0, 49.0, 48.5, 49.0], "Curr": [9.0] * 4}
    [f] = found({"RCOU": rcou([1550] * 4), "ESC": esc(columns)}, reg)
    assert (f.code, f.params) == ("esc.supply_drop", {"motor": 3, "drop": 0.5})


def test_simulator_esc_values_are_not_judged(reg: Registry) -> None:
    columns = {
        "RPM": [9000.0] * 4,
        "Temp": [60.0, 61.0, 59.0, 85.0],
        "Volt": [49.0, 49.0, 48.0, 49.0],
    }
    sitl = {"board": {"hal": "SITL", "family": "SITL"}}
    assert found({"RCOU": rcou([1550] * 4), "ESC": esc(columns)}, reg, **sitl) == []


def test_no_motors_configured_is_skipped(reg: Registry) -> None:
    r = MOTORS_HEALTH.run(hover_flight({}, {}), reg)
    assert r.skipped and "SERVOn_FUNCTION" in r.skipped
