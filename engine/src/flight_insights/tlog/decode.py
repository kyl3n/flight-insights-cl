"""Decode ground-station tlogs (MAVLink with timestamps) into Arrow tables.

A tlog is what Mission Planner or QGroundControl recorded on the link: every
record is an 8-byte big-endian microsecond time and one MAVLink packet. Both
stations write the same format; pymavlink, the reference MAVLink implementation,
decodes it (spec: Mission Planner and QGroundControl).
"""

# pymavlink ships without type information.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import contextlib
import io
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
from pymavlink import mavutil
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from flight_insights.tlog.positions import position_columns

ARROW_TYPES: dict[str, pa.DataType] = {
    "int8_t": pa.int8(),
    "uint8_t": pa.uint8(),
    "uint8_t_mavlink_version": pa.uint8(),
    "int16_t": pa.int16(),
    "uint16_t": pa.uint16(),
    "int32_t": pa.int32(),
    "uint32_t": pa.uint32(),
    "int64_t": pa.int64(),
    "uint64_t": pa.uint64(),
    "float": pa.float32(),
    "double": pa.float64(),
}

# Columns every table starts with: when it was recorded and who sent it. Named so
# that no MAVLink field (MISSION_CURRENT has its own "seq") can collide with them.
RECORD_COLUMNS = ["tlog_time_us", "src_sysid", "src_compid", "src_seq"]


@dataclass(frozen=True)
class Source:
    sysid: int
    compid: int
    role: str  # "vehicle", "ground station" or "other", from its HEARTBEAT
    mav_type: int | None
    messages: int
    first_us: int
    last_us: int


class TlogError(ValueError):
    """The file cannot be read as a tlog at all."""


@dataclass
class TlogLog:
    """A decoded tlog: one table per message type, the senders, and data quality."""

    tables: dict[str, pa.Table]
    sources: list[Source]
    bad_records: int  # stretches pymavlink could not parse
    rates: dict[tuple[int, int, str], float] = field(
        default_factory=dict[tuple[int, int, str], float]
    )
    bad_times: int = 0  # records whose timestamp cannot be a time (stored as null)
    read_error: str | None = None  # pymavlink failed part-way: what was read before is kept

    def station_sent(self) -> bool:
        """Whether the tlog holds packets the ground station sent, not only the vehicle's."""
        return any(s.role == "ground station" and s.messages for s in self.sources)


def _arrow_type(cls: type, name: str) -> pa.DataType:
    ctype = cls.fieldtypes[cls.fieldnames.index(name)]
    length = dict(zip(cls.ordered_fieldnames, cls.array_lengths, strict=True)).get(name, 0)
    if ctype == "char":
        return pa.string()
    base = ARROW_TYPES[ctype]
    return pa.list_(base, length) if length else base


def _schema(cls: type) -> pa.Schema:
    units: dict[str, str] = getattr(cls, "fieldunits_by_name", {})
    positions = position_columns(cls)
    fields: list[pa.Field[Any]] = [
        pa.field("tlog_time_us", pa.int64(), metadata={"unit": "us"}),
        pa.field("src_sysid", pa.uint8()),
        pa.field("src_compid", pa.uint8()),
        pa.field("src_seq", pa.uint8()),
    ]
    for name in cls.fieldnames:
        meta = {"unit": units.get(name, "")}
        if name in positions:
            meta["position"] = positions[name]
        fields.append(pa.field(name, _arrow_type(cls, name), metadata=meta))
    return pa.schema(fields, metadata={"name": cls.msgname, "msgid": str(cls.id)})


def _role(msg: Any) -> str:
    if msg.type == mavlink.MAV_TYPE_GCS:
        return "ground station"
    if msg.autopilot != mavlink.MAV_AUTOPILOT_INVALID:
        return "vehicle"
    return "other"


def read_tlog(path: Path | str) -> TlogLog:
    columns: dict[str, dict[str, list[Any]]] = {}
    classes: dict[str, type] = {}
    counts: Counter[tuple[int, int, str]] = Counter()
    spans: dict[tuple[int, int], list[int]] = {}
    roles: dict[tuple[int, int], tuple[str, int]] = {}
    per_source: Counter[tuple[int, int]] = Counter()
    bad = bad_times = 0
    read_error: str | None = None
    # pymavlink prints notices about unparseable data to stdout.
    with contextlib.redirect_stdout(io.StringIO()):
        mavutil.set_dialect("ardupilotmega")
        try:
            # The sequential reader: mavlink_connection would pick an indexed one for
            # .tlog files, whose indexing pass crashes on some damaged files.
            conn = mavutil.mavlogfile(str(path), robust_parsing=True)
        except Exception as e:
            raise TlogError(f"not readable as a tlog: {type(e).__name__}") from e
        try:
            while True:
                try:
                    msg = conn.recv_msg()
                except Exception as e:  # pymavlink failed on damaged data: keep what was read
                    read_error = type(e).__name__
                    break
                if msg is None:
                    break
                name = msg.get_type()
                if name == "BAD_DATA":
                    bad += 1
                    continue
                t: int | None = round(msg._timestamp * 1e6)
                if t is None or not 0 <= t < 2**63:
                    t, bad_times = None, bad_times + 1
                sysid, compid = msg.get_srcSystem(), msg.get_srcComponent()
                src = (sysid, compid)
                per_source[src] += 1
                counts[(sysid, compid, name)] += 1
                if t is not None:
                    first_last = spans.setdefault(src, [t, t])
                    first_last[1] = max(first_last[1], t)
                if name == "HEARTBEAT":
                    roles[src] = (_role(msg), int(msg.type))
                if name not in columns:
                    classes[name] = type(msg)
                    columns[name] = defaultdict(list)
                col = columns[name]
                col["tlog_time_us"].append(t)
                col["src_sysid"].append(sysid)
                col["src_compid"].append(compid)
                col["src_seq"].append(msg.get_seq())
                for f in type(msg).fieldnames:
                    value = getattr(msg, f)
                    col[f].append(list(value) if isinstance(value, bytes | bytearray) else value)
        finally:
            conn.close()
    tables = {
        name: pa.Table.from_pydict(
            {k: col[k] for k in RECORD_COLUMNS + list(classes[name].fieldnames)},
            schema=_schema(classes[name]),
        )
        for name, col in columns.items()
    }
    sources = [
        Source(
            sysid=s[0],
            compid=s[1],
            role=roles.get(s, ("other", None))[0],
            mav_type=roles.get(s, ("other", None))[1],
            messages=n,
            first_us=spans.get(s, [0, 0])[0],
            last_us=spans.get(s, [0, 0])[1],
        )
        for s, n in sorted(per_source.items())
    ]
    rates: dict[tuple[int, int, str], float] = {}
    for (sysid, compid, name), n in counts.items():
        first, last = spans.get((sysid, compid), (0, 0))
        if n > 1 and last > first:
            rates[(sysid, compid, name)] = (n - 1) / ((last - first) / 1e6)
    return TlogLog(
        tables=tables,
        sources=sources,
        bad_records=bad,
        rates=rates,
        bad_times=bad_times,
        read_error=read_error,
    )
