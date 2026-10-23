"""Decode the messages of one format into columns, every sample, nothing approximated."""

from __future__ import annotations

from typing import Any, cast

import numpy as np
import pyarrow as pa

from flight_insights.dataflash.formats import ARRAY_LEN, FORMAT_CHARS, DfFormat

# Rows gathered per block, so the byte-index array stays small for large logs.
BLOCK_ROWS = 1 << 18


def decode_rows(
    data: np.ndarray[tuple[int], np.dtype[np.uint8]],
    offsets: np.ndarray[tuple[int], np.dtype[np.int64]],
    fmt: DfFormat,
) -> np.ndarray[tuple[int], np.dtype[np.void]]:
    """The stored values of every message of `fmt` at `offsets` (header offsets)."""
    out = np.empty(len(offsets), dtype=fmt.dtype)
    if not len(offsets):
        return out
    body = np.arange(3, fmt.length, dtype=np.int64)
    raw = out.view(np.uint8).reshape(len(offsets), fmt.dtype.itemsize)
    for start in range(0, len(offsets), BLOCK_ROWS):
        block = offsets[start : start + BLOCK_ROWS]
        raw[start : start + len(block)] = data[block[:, None] + body]
    return out


def _metadata(fmt: DfFormat, i: int) -> dict[bytes | str, bytes | str]:
    ch = fmt.format[i]
    meta: dict[bytes | str, bytes | str] = {
        b"format": ch.encode(),
        b"unit": fmt.units[i].encode(),
        b"position": b"1" if fmt.is_position(i) else b"0",
        b"instance": b"1" if fmt.instance_column == fmt.field_names[i] else b"0",
    }
    scale = FORMAT_CHARS[ch].scale
    if scale is not None:
        meta[b"scale"] = repr(scale).encode()
    mult = fmt.mults[i]
    if mult is not None:
        meta[b"multiplier"] = repr(mult).encode()
    return meta


def to_table(rows: np.ndarray[tuple[int], np.dtype[np.void]], fmt: DfFormat) -> pa.Table:
    """Columns hold the values as stored; each field's metadata says how to read them.

    - `format`: the format character; `scale`: the multiplier it implies (c, C, e, E, L);
    - `unit`, `multiplier`: from the log's FMTU, UNIT and MULT messages;
    - `position`: 1 if the unit is a latitude or longitude, the format is `L`, or the
      message holds raw GNSS observations; `instance`: 1 for the
      column that tells instances (IMU, ESC, battery...) apart.
    Strings stay as their fixed-width bytes; `formats.text` decodes them.
    """
    arrays: list[pa.Array[Any]] = []
    fields: list[pa.Field[Any]] = []
    for i, name in enumerate(fmt.field_names):
        ch = fmt.format[i]
        col = rows[name]
        kind = FORMAT_CHARS[ch].kind
        arr: pa.Array[Any]
        if kind == "str":
            # numpy drops a fixed-width string's trailing NULs; pad back to the stored bytes.
            width = col.dtype.itemsize
            arr = pa.array([v.ljust(width, b"\0") for v in col.tolist()], type=pa.binary(width))
        elif kind == "array":
            flat = pa.array(np.ascontiguousarray(col).reshape(-1))
            # pyarrow-stubs leave from_arrays partly untyped
            arr = cast("pa.Array[Any]", pa.FixedSizeListArray.from_arrays(flat, ARRAY_LEN))  # pyright: ignore[reportUnknownMemberType]
        else:
            arr = pa.array(np.ascontiguousarray(col).astype(col.dtype.newbyteorder("=")))
        arrays.append(arr)
        fields.append(pa.field(name, arr.type, metadata=_metadata(fmt, i)))
    meta: dict[bytes | str, bytes | str] = {
        b"name": fmt.name.encode(),
        b"type": str(fmt.type).encode(),
    }
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields, metadata=meta))
