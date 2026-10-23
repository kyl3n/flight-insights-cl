#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pymavlink==2.4.50"]
# ///
"""Shift positions out of flight data so it can be committed as a test fixture.

Reads DataFlash logs (.bin), tlogs (.tlog) and parameter files (.param,
.params). Every latitude and longitude moves by one random shift shared by
all files of a session, so paired files still line up. The shift is never
printed or stored. Values of exactly zero, which mean "no position", stay
zero, and so do messages whose positions all lie within NO_POSITION_DEG of
0,0 (what autopilots report before their first fix), so they cannot reveal
the shift.

What is changed:
- DataFlash: every field whose FMTU unit is a latitude or longitude, found
  from the log's own FMT, FMTU and UNIT messages, not from message names.
- tlog: every field whose MAVLink unit is degE7, float fields in degrees
  named as a latitude or longitude, and the x/y or param5/param6 of
  commands whose MAV_CMD definition says they hold a latitude and longitude.
- Parameters named *_LAT, *_LNG or *_LON move by the same shift;
  COMPASS_DEC (which narrows down the region) is set to 0.
- Board unique IDs are zeroed: long hex runs in MSG and STATUSTEXT text,
  and AUTOPILOT_VERSION uid and uid2.
- Embedded files that copy the autopilot's storage (@SYS/storage.bin, which
  holds every parameter and the mission, fence and rally points) are zeroed.
- DataFlash bytes that are not part of a whole message (a message cut off at
  the end of the file, junk between messages) are zeroed: they cannot be
  decoded, so a position in them could not be shifted. The file keeps its
  length, so every message stays at its offset.

Absolute altitudes are not changed. Logs from real aircraft need a further
review before they become fixtures; see tests/fixtures/README.md.

After writing, each output is checked against the input with pymavlink's own
decoder: the same messages in the same order, every position moved by
exactly the shift, every other value unchanged, and none of the input's
position values (every sample of every position field) left anywhere in the
output bytes in any common encoding.

Usage:
  uv run tools/fixtures/scrub_fixture.py --out DIR [--manifest FILE] FILE...
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import math
import re
import secrets
import struct
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Kind = Literal["lat", "lng"]

POSITION_PARAM = re.compile(r"_(LAT|LNG|LON)$")
ZEROED_PARAMS = frozenset({"COMPASS_DEC"})
# Two or more 8-digit hex groups, as boards print their unique IDs.
HEX_ID_RUN = re.compile(rb"[0-9A-Fa-f]{8}(?:[ :-]?[0-9A-Fa-f]{8})+")
HEX_ID_SEPARATORS = frozenset(b" :-")
# Embedded log files whose content is blanked: raw storage holds missions and parameters.
BLANKED_FILES = frozenset({"@SYS/storage.bin"})

MIN_SHIFT_DEG = 5.0
NO_POSITION_DEG = 0.01  # about 1 km: closer to 0,0 than this is a placeholder, not a place
LAT_LIMIT_DEG = 80.0


class ScrubError(Exception):
    """The file cannot be scrubbed safely."""


def name_kind(name: str) -> Kind | None:
    low = name.lower()
    if "lat" in low:
        return "lat"
    if "lon" in low or "lng" in low:
        return "lng"
    return None


def is_placeholder(degrees: Sequence[float]) -> bool:
    """True if a message's non-zero positions all sit near 0,0: no fix yet, not a place."""
    return bool(degrees) and all(abs(d) < NO_POSITION_DEG for d in degrees)


def param_kind(name: str) -> Kind | None:
    m = POSITION_PARAM.search(name)
    if m is None:
        return None
    return "lat" if m.group(1) == "LAT" else "lng"


@dataclass(frozen=True)
class Shift:
    lat: float
    lng: float

    def apply(self, kind: Kind, deg: float) -> float:
        if kind == "lat":
            out = deg + self.lat
            if not -90.0 <= out <= 90.0:
                raise ScrubError("a shifted latitude falls outside -90..90")
            return out
        return (deg + self.lng + 180.0) % 360.0 - 180.0


def choose_shift(lats: Sequence[float]) -> Shift:
    """A random shift that keeps every latitude inside +-LAT_LIMIT_DEG."""
    rng = secrets.SystemRandom()
    lo, hi = (min(lats), max(lats)) if lats else (0.0, 0.0)
    ranges = [
        (r0, r1)
        for r0, r1 in (
            (-LAT_LIMIT_DEG - lo, -MIN_SHIFT_DEG),
            (MIN_SHIFT_DEG, LAT_LIMIT_DEG - hi),
        )
        if r1 > r0
    ]
    if not ranges:
        raise ScrubError("latitudes span too wide a band to shift")
    total = sum(r1 - r0 for r0, r1 in ranges)
    pick = rng.uniform(0.0, total)
    for r0, r1 in ranges:
        if pick <= r1 - r0:
            lat = r0 + pick
            break
        pick -= r1 - r0
    else:
        lat = ranges[-1][1]
    lng = rng.uniform(MIN_SHIFT_DEG, 180.0) * rng.choice((-1.0, 1.0))
    return Shift(lat, lng)


@dataclass(frozen=True)
class PosRef:
    """One non-zero position value. `scale` turns the stored number into degrees."""

    loc: tuple[int, str]  # (byte offset, struct code) or (tlog record, field)
    what: str
    kind: Kind
    scale: float
    is_float: bool
    value: float

    @property
    def deg(self) -> float:
        return self.value * self.scale

    def encode(self, shift: Shift) -> float:
        new = shift.apply(self.kind, self.deg) / self.scale
        return new if self.is_float else round(new)


