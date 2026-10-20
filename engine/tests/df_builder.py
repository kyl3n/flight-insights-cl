"""Build small DataFlash logs byte by byte, for tests."""

from __future__ import annotations

import struct

PACK = {
    "a": "32h", "b": "b", "B": "B", "h": "h", "H": "H", "i": "i", "I": "I", "q": "q",
    "Q": "Q", "f": "f", "d": "d", "g": "e", "n": "4s", "N": "16s", "Z": "64s", "c": "h",
    "C": "H", "e": "i", "E": "I", "L": "i", "M": "B",
}  # fmt: skip

UNITS = [("s", "s"), ("D", "deglatitude"), ("U", "deglongitude"), ("m", "m"), ("#", "instance")]
MULTS = [("-", 0.0), ("F", 1e-6), ("G", 1e-7), ("0", 1.0)]


def body_size(fmt: str) -> int:
    return struct.calcsize("<" + "".join(PACK[c] for c in fmt))


class LogBuilder:
    def __init__(self, units: bool = True) -> None:
        self.data = bytearray()
        self.formats: dict[str, tuple[int, str]] = {}
        self.fmt(0x80, "FMT", "BBnNZ", "Type,Length,Name,Format,Columns")
        if units:
            self.fmt(0x81, "UNIT", "QbZ", "TimeUS,Id,Label")
            self.fmt(0x82, "MULT", "Qbd", "TimeUS,Id,Mult")
            self.fmt(0x83, "FMTU", "QBNN", "TimeUS,FmtType,UnitIds,MultIds")
            for uid, label in UNITS:
                self.write("UNIT", 0, ord(uid), label.encode())
            for mid, mult in MULTS:
                self.write("MULT", 0, ord(mid), mult)

    def fmt(self, type_: int, name: str, fmt: str, columns: str, length: int | None = None) -> None:
        """Write an FMT message; `length` overrides the true length to make a bad one."""
        size = 3 + body_size(fmt) if length is None else length
        self.formats[name] = (type_, fmt)
        self.data += b"\xa3\x95\x80" + struct.pack(
            "<BB4s16s64s", type_, size, name.encode(), fmt.encode(), columns.encode()
        )

    def units(self, name: str, unit_ids: str, mult_ids: str) -> None:
        self.write("FMTU", 0, self.formats[name][0], unit_ids.encode(), mult_ids.encode())

    def write(self, name: str, *values: object) -> None:
        type_, fmt = self.formats[name]
        flat: list[object] = []
        for ch, v in zip(fmt, values, strict=True):
            flat.extend(v if ch == "a" else [v])  # type: ignore[arg-type]
        packed = struct.pack("<" + "".join(PACK[c] for c in fmt), *flat)
        self.data += b"\xa3\x95" + bytes([type_]) + packed

    def raw(self, data: bytes) -> None:
        self.data += data

    def bytes(self) -> bytes:
        return bytes(self.data)
