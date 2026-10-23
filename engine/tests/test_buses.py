"""Buses and interfaces on synthetic flights: serial drops by port and protocol, bus and
sensor error counters, and GPS update gaps."""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import ARM, DISARM, hover_flight, table

from flight_insights.analyzers import Registry
from flight_insights.analyzers.buses import BUSES
from flight_insights.knowledge import load_pack


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def uart(drops: dict[int, float]) -> pa.Table:
    t = np.arange(ARM, DISARM, 1_000_000)
    rows = [(int(x), p) for x in t for p in sorted(drops)]
    return table(
        [r[0] for r in rows],
        {
            "I": (pa.uint8(), [r[1] for r in rows]),
            "Tx": (pa.float32(), [100.0] * len(rows)),
            "Rx": (pa.float32(), [2000.0] * len(rows)),
            "RxDp": (pa.float32(), [drops[r[1]] for r in rows]),
        },
    )


def counter(
    name: str, field: str, rises_at: list[int], extra: dict[str, list[int]] | None = None
) -> pa.Table:
    """A counter message every 0.5 s from boot, its `field` rising by one at each time."""
    t = np.arange(0, DISARM, 500_000)
    values = np.array([sum(1 for r in rises_at if r <= x) for x in t])
    cols: dict[str, tuple[pa.DataType, list[int]]] = {field: (pa.uint32(), values.tolist())}
    if name in ("CANS", "IMU"):
        cols["I"] = (pa.uint8(), [0] * len(t))
    for other, v in (extra or {}).items():
        cols[other] = (pa.uint32(), v)
    return table(t.tolist(), cols)


def test_a_dropping_port_is_named_by_its_protocol(reg: Registry) -> None:
    flight = hover_flight({"UART": uart({0: 0.0, 3: 250.0})}, {"SERIAL3_PROTOCOL": 5.0})
    r = BUSES.run(flight, reg)
    [f] = r.findings
    assert (f.code, f.params) == ("bus.serial_drops", {"port": 3, "protocol": "GPS", "rate": 250})
    assert next(m for m in r.metrics if m.metric == "bus.serial_dropped").zone == "caution"


def test_isolated_sensor_errors_are_a_caution(reg: Registry) -> None:
    imu = counter("IMU", "EG", [ARM + 3_000_000, ARM + 3_500_000])
    r = BUSES.run(hover_flight({"IMU": imu}, {}), reg)
    [f] = r.findings
    assert (f.code, f.severity, f.params["count"]) == ("bus.errors", "caution", 2)
    assert f.params["pattern"] == "isolated" and "IMU 0: gyro errors +2" in f.params["sources"]


def test_errors_growing_through_the_flight_are_out(reg: Registry) -> None:
    cans = counter("CANS", "Rer", [ARM + k * 10_000_000 + 1 for k in range(4)])
    [f] = BUSES.run(hover_flight({"CANS": cans}, {}), reg).findings
    assert (f.severity, f.params["pattern"]) == ("out", "growing through the flight")


def test_a_can_bus_off_is_out_at_once(reg: Registry) -> None:
    cans = counter("CANS", "Bo", [ARM + 2_000_000])
    [f] = BUSES.run(hover_flight({"CANS": cans}, {}), reg).findings
    assert f.severity == "out" and "bus-off +1" in f.params["sources"]


def test_errors_from_before_arming_are_kept_apart(reg: Registry) -> None:
    imu = counter("IMU", "EA", [ARM - 600_000])
    r = BUSES.run(hover_flight({"IMU": imu}, {}), reg)
    assert r.findings == []
    [ev] = [e for e in r.evidence if e.id == "bus:errors"]
    assert ev.values["before_arming"] == {"IMU 0: accelerometer errors": 1}


def test_a_gps_gap_in_flight(reg: Registry) -> None:
    t = list(range(ARM, ARM + 10_000_000, 200_000)) + list(range(ARM + 13_000_000, DISARM, 200_000))
    gps = table(t, {"I": (pa.uint8(), [0] * len(t))})
    r = BUSES.run(hover_flight({"GPS": gps}, {}), reg)
    [f] = r.findings
    assert (f.code, f.severity) == ("bus.gps_gaps", "out") and f.params["seconds"] == pytest.approx(
        3.2
    )


def test_nothing_logged_is_skipped(reg: Registry) -> None:
    assert BUSES.run(hover_flight({}, {}), reg).skipped