@dataclass(frozen=True)
class Edit:
    """A value set to zero (parameter, board ID, embedded file) or text with hex IDs blanked."""

    loc: tuple[int, str]
    what: str
    action: Literal["zero", "hex_ids"] = "zero"


@dataclass
class Plan:
    positions: list[PosRef] = field(default_factory=list[PosRef])
    edits: list[Edit] = field(default_factory=list[Edit])
    counts: Counter[str] = field(default_factory=Counter[str])
    unparsed: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])

    @property
    def skipped_bytes(self) -> int:
        return sum(end - start for start, end in self.unparsed)

    def skip(self, offset: int) -> None:
        """Record one byte that starts no message, merging runs into [start, end) ranges."""
        if self.unparsed and self.unparsed[-1][1] == offset:
            self.unparsed[-1] = (self.unparsed[-1][0], offset + 1)
        else:
            self.unparsed.append((offset, offset + 1))

    def degrees(self, kind: Kind) -> list[float]:
        return [p.deg for p in self.positions if p.kind == kind]

    def sentinels(self) -> list[float]:
        """Every position value in the input: none may survive scrubbing."""
        return list(dict.fromkeys(p.deg for p in self.positions))


def blank_hex_ids(raw: bytes) -> bytes:
    def zero(m: re.Match[bytes]) -> bytes:
        return bytes(b if b in HEX_ID_SEPARATORS else ord("0") for b in m.group(0))

    return HEX_ID_RUN.sub(zero, raw)


# --- DataFlash ---------------------------------------------------------------

DF_HEAD = b"\xa3\x95"
DF_FMT_ID = 0x80
# The FMT message describes itself: Type B, Length B, Name n, Format N, Columns Z.
DF_FMT_LENGTH = 89
DF_STRUCT = {
    "a": "64s", "b": "b", "B": "B", "g": "e", "h": "h", "H": "H", "i": "i", "I": "I",
    "f": "f", "n": "4s", "N": "16s", "Z": "64s", "c": "h", "C": "H", "e": "i", "E": "I",
    "L": "i", "d": "d", "M": "B", "q": "q", "Q": "Q",
}  # fmt: skip
DF_IMPLICIT_SCALE = {"L": 1e-7, "c": 0.01, "C": 0.01, "e": 0.01, "E": 0.01}
DF_FLOATS = frozenset("fdg")


@dataclass(eq=False)
class DfFormat:
    type: int
    length: int
    name: str
    format: str
    columns: list[str]
    codes: list[str]
    offsets: list[int]


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def _df_format(type_: int, length: int, name: str, fmt: str, columns: list[str]) -> DfFormat:
    codes: list[str] = []
    offsets: list[int] = []
    pos = 0
    for ch in fmt:
        code = DF_STRUCT.get(ch)
        if code is None:
            raise ScrubError(f"{name}: unknown format character {ch!r}")
        codes.append(code)
        offsets.append(pos)
        pos += struct.calcsize("<" + code)
    if pos + 3 != length:
        raise ScrubError(f"{name}: format {fmt!r} does not fill its length {length}")
    if len(columns) != len(fmt):
        raise ScrubError(f"{name}: {len(columns)} columns for format {fmt!r}")
    return DfFormat(type_, length, name, fmt, columns, codes, offsets)


DF_FMT = _df_format(
    DF_FMT_ID,
    DF_FMT_LENGTH,
    "FMT",
    "BBnNZ",
    ["Type", "Length", "Name", "Format", "Columns"],
)


def iter_dataflash(data: bytes) -> Iterator[tuple[int, DfFormat | None]]:
    """Yield (offset, format) per message, or (offset, None) per byte that starts none."""
    formats = {DF_FMT_ID: DF_FMT}
    o, n = 0, len(data)
    while o < n:
        if data[o : o + 2] == DF_HEAD and o + 2 < n:
            fmt = formats.get(data[o + 2])
            if fmt is not None and o + fmt.length <= n:
                if fmt is DF_FMT:
                    t, length, name, f, cols = struct.unpack_from("<BB4s16s64s", data, o + 3)
                    if t != DF_FMT_ID:  # the log's own FMT entry restates the bootstrap
                        columns = _cstr(cols).split(",") if _cstr(cols) else []
                        formats[t] = _df_format(t, length, _cstr(name), _cstr(f), columns)
                yield o, fmt
                o += fmt.length
                continue
        yield o, None
        o += 1


def _df_values(data: bytes, o: int, fmt: DfFormat) -> dict[str, object]:
    return {
        col: struct.unpack_from("<" + code, data, o + 3 + off)[0]
        for col, code, off in zip(fmt.columns, fmt.codes, fmt.offsets, strict=True)
    }


@dataclass
class DfMeta:
    units: dict[str, str] = field(default_factory=dict[str, str])
    mults: dict[str, float] = field(default_factory=dict[str, float])
    fmtu: dict[int, tuple[str, str]] = field(default_factory=dict[int, tuple[str, str]])


def read_df_meta(data: bytes) -> DfMeta:
    meta = DfMeta()
    for o, fmt in iter_dataflash(data):
        if fmt is None or fmt.name not in ("UNIT", "MULT", "FMTU"):
            continue
        v = _df_values(data, o, fmt)
        if fmt.name == "UNIT":
            meta.units[chr(int(v["Id"]) & 0xFF)] = _cstr(v["Label"])  # type: ignore[arg-type]
        elif fmt.name == "MULT":
            meta.mults[chr(int(v["Id"]) & 0xFF)] = float(v["Mult"])  # type: ignore[arg-type]
        else:
            meta.fmtu[int(v["FmtType"])] = (_cstr(v["UnitIds"]), _cstr(v["MultIds"]))  # type: ignore[arg-type]
    return meta


