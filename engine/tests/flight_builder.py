"""Flights for analyzer tests, built in memory: cached tables, parameters, fingerprint
and log checks, served the way FlightData.load would serve them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.analyzers import FlightData

TIME_META = {b"unit": b"s", b"multiplier": b"1e-06"}
RAD = {b"unit": b"rad/s", b"multiplier": b"1.0"}
RATE = 1000.0  # synthetic gyro rate, Hz
ARM, DISARM = 1_000_000, 40_000_000  # the synthetic flight's armed span
HOVER = (5_000_000, 35_000_000)  # and its steady hover


class FakeCache:
    """Serves one flight's tables as the cache would, from memory."""

    def __init__(self, tables: dict[str, pa.Table]) -> None:
        self.tables = tables

    def names(self, _key: str) -> list[str]:
        return sorted(self.tables)

    def read(
        self, _key: str, name: str, columns: list[str] | None = None, positions: bool = False
    ) -> pa.Table:
        t = self.tables[name]
        return t.select([c for c in columns if c in t.column_names]) if columns else t


def table(
    times_us: Sequence[int],
    columns: Mapping[str, tuple[pa.DataType, Sequence[Any]]],
    meta: Mapping[str, Mapping[bytes, bytes]] | None = None,
) -> pa.Table:
    """A message type's table: TimeUS plus the named columns, with FMTU-style metadata."""
    fields: list[pa.Field[Any]] = [pa.field("TimeUS", pa.uint64(), metadata=TIME_META)]
    arrays: list[pa.Array[Any]] = [pa.array(list(times_us), pa.uint64())]
    for name, (kind, values) in columns.items():
        field_meta = (meta or {}).get(name)
        fields.append(pa.field(name, kind, metadata=dict(field_meta) if field_meta else None))
        arrays.append(pa.array(list(values), kind))
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def flight(
    tables: dict[str, pa.Table],
    arm_us: int,
    disarm_us: int,
    params: Mapping[str, float] | None = None,
    validation: Mapping[str, Any] | None = None,
    fingerprint: Mapping[str, Any] | None = None,
    firmware: str = "4.7.0",
    answers: Mapping[str, Any] | None = None,
    defaults: Mapping[str, float] | None = None,
) -> FlightData:
    return FlightData(
        id="f",
        key="fl1-x",
        airframe_id=None,
        firmware=firmware,
        arm_us=arm_us,
        disarm_us=disarm_us,
        params=params or {},
        fingerprint=fingerprint
        or {"vehicle": "copter", "board": {"hal": "ChibiOS"}, "hover_windows": []},
        cache=FakeCache(tables),  # type: ignore[arg-type]
        validation=validation,
        answers=answers or {},
        defaults=defaults or {},
    )


def gyr(
    tones: list[tuple[float, float]],
    seconds: float = 30.0,
    drop: slice | None = None,
    instance: int = 0,
    start_us: int = HOVER[0],
) -> pa.Table:
    """GYR at 1 kHz: sines of (Hz, amplitude in deg/s) on X and Y, with noise on Z."""
    n = int(seconds * RATE)
    clock = start_us + np.arange(n, dtype=np.int64) * 1000
    t = (clock - start_us) / 1e6
    x = sum(a * np.sin(2 * np.pi * f * t) for f, a in tones) * np.pi / 180
    z = np.random.default_rng(1).normal(0, 0.001, n)
    keep = np.ones(n, bool)
    if drop is not None:
        keep[drop] = False
    cols = {
        "I": (pa.uint8(), [instance] * int(keep.sum())),
        "SampleUS": (pa.uint64(), clock[keep].tolist()),
        "GyrX": (pa.float32(), np.asarray(x)[keep].tolist()),
        "GyrY": (pa.float32(), np.asarray(x)[keep].tolist()),
        "GyrZ": (pa.float32(), z[keep].tolist()),
    }
    meta = {a: RAD for a in ("GyrX", "GyrY", "GyrZ")}
    return table(clock[keep].tolist(), cols, meta)


def esc(
    rpm: float, motors: int = 4, err: float = 0.0, gap: tuple[int, int] | None = None
) -> pa.Table:
    """ESC telemetry every 10 ms for each motor between arming and disarming."""
    t = np.arange(ARM, DISARM, 10_000)
    if gap is not None:
        t = t[(t < gap[0]) | (t > gap[1])]
    rows = [(int(x), m) for x in t for m in range(motors)]
    meta = {"RPM": {b"unit": b"rpm", b"multiplier": b"1.0"}, "Err": {b"unit": b"%"}}
    return table(
        [r[0] for r in rows],
        {
            "Instance": (pa.uint8(), [r[1] for r in rows]),
            "RPM": (pa.float32(), [rpm] * len(rows)),
            "Err": (pa.float32(), [err] * len(rows)),
        },
        meta,
    )


def hover_flight(
    tables: dict[str, pa.Table],
    params: dict[str, float] | None = None,
    fingerprint: dict[str, Any] | None = None,
    answers: dict[str, Any] | None = None,
    defaults: dict[str, float] | None = None,
) -> FlightData:
    """A flight with 30 s of steady hover, pre-filter gyro only (INS_RAW_LOG_OPT 1)."""
    fp: dict[str, Any] = {
        "vehicle": "copter",
        "board": {"hal": "ChibiOS", "family": "H7"},
        "imu_count": {"value": 1},
        "hover_windows": [{"start_us": HOVER[0], "end_us": HOVER[1]}],
        **(fingerprint or {}),
    }
    return flight(
        tables,
        ARM,
        DISARM,
        {"INS_RAW_LOG_OPT": 1, **(params or {})},
        fingerprint=fp,
        answers=answers,
        defaults=defaults,
    )
