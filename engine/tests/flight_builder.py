"""Flights for analyzer tests, built in memory: cached tables, parameters, fingerprint
and log checks, served the way FlightData.load would serve them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pyarrow as pa

from flight_insights.analyzers import FlightData

TIME_META = {b"unit": b"s", b"multiplier": b"1e-06"}


class FakeCache:
    """Serves one flight's tables as the cache would, from memory."""

    def __init__(self, tables: dict[str, pa.Table]) -> None:
        self.tables = tables

    def names(self, _key: str) -> list[str]:
        return sorted(self.tables)

    def read(self, _key: str, name: str, columns: list[str] | None = None) -> pa.Table:
        t = self.tables[name]
        return t.select(columns) if columns else t


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
    )