@dataclass(frozen=True)
class DfField:
    index: int
    kind: Kind
    scale: float
    is_float: bool
    implicit: bool = False  # the format character carries the scale, so pymavlink applies it


def df_position_fields(fmt: DfFormat, meta: DfMeta) -> list[DfField]:
    ids = meta.fmtu.get(fmt.type)
    out: list[DfField] = []
    for i, (ch, col) in enumerate(zip(fmt.format, fmt.columns, strict=True)):
        if ids is not None:
            unit = meta.units.get(ids[0][i] if i < len(ids[0]) else "-", "").lower()
            kind: Kind | None = (
                "lat" if "latitude" in unit else "lng" if "longitude" in unit else None
            )
            if kind is None and ch == "L":
                raise ScrubError(f"{fmt.name}.{col}: format L without a latitude/longitude unit")
        elif ch == "L":
            kind = name_kind(col)
            if kind is None:
                raise ScrubError(f"{fmt.name}.{col}: format L with no FMTU and no lat/lng name")
        else:
            kind = None
        if kind is None:
            continue
        mult = meta.mults.get(ids[1][i]) if ids is not None and i < len(ids[1]) else None
        if ch in DF_IMPLICIT_SCALE:
            # The format character already carries the multiplier; FMTU only restates it.
            out.append(DfField(i, kind, DF_IMPLICIT_SCALE[ch], False, implicit=True))
        elif ch in DF_FLOATS:
            out.append(DfField(i, kind, mult if mult else 1.0, True))
        elif mult:
            out.append(DfField(i, kind, mult, False))
        else:
            raise ScrubError(f"{fmt.name}.{col}: integer position with no multiplier")
    return out


def plan_dataflash(data: bytes) -> Plan:
    meta = read_df_meta(data)
    plan = Plan()
    fields: dict[DfFormat, list[DfField]] = {}
    for o, fmt in iter_dataflash(data):
        if fmt is None:
            plan.skip(o)
            continue
        plan.counts[fmt.name] += 1
        if fmt not in fields:
            fields[fmt] = df_position_fields(fmt, meta)
        base = o + 3
        refs: list[PosRef] = []
        for f in fields[fmt]:
            code = fmt.codes[f.index]
            (raw,) = struct.unpack_from("<" + code, data, base + fmt.offsets[f.index])
            if raw == 0 or not math.isfinite(raw):
                continue
            what = f"{fmt.name}.{fmt.columns[f.index]}"
            loc = (base + fmt.offsets[f.index], code)
            refs.append(PosRef(loc, what, f.kind, f.scale, f.is_float, raw))
        if not is_placeholder([r.deg for r in refs]):
            plan.positions += refs
        if fmt.name == "PARM":
            _plan_df_parm(data, base, fmt, plan)
        elif fmt.name == "MSG" and "Message" in fmt.columns:
            i = fmt.columns.index("Message")
            raw = data[base + fmt.offsets[i] : base + fmt.offsets[i] + 64]
            if blank_hex_ids(raw) != raw:
                loc = (base + fmt.offsets[i], fmt.codes[i])
                plan.edits.append(Edit(loc, "MSG.Message", "hex_ids"))
        elif fmt.name == "FILE" and _df_file_blanked(data, base, fmt):
            i = fmt.columns.index("Data")
            plan.edits.append(Edit((base + fmt.offsets[i], fmt.codes[i]), "FILE.Data"))
    return plan


def _df_file_blanked(data: bytes, base: int, fmt: DfFormat) -> bool:
    """True if this FILE chunk belongs to a blanked file and still has content."""
    v = _df_values(data, base - 3, fmt)
    return _cstr(v["FileName"]) in BLANKED_FILES and any(v["Data"])  # type: ignore[arg-type]


def _plan_df_parm(data: bytes, base: int, fmt: DfFormat, plan: Plan) -> None:
    name = _cstr(struct.unpack_from("<16s", data, base + fmt.offsets[fmt.columns.index("Name")])[0])
    kind = param_kind(name)
    for col in ("Value", "Default"):
        if col not in fmt.columns:
            continue
        i = fmt.columns.index(col)
        loc = (base + fmt.offsets[i], fmt.codes[i])
        (raw,) = struct.unpack_from("<" + fmt.codes[i], data, loc[0])
        if raw == 0 or not math.isfinite(raw):
            continue
        if kind is not None:
            if not is_placeholder([raw]):
                plan.positions.append(PosRef(loc, f"PARM {name}.{col}", kind, 1.0, True, raw))
        elif name in ZEROED_PARAMS:
            plan.edits.append(Edit(loc, f"PARM {name}.{col}"))


def apply_dataflash(data: bytes, plan: Plan, shift: Shift) -> bytes:
    out = bytearray(data)
    for p in plan.positions:
        struct.pack_into("<" + p.loc[1], out, p.loc[0], p.encode(shift))
    for e in plan.edits:
        offset, code = e.loc
        size = struct.calcsize("<" + code)
        if e.action == "hex_ids":
            out[offset : offset + size] = blank_hex_ids(bytes(out[offset : offset + size]))
        else:
            out[offset : offset + size] = bytes(size)
    for (
        start,
        end,
    ) in plan.unparsed:  # cannot be decoded, so cannot be shifted: blank it
        out[start:end] = bytes(end - start)
    return bytes(out)


