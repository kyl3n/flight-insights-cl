"""Messages as pymavlink's DFReader presents them, for comparison with the reference.

The Phase 0 gate requires the engine to decode exactly as pymavlink does. These
are pymavlink's presentation rules, applied to the engine's stored values:
- c, C, e, E and L are divided by the reciprocal of their implied multiplier
  (pymavlink divides rather than multiplies, for accuracy);
- M is read as a signed byte;
- strings are decoded as UTF-8, or Latin-1 if that fails, then cut at the first NUL;
  FILE's Z field is the exception and stays raw bytes;
- 'a' arrays become lists of int16.
FMTU multipliers are not applied: pymavlink does not apply them either.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import numpy as np

from flight_insights.dataflash.formats import FORMAT_CHARS, DfFormat
from flight_insights.dataflash.log import DataflashLog

Value = int | float | str | bytes | list[int]
Converter = Callable[[Any], Value]


def _string(raw: bytes) -> str:
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        s = raw.decode("ISO-8859-1")
    cut = s.find("\0")
    return s if cut < 0 else s[:cut]


def _converter(fmt: DfFormat, i: int) -> Converter:
    ch = fmt.format[i]
    spec = FORMAT_CHARS[ch]
    if spec.kind == "str":
        # numpy drops a fixed-width string's trailing NULs; pad back to the stored bytes.
        width = np.dtype(spec.dtype).itemsize
        if ch == "Z" and fmt.name == "FILE":
            return lambda v: bytes(v).ljust(width, b"\0")
        return lambda v: _string(bytes(v).ljust(width, b"\0"))
    if spec.kind == "array":
        return lambda v: [int(x) for x in v]
    if ch == "M":
        return lambda v: int(np.int8(np.uint8(v)))
    if spec.scale is not None:
        divisor = 1 / spec.scale
        return lambda v: float(int(v)) / divisor
    if spec.kind == "float":
        return float
    return int


def pymavlink_messages(log: DataflashLog) -> Iterator[tuple[str, dict[str, Value]]]:
    """Every message in file order as (type name, {column: value}), the way pymavlink reads it."""
    formats = log.index.formats
    converters: dict[int, list[tuple[str, str, Converter]]] = {}
    positions: dict[int, int] = {}
    for fid in log.index.format_ids.tolist():
        fmt = formats[fid]
        if fid not in converters:
            converters[fid] = [
                (column, name, _converter(fmt, i))
                for i, (column, name) in enumerate(zip(fmt.columns, fmt.field_names, strict=False))
            ]
        k = positions.get(fid, 0)
        positions[fid] = k + 1
        row = log.rows(fid)[k]
        yield fmt.name, {column: conv(row[name]) for column, name, conv in converters[fid]}
