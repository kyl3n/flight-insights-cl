"""Field values in the units their FMTU names: the analysis layer's view of a column.

Tables hold values as stored. ArduPilot's rule (libraries/AP_Logger/LogStructure.h)
is that the FMTU multiplier applies to the raw stored value, and any scaling the
format character implies (the "centi" of c, C, e, E) is ignored: the format only
gives the C type. So a value is raw x multiplier. Where a log has no FMTU for a
type, the format character's own scaling is the only information, as pymavlink
uses it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import numpy as np
import pyarrow as pa

Floats = np.ndarray[tuple[int], np.dtype[np.float64]]


def field_meta(table: pa.Table, name: str) -> dict[str, str]:
    schema = cast(Any, table.schema)
    raw = cast(Mapping[bytes, bytes] | None, schema.field(name).metadata) or {}
    return {k.decode(): v.decode() for k, v in raw.items()}


def scale_of(meta: Mapping[str, str]) -> float:
    """The factor from a stored value to its unit."""
    if "multiplier" in meta:
        mult = float(meta["multiplier"])
        return 1.0 if mult == 0 else mult  # 0 is '-': no multiplier
    return float(meta.get("scale", 1.0))


def values(table: pa.Table, name: str) -> Floats:
    """A numeric column as float64 in the unit `unit(table, name)` names."""
    column = table.column(name).to_numpy(zero_copy_only=False)
    return np.asarray(column, dtype=np.float64) * scale_of(field_meta(table, name))


def unit(table: pa.Table, name: str) -> str:
    return field_meta(table, name).get("unit", "")


def seconds(table: pa.Table, name: str = "TimeUS") -> Floats:
    """A timestamp column in seconds, whatever its stored unit."""
    if unit(table, name) not in ("s", ""):
        raise ValueError(f"{name} is in {unit(table, name)!r}, not a time")
    return values(table, name)