def _changed_ranges(plan: Plan) -> list[tuple[int, int]]:
    locs = [p.loc for p in plan.positions] + [e.loc for e in plan.edits]
    ranges = [(o, o + struct.calcsize("<" + code)) for o, code in locs]
    return sorted(ranges + plan.unparsed)


def verify_dataflash(original: Path, scrubbed: Path, shift: Shift) -> list[str]:
    from pymavlink import DFReader

    a, b = original.read_bytes(), scrubbed.read_bytes()
    problems: list[str] = []
    if len(a) != len(b):
        return [f"{scrubbed.name}: size changed"]
    plan = plan_dataflash(a)
    pos = 0
    for start, end in _changed_ranges(plan):
        if a[pos:start] != b[pos:start]:
            problems.append(f"{scrubbed.name}: bytes changed outside position fields")
            break
        pos = max(pos, end)
    if a[pos:] != b[pos:]:
        problems.append(f"{scrubbed.name}: bytes changed outside position fields")

    if any(b[start:end].strip(b"\0") for start, end in plan.unparsed):
        problems.append(f"{scrubbed.name}: unparsed bytes not blanked")

    after = plan_dataflash(b)
    if after.counts != plan.counts or after.unparsed != plan.unparsed:
        problems.append(f"{scrubbed.name}: message counts changed")
    if after.edits:
        problems.append(f"{scrubbed.name}: board IDs or zeroed parameters left")

    # Independent check with pymavlink's reader: same messages, positions moved by the shift.
    meta = read_df_meta(a)
    columns: dict[str, dict[str, DfField]] = {}
    for _, fmt in iter_dataflash(a):
        if fmt is not None and fmt.name not in columns:
            columns[fmt.name] = {fmt.columns[f.index]: f for f in df_position_fields(fmt, meta)}
    ra = DFReader.DFReader_binary(str(original), zero_time_base=False)
    rb = DFReader.DFReader_binary(str(scrubbed), zero_time_base=False)
    seen = 0
    while True:
        ma, mb = ra.recv_msg(), rb.recv_msg()
        if ma is None or mb is None:
            if ma is not mb:
                problems.append(f"{scrubbed.name}: pymavlink reads a different number of messages")
            break
        seen += 1
        typ = ma.get_type()
        if typ != mb.get_type():
            problems.append(f"{scrubbed.name}: message {seen} type differs")
            break
        pos_cols = columns.get(typ, {})
        placeholder = is_placeholder(
            [
                getattr(ma, c) * (1.0 if f.implicit else f.scale)
                for c, f in pos_cols.items()
                if getattr(ma, c) != 0 and math.isfinite(getattr(ma, c))
            ]
        )
        parm_kind = param_kind(str(ma.Name)) if typ == "PARM" else None
        parm_zero = typ == "PARM" and str(ma.Name) in ZEROED_PARAMS
        for col in ma.get_fieldnames():
            va, vb = getattr(ma, col), getattr(mb, col)
            if (col in pos_cols and placeholder) or (
                parm_kind is not None and col in ("Value", "Default") and is_placeholder([va])
            ):
                problem = None if _same(va, vb) else "placeholder position changed"
            elif col in pos_cols:
                f = pos_cols[col]
                to_deg = 1.0 if f.implicit else f.scale
                problem = _check_shifted(
                    va * to_deg,
                    vb * to_deg,
                    f.kind,
                    shift,
                    None if f.is_float else f.scale,
                )
            elif parm_kind is not None and col in ("Value", "Default"):
                problem = _check_shifted(va, vb, parm_kind, shift, None)
            elif parm_zero and col in ("Value", "Default"):
                problem = None if vb == 0 else "not zeroed"
            elif typ == "MSG" and col == "Message":
                problem = None if len(va) == len(vb) else "length changed"
            elif typ == "FILE" and col == "Data" and str(ma.FileName) in BLANKED_FILES:
                problem = None if not _as_bytes(vb).strip(b"\0") else "not blanked"
            else:
                problem = None if _same(va, vb) else "changed"
            if problem:
                problems.append(f"{scrubbed.name}: {typ}.{col} {problem}")
                break
        if len(problems) > 20:
            break
    return problems


def _same(a: object, b: object) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def _as_bytes(value: object) -> bytes:
    return value if isinstance(value, bytes) else str(value).encode("latin-1", "replace")


def _check_shifted(
    old: object, new: object, kind: Kind, shift: Shift, lsb: float | None
) -> str | None:
    """None if `new` is `old` moved by the shift; `lsb` is the integer step in degrees."""
    if not isinstance(old, int | float) or not isinstance(new, int | float):
        return "is not a number"
    if old == 0 or not math.isfinite(old):
        return None if _same(old, new) else "zero or invalid value changed"
    expected = shift.apply(kind, float(old))
    if lsb is not None:
        ok = abs(new - expected) <= lsb * 1.01
    else:
        f32 = struct.unpack("<f", struct.pack("<f", expected))[0]
        ok = math.isclose(new, f32, rel_tol=0, abs_tol=1e-9) or math.isclose(
            new, expected, rel_tol=0, abs_tol=1e-9
        )
    return None if ok else "not moved by the shift"


# --- tlog ---------------------------------------------------------------------


def _mavlink():  # imported lazily so --help works without pymavlink
    from pymavlink.dialects.v20 import ardupilotmega

    return ardupilotmega


@dataclass(frozen=True)
class Record:
    timestamp: bytes
    packet: bytes
    v2: bool
    signed: bool
    msgid: int


