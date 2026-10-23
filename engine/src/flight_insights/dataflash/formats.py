"""The DataFlash format alphabet, and message formats built from a log's FMT messages.

Message layouts are never hard-coded: each comes from an FMT message in the log
itself. What is fixed is the alphabet those FMT messages are written in, plus the
layout of FMT, which the format defines for itself.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

HEAD = b"\xa3\x95"
FMT_TYPE = 0x80
# FMT describes itself: Type B, Length B, Name n, Format N, Columns Z.
FMT_LENGTH = 89
FMT_STRUCT = struct.Struct("<BB4s16s64s")

Kind = Literal["int", "float", "str", "array"]


@dataclass(frozen=True)
class FormatChar:
    dtype: str  # little-endian numpy dtype of the stored value
    kind: Kind
    scale: float | None = None  # multiplier the character itself implies


ARRAY_LEN = 32  # 'a' is int16[32]

FORMAT_CHARS: dict[str, FormatChar] = {
    "a": FormatChar("<i2", "array"),
    "b": FormatChar("<i1", "int"),
    "B": FormatChar("<u1", "int"),
    "h": FormatChar("<i2", "int"),
    "H": FormatChar("<u2", "int"),
    "i": FormatChar("<i4", "int"),
    "I": FormatChar("<u4", "int"),
    "q": FormatChar("<i8", "int"),
    "Q": FormatChar("<u8", "int"),
    "f": FormatChar("<f4", "float"),
    "d": FormatChar("<f8", "float"),
    "g": FormatChar("<f2", "float"),
    "n": FormatChar("S4", "str"),
    "N": FormatChar("S16", "str"),
    "Z": FormatChar("S64", "str"),
    "c": FormatChar("<i2", "int", 0.01),
    "C": FormatChar("<u2", "int", 0.01),
    "e": FormatChar("<i4", "int", 0.01),
    "E": FormatChar("<u4", "int", 0.01),
    "L": FormatChar("<i4", "int", 1e-7),  # latitude or longitude, degrees x 1e7
    "M": FormatChar("<u1", "int"),  # flight mode number
}

INSTANCE_UNIT = "#"  # FMTU unit id marking the instance column


class FormatError(ValueError):
    """An FMT message that cannot describe a message layout."""


def _np_dtype(ch: str) -> np.dtype[np.generic] | tuple[str, tuple[int]]:
    spec = FORMAT_CHARS[ch]
    if spec.kind == "array":
        return np.dtype((spec.dtype, (ARRAY_LEN,)))
    return np.dtype(spec.dtype)


def unique_names(columns: list[str], count: int) -> list[str]:
    """Column names usable as field names: padded to `count`, duplicates suffixed."""
    names: list[str] = []
    seen: set[str] = set()
    for i in range(count):
        base = columns[i] if i < len(columns) and columns[i] else f"_col{i}"
        name, n = base, 1
        while name in seen:
            n += 1
            name = f"{base}_{n}"
        seen.add(name)
        names.append(name)
    return names


@dataclass(eq=False)
class DfFormat:
    """One message type's layout, as an FMT message declared it."""

    type: int
    name: str
    length: int  # whole message, including the 3-byte header
    format: str
    columns: list[str]
    dtype: np.dtype[np.void]
    units: list[str] = field(default_factory=list[str])  # unit label per column, "" if none
    mults: list[float | None] = field(default_factory=list["float | None"])  # from FMTU/MULT
    instance_column: str | None = None

    @classmethod
    def build(cls, type_: int, name: str, length: int, fmt: str, columns: list[str]) -> DfFormat:
        unknown = [c for c in fmt if c not in FORMAT_CHARS]
        if unknown:
            raise FormatError(f"{name}: unknown format character {unknown[0]!r}")
        names = unique_names(columns, len(fmt))
        offsets: list[int] = []
        pos = 0
        for ch in fmt:
            offsets.append(pos)
            pos += np.dtype(_np_dtype(ch)).itemsize
        dtype = np.dtype(
            {
                "names": names,
                "formats": [_np_dtype(ch) for ch in fmt],
                "offsets": offsets,
                "itemsize": pos,
            }
        )
        return cls(type_, name, length, fmt, columns, dtype, [""] * len(fmt), [None] * len(fmt))

    @property
    def body_size(self) -> int:
        return self.length - 3

    @property
    def decodable(self) -> bool:
        """False when the declared length disagrees with the format, so no message decodes."""
        return self.dtype.itemsize == self.body_size

    @property
    def field_names(self) -> list[str]:
        return list(self.dtype.names or ())

    def apply_units(self, unit_ids: str, mult_ids: str, units: dict[str, str],
                    mults: dict[str, float]) -> None:  # fmt: skip
        """Attach FMTU unit and multiplier ids, resolved through the UNIT and MULT tables."""
        for i in range(len(self.format)):
            uid = unit_ids[i] if i < len(unit_ids) else "-"
            mid = mult_ids[i] if i < len(mult_ids) else "-"
            self.units[i] = units.get(uid, "")
            self.mults[i] = mults.get(mid)
        idx = unit_ids.find(INSTANCE_UNIT)
        self.instance_column = self.field_names[idx] if 0 <= idx < len(self.format) else None

    def is_position(self, i: int) -> bool:
        label = self.units[i].lower()
        return "latitude" in label or "longitude" in label


FMT_FORMAT = DfFormat.build(
    FMT_TYPE, "FMT", FMT_LENGTH, "BBnNZ", ["Type", "Length", "Name", "Format", "Columns"]
)


def text(raw: bytes) -> str:
    """Decode a fixed-width string field and cut it at its first NUL."""
    s = raw.decode(errors="backslashreplace")
    cut = s.find("\0")
    return s if cut < 0 else s[:cut]