def iter_tlog(data: bytes) -> Iterator[Record]:
    """Each tlog record is an 8-byte big-endian microsecond time, then one MAVLink packet."""
    o, n = 0, len(data)
    while o < n:
        if o + 10 > n:
            raise ScrubError(f"truncated tlog record at offset {o}")
        magic, length = data[o + 8], data[o + 9]
        if magic == 0xFE:
            size, v2, signed, msgid = 8 + length, False, False, data[o + 13]
        elif magic == 0xFD:
            signed = bool(data[o + 10] & 0x01)
            size, v2 = 12 + length + (13 if signed else 0), True
            msgid = int.from_bytes(data[o + 15 : o + 18], "little")
        else:
            raise ScrubError(f"tlog offset {o}: no MAVLink packet after the timestamp")
        end = o + 8 + size
        if end > n:
            raise ScrubError(f"truncated tlog record at offset {o}")
        yield Record(data[o : o + 8], data[o + 8 : end], v2, signed, msgid)
        o = end


_FIELD_TOKEN = re.compile(r"(\d*)([a-zA-Z?])")


def _wire_layout(cls: type) -> dict[str, tuple[int, str]]:
    """Field name -> (offset in the full payload, struct code), in wire order."""
    tokens = _FIELD_TOKEN.findall(cls.unpacker.format.lstrip("<"))  # type: ignore[attr-defined]
    names: list[str] = cls.ordered_fieldnames  # type: ignore[attr-defined]
    if len(tokens) != len(names):
        raise ScrubError(f"{cls.msgname}: cannot map its wire format")  # type: ignore[attr-defined]
    layout: dict[str, tuple[int, str]] = {}
    pos = 0
    for name, (count, ch) in zip(names, tokens, strict=True):
        code = f"{count}{ch}"
        layout[name] = (pos, code)
        pos += struct.calcsize("<" + code)
    return layout


def _cmd_latlng(mav: object, command: int) -> bool:
    entry = mav.enums["MAV_CMD"].get(command)  # type: ignore[attr-defined]
    if entry is None:
        return True  # unknown command: shift to be safe
    labels = {i: str(entry.param.get(i, "")).lower() for i in (5, 6)}
    return "latitude" in labels[5] and "longitude" in labels[6]


def _global_frame(mav: object, frame: int) -> bool:
    entry = mav.enums["MAV_FRAME"].get(frame)  # type: ignore[attr-defined]
    return entry is None or "GLOBAL" in entry.name


# message -> (latitude field, longitude field, scale, depends on the frame)
TLOG_COMMAND_POSITIONS: dict[str, tuple[str, str, float, bool]] = {
    "COMMAND_LONG": ("param5", "param6", 1.0, False),
    "COMMAND_INT": ("x", "y", 1e-7, True),
    "MISSION_ITEM_INT": ("x", "y", 1e-7, True),
    "MISSION_ITEM": ("x", "y", 1.0, True),
}
TLOG_ZEROED_FIELDS = {"AUTOPILOT_VERSION": ("uid", "uid2")}


def tlog_position_fields(msg: object) -> list[tuple[str, Kind, float, bool]]:
    """(field, kind, scale, is_float) for every position in this message."""
    mav = _mavlink()
    cls = type(msg)
    name: str = cls.msgname  # type: ignore[attr-defined]
    units: dict[str, str] = getattr(cls, "fieldunits_by_name", {})
    out: list[tuple[str, Kind, float, bool]] = []
    for f in cls.fieldnames:  # type: ignore[attr-defined]
        unit = units.get(f)
        kind = name_kind(f)
        if unit == "degE7":
            if kind is None:
                raise ScrubError(f"{name}.{f}: degE7 field that is not a latitude or longitude")
            out.append((f, kind, 1e-7, False))
        elif unit == "deg" and kind is not None:
            out.append((f, kind, 1.0, True))
    cmd = TLOG_COMMAND_POSITIONS.get(name)
    if cmd is not None:
        lat_f, lng_f, scale, by_frame = cmd
        positional = _cmd_latlng(mav, int(msg.command))  # type: ignore[attr-defined]
        if by_frame:
            positional = positional and _global_frame(mav, int(msg.frame))  # type: ignore[attr-defined]
        if positional:
            is_float = scale == 1.0
            out += [(lat_f, "lat", scale, is_float), (lng_f, "lng", scale, is_float)]
    if name in ("PARAM_VALUE", "PARAM_SET"):
        kind = param_kind(_text(msg.param_id))  # type: ignore[attr-defined]
        if kind is not None:
            out.append(("param_value", kind, 1.0, True))
    return out


def _text(value: object) -> str:
    return value.decode("ascii", "replace") if isinstance(value, bytes) else str(value)


def _tlog_edits(msg: object) -> list[str]:
    name = type(msg).msgname  # type: ignore[attr-defined]
    edits = [f for f in TLOG_ZEROED_FIELDS.get(name, ()) if _nonzero(getattr(msg, f))]
    is_param = name in ("PARAM_VALUE", "PARAM_SET")
    if is_param and _text(msg.param_id) in ZEROED_PARAMS and msg.param_value != 0:  # type: ignore[attr-defined]
        edits.append("param_value")
    if name == "STATUSTEXT":
        raw = _text(msg.text).encode("ascii", "replace")  # type: ignore[attr-defined]
        if blank_hex_ids(raw) != raw:
            edits.append("text")
    return edits


def _nonzero(value: object) -> bool:
    if isinstance(value, list | tuple):
        return any(value)
    return bool(value)


def _decoder():
    mav = _mavlink().MAVLink(None)
    mav.robust_parsing = False
    return mav


def plan_tlog(data: bytes) -> Plan:
    mav = _decoder()
    plan = Plan()
    for i, rec in enumerate(iter_tlog(data)):
        try:
            msg = mav.decode(bytearray(rec.packet))
        except Exception as e:  # pymavlink raises its own error types
            raise ScrubError(f"tlog record {i}: pymavlink cannot decode it ({e})") from e
        name = msg.get_type()
        plan.counts[name] += 1
        refs: list[PosRef] = []
        for f, kind, scale, is_float in tlog_position_fields(msg):
            raw = getattr(msg, f)
            if raw == 0 or not math.isfinite(raw):
                continue
            refs.append(PosRef((i, f), f"{name}.{f}", kind, scale, is_float, raw))
        if not is_placeholder([r.deg for r in refs]):
            plan.positions += refs
        plan.edits += [
            Edit((i, f), f"{name}.{f}", "hex_ids" if f == "text" else "zero")
            for f in _tlog_edits(msg)
        ]
    return plan


def apply_tlog(data: bytes, plan: Plan, shift: Shift) -> bytes:
    mavlink = _mavlink()
    mav = _decoder()
    changes: dict[int, dict[str, PosRef | Edit]] = {}
    for c in [*plan.positions, *plan.edits]:
        changes.setdefault(c.loc[0], {})[c.loc[1]] = c
    out = bytearray()
    for i, rec in enumerate(iter_tlog(data)):
        out += rec.timestamp
        if i not in changes:
            out += rec.packet
            continue
        if rec.signed:
            raise ScrubError(f"tlog record {i}: signed packet with a position in it")
        msg = mav.decode(bytearray(rec.packet))
        cls = type(msg)
        layout = _wire_layout(cls)
        header = 10 if rec.v2 else 6
        length = rec.packet[1]
        full = bytearray(cls.unpacker.size)
        full[:length] = rec.packet[header : header + length]
        for f, c in changes[i].items():
            offset, code = layout[f]
            size = struct.calcsize("<" + code)
            if isinstance(c, PosRef):
                struct.pack_into("<" + code, full, offset, c.encode(shift))
            elif c.action == "hex_ids":
                full[offset : offset + size] = blank_hex_ids(bytes(full[offset : offset + size]))
            else:
                full[offset : offset + size] = bytes(size)
        if rec.v2:
            end = len(full)
            while end > 1 and full[end - 1] == 0:
                end -= 1
            payload = bytes(full[:end])
        else:
            payload = bytes(full[:length])
        head = bytearray(rec.packet[:header])
        head[1] = len(payload)
        crc = mavlink.x25crc(bytes(head[1:]) + payload)
        crc.accumulate(struct.pack("B", cls.crc_extra))
        out += bytes(head) + payload + struct.pack("<H", crc.crc)
    return bytes(out)


def verify_tlog(original: Path, scrubbed: Path, shift: Shift) -> list[str]:
    a, b = original.read_bytes(), scrubbed.read_bytes()
    ra, rb = list(iter_tlog(a)), list(iter_tlog(b))
    if len(ra) != len(rb):
        return [f"{scrubbed.name}: record count changed"]
    mav_a, mav_b = _decoder(), _decoder()
    problems: list[str] = []
    for i, (x, y) in enumerate(zip(ra, rb, strict=True)):
        if (x.timestamp, x.msgid, x.v2) != (y.timestamp, y.msgid, y.v2):
            problems.append(f"{scrubbed.name}: record {i} header changed")
            break
        ma = mav_a.decode(bytearray(x.packet))
        try:
            mb = mav_b.decode(bytearray(y.packet))  # also checks the CRC
        except Exception as e:
            problems.append(f"{scrubbed.name}: record {i} no longer decodes ({e})")
            break
        if (ma.get_srcSystem(), ma.get_srcComponent(), ma.get_seq()) != (
            mb.get_srcSystem(),
            mb.get_srcComponent(),
            mb.get_seq(),
        ):
            problems.append(f"{scrubbed.name}: record {i} sender or sequence changed")
            break
        positions = {
            f: (kind, scale, is_float) for f, kind, scale, is_float in tlog_position_fields(ma)
        }
        edited = set(_tlog_edits(ma))
        placeholder = is_placeholder(
            [
                getattr(ma, f) * scale
                for f, (_, scale, _) in positions.items()
                if getattr(ma, f) != 0 and math.isfinite(getattr(ma, f))
            ]
        )
        for f in ma.get_fieldnames():
            va, vb = getattr(ma, f), getattr(mb, f)
            if f in positions and placeholder:
                problem = None if _same(va, vb) else "placeholder position changed"
            elif f in positions:
                kind, scale, is_float = positions[f]
                if va == 0 or not math.isfinite(va):
                    problem = None if _same(va, vb) else "zero or invalid value changed"
                else:
                    problem = _check_shifted(
                        va * scale, vb * scale, kind, shift, None if is_float else scale
                    )
            elif f in edited:
                problem = None if (f == "text" or not _nonzero(vb)) else "not zeroed"
            else:
                problem = None if _same(va, vb) else "changed"
            if problem:
                problems.append(f"{scrubbed.name}: record {i} {ma.get_type()}.{f} {problem}")
                break
        if len(problems) > 20:
            break
    after = plan_tlog(b)
    if after.edits:
        problems.append(f"{scrubbed.name}: board IDs or zeroed parameters left")
    return problems


# --- parameter files ------------------------------------------------------------

MP_LINE = re.compile(r"^(\s*)([A-Za-z0-9_]+)(\s*[,\s]\s*)([-+0-9.eE]+)(.*)$", re.DOTALL)


@dataclass(frozen=True)
class ParamLine:
    index: int
    name: str
    value: float
    span: tuple[int, int]  # where the value text sits in the line


def parse_params(text: str, qgc: bool) -> list[ParamLine]:
    out: list[ParamLine] = []
    for i, line in enumerate(text.splitlines(keepends=True)):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if qgc:
            parts = line.split("\t")
            if len(parts) < 5:
                raise ScrubError(f"line {i + 1}: not a QGroundControl parameter line")
            start = sum(len(p) + 1 for p in parts[:3])
            name, value = parts[2], parts[3]
            out.append(ParamLine(i, name, float(value), (start, start + len(value))))
        else:
            m = MP_LINE.match(line)
            if m is None:
                raise ScrubError(f"line {i + 1}: not a NAME,VALUE parameter line")
            out.append(ParamLine(i, m.group(2), float(m.group(4)), m.span(4)))
    return out


def plan_params(text: str, qgc: bool) -> Plan:
    plan = Plan()
    for p in parse_params(text, qgc):
        plan.counts["param"] += 1
        kind = param_kind(p.name)
        if kind is not None and p.value != 0 and not is_placeholder([p.value]):
            plan.positions.append(PosRef((p.index, p.name), p.name, kind, 1.0, True, p.value))
        elif p.name in ZEROED_PARAMS and p.value != 0:
            plan.edits.append(Edit((p.index, p.name), p.name))
    return plan


def format_param(value: float) -> str:
    return f"{value:.7f}".rstrip("0").rstrip(".") or "0"


def apply_params(text: str, qgc: bool, plan: Plan, shift: Shift) -> str:
    lines = text.splitlines(keepends=True)
    new_values: dict[int, float] = {p.loc[0]: shift.apply(p.kind, p.value) for p in plan.positions}
    new_values |= {e.loc[0]: 0.0 for e in plan.edits}
    for p in parse_params(text, qgc):
        if p.index in new_values:
            line = lines[p.index]
            lines[p.index] = (
                line[: p.span[0]] + format_param(new_values[p.index]) + line[p.span[1] :]
            )
    return "".join(lines)


def verify_params(original: Path, scrubbed: Path, qgc: bool, shift: Shift) -> list[str]:
    a = parse_params(original.read_text(), qgc)
    b = parse_params(scrubbed.read_text(), qgc)
    if [p.name for p in a] != [p.name for p in b]:
        return [f"{scrubbed.name}: parameter names or order changed"]
    problems: list[str] = []
    for x, y in zip(a, b, strict=True):
        kind = param_kind(x.name)
        if kind is not None and x.value != 0 and not is_placeholder([x.value]):
            ok = abs(y.value - shift.apply(kind, x.value)) <= 1e-6
        elif x.name in ZEROED_PARAMS:
            ok = y.value == 0
        else:
            ok = x.value == y.value
        if not ok:
            problems.append(f"{scrubbed.name}: {x.name} wrong after scrubbing")
    return problems


# --- leak search, manifest and command line ----------------------------------


def _leak_encodings(deg: float) -> tuple[list[bytes], bytes]:
    """(binary encodings: float32, float64, int32 x1e7; decimal text to four places)."""
    text = f"{deg:.7f}"
    binary = [struct.pack("<f", deg), struct.pack("<d", deg)]
    raw = round(deg * 1e7)
    if raw:
        binary.append(struct.pack("<i", raw))
    return binary, text[: text.index(".") + 5].encode()


def leak_patterns(deg: float) -> list[bytes]:
    """Common encodings of a coordinate: int32 x1e7, float32, float64, and decimal text."""
    binary, text = _leak_encodings(deg)
    return [*binary, text]


LEAK_TEXT = re.compile(rb"\.[0-9]{4}")


def _windows(data: bytes, size: int) -> Iterator[memoryview]:
    """Every `size`-byte window of `data`, as `size` views of fixed-width integers."""
    code = {4: "I", 8: "Q"}[size]
    view = memoryview(data)
    for start in range(min(size, len(data))):
        usable = (len(data) - start) // size * size
        yield view[start : start + usable].cast(code)


def _present(data: bytes, size: int, wanted: set[int]) -> set[int]:
    """Which of the `size`-byte integers in `wanted` appear anywhere in `data`."""
    hits: set[int] = set()
    for view in _windows(data, size):
        hits |= wanted.intersection(view)
        view.release()
    return hits


def _offsets(data: bytes, patterns: Iterable[bytes]) -> list[int]:
    out: list[int] = []
    for pat in patterns:
        start = 0
        while (i := data.find(pat, start)) != -1:
            out.append(i)
            start = i + 1
    return sorted(out)


def _text_hits(data: bytes, texts: set[bytes]) -> dict[bytes, list[int]]:
    hits: dict[bytes, list[int]] = {}
    for m in LEAK_TEXT.finditer(data):  # a text pattern is a sign, up to 3 digits, ".dddd"
        dot = m.start()
        for start in range(max(0, dot - 4), dot):
            if data[start : dot + 5] in texts:
                hits.setdefault(data[start : dot + 5], []).append(start)
    return hits


def find_leaks(data: bytes, sentinels: Sequence[float]) -> int:
    """How many distinct encodings of the sentinel positions appear anywhere in `data`.

    The same search as `pattern in data` for every pattern of every sentinel, but
    one pass per window width. A 4-byte pattern can turn up by chance in a large
    log, so search every sample of a log with `find_position_leaks` instead.
    """
    by_size: dict[int, set[int]] = {4: set(), 8: set()}
    texts: set[bytes] = set()
    for deg in sentinels:
        binary, text = _leak_encodings(deg)
        texts.add(text)
        for pat in binary:
            by_size[len(pat)].add(int.from_bytes(pat, sys.byteorder))
    found = sum(len(_present(data, size, wanted)) for size, wanted in by_size.items())
    return found + len(_text_hits(data, texts))


PAIR_BYTES = 32  # a latitude and a longitude this close together are one position


def find_position_leaks(data: bytes, lats: Sequence[float], lngs: Sequence[float]) -> int:
    """Encodings of the original positions left in `data`, every sample searched.

    Decimal text and 8-byte doubles count wherever they appear: a chance match of
    eight exact bytes does not happen. A 4-byte value (int32 x1e7 or float32) does
    turn up by chance among the millions of 4-byte windows of a large log, so it
    counts only beside a 4-byte encoding of a longitude (or latitude), as a
    position is stored: that pairing does not happen by chance.
    """
    texts: set[bytes] = set()
    doubles: set[int] = set()
    small: dict[str, set[int]] = {"lat": set(), "lng": set()}
    for kind, degs in (("lat", lats), ("lng", lngs)):
        for deg in degs:
            binary, text = _leak_encodings(deg)
            texts.add(text)
            for pat in binary:
                if len(pat) == 8:
                    doubles.add(int.from_bytes(pat, sys.byteorder))
                else:
                    small[kind].add(int.from_bytes(pat, sys.byteorder))
    found = len(_present(data, 8, doubles)) + len(_text_hits(data, texts))
    present = {kind: _present(data, 4, wanted) for kind, wanted in small.items()}
    if present["lat"] and present["lng"]:

        def at(values: set[int]) -> list[int]:
            return _offsets(data, (v.to_bytes(4, sys.byteorder) for v in values))

        lat_at, lng_at = at(present["lat"]), at(present["lng"])
        found += sum(
            1
            for i in lat_at
            if bisect.bisect_right(lng_at, i + PAIR_BYTES)
            > bisect.bisect_left(lng_at, i - PAIR_BYTES)
        )
    return found


def file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".bin", ".tlog", ".param", ".params"):
        return suffix[1:]
    raise ScrubError(f"{path.name}: unsupported file type")


def plan_file(path: Path) -> Plan:
    kind = file_kind(path)
    if kind == "bin":
        return plan_dataflash(path.read_bytes())
    if kind == "tlog":
        return plan_tlog(path.read_bytes())
    return plan_params(path.read_text(), qgc=kind == "params")


def scrub_file(path: Path, plan: Plan, shift: Shift, dest: Path) -> list[str]:
    kind = file_kind(path)
    if kind == "bin":
        dest.write_bytes(apply_dataflash(path.read_bytes(), plan, shift))
        return verify_dataflash(path, dest, shift)
    if kind == "tlog":
        dest.write_bytes(apply_tlog(path.read_bytes(), plan, shift))
        return verify_tlog(path, dest, shift)
    qgc = kind == "params"
    with dest.open("w", newline="") as f, path.open(newline="") as src:
        f.write(apply_params(src.read(), qgc, plan, shift))
    return verify_params(path, dest, qgc, shift)


def scrub(paths: Sequence[Path], out_dir: Path, shift: Shift | None = None) -> list[Path]:
    """Scrub a session's files with one shared shift. Raises ScrubError on any failure."""
    plans = {p: plan_file(p) for p in paths}
    if shift is None:
        shift = choose_shift([d for plan in plans.values() for d in plan.degrees("lat")])
    lats = [d for plan in plans.values() for d in dict.fromkeys(plan.degrees("lat"))]
    lngs = [d for plan in plans.values() for d in dict.fromkeys(plan.degrees("lng"))]
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for p, plan in plans.items():
            staged = Path(tmp) / p.name
            problems += scrub_file(p, plan, shift, staged)
            leaks = find_position_leaks(staged.read_bytes(), lats, lngs)
            if leaks:
                problems.append(f"{p.name}: {leaks} original position encoding(s) still present")
            outputs.append(staged)
        if problems:
            raise ScrubError("\n".join(problems))
        final = []
        for staged in outputs:
            dest = out_dir / staged.name
            dest.write_bytes(staged.read_bytes())
            final.append(dest)
    for p, plan in plans.items():
        changed = Counter(r.what.split(".")[0] for r in plan.positions)
        print(
            f"{p.name}: {len(plan.positions)} position value(s) shifted "
            f"({', '.join(f'{k} {v}' for k, v in sorted(changed.items())) or 'none'}), "
            f"{len(plan.edits)} identifier(s) zeroed, {plan.skipped_bytes} unparsed byte(s)"
        )
    return final


def update_manifest(manifest: Path, files: Sequence[Path]) -> None:
    """Record each file's SHA-256, relative to the manifest's folder (sha256sum -c format)."""
    entries: dict[str, str] = {}
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            if line.strip():
                digest, rel = line.split(None, 1)
                entries[rel.strip()] = digest
    base = manifest.resolve().parent
    for f in files:
        rel = f.resolve().relative_to(base).as_posix()
        entries[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    manifest.write_text("".join(f"{d}  {r}\n" for r, d in sorted(entries.items())))


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("files", nargs="+", type=Path, help="files of one session")
    parser.add_argument("--out", required=True, type=Path, help="output folder")
    parser.add_argument("--manifest", type=Path, help="SHA-256 manifest to update")
    args = parser.parse_args(argv)
    try:
        outputs = scrub(args.files, args.out)
    except ScrubError as e:
        print(f"scrub failed:\n{e}", file=sys.stderr)
        return 1
    if args.manifest:
        update_manifest(args.manifest, outputs)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
