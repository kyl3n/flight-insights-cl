"""Flights and flight modes in a log (spec: Data ingestion, pipeline step 5 "Segment").

A flight is one armed span. Without LOG_FILE_DSRMROT one log covers a whole
power cycle, so it is split at every arm and disarm. Arming comes from ARM
(ArmState) and the armed and disarmed events (EV); a log that starts on arming,
the default without LOG_DISARMED, holds no arming record, so the vehicle counts
as armed from the start of the boot. Mode names come from the release's own
enums in the knowledge pack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from flight_insights.dataflash.log import DataflashLog
from flight_insights.knowledge import Pack, Vehicle
from flight_insights.segment.validate import Boot, boot_index

ARMED_EVENT, DISARMED_EVENT = 10, 11


@dataclass(frozen=True)
class ArmChange:
    boot: int
    time_us: int
    armed: bool
    source: str  # "ARM" or "EV"


@dataclass(frozen=True)
class ModeSpan:
    boot: int
    start_us: int
    end_us: int
    number: int
    name: str  # "MODE_<n>" when the release has no name for it
    reason: int | None  # MODE.Rsn: why the mode changed


@dataclass(frozen=True)
class Flight:
    boot: int
    number: int  # 1-based, in the order flown
    arm_us: int
    disarm_us: int
    start: Literal["armed", "log started armed"]
    end: Literal["disarmed", "log ended armed"]
    modes: tuple[ModeSpan, ...] = field(default=())

    @property
    def duration_s(self) -> float:
        return (self.disarm_us - self.arm_us) / 1e6


def arm_changes(df: DataflashLog, boots: list[Boot]) -> list[ArmChange]:
    """Every arm and disarm record. ARM and EV both report a change; one is kept."""
    found: list[ArmChange] = []
    for name, column in (("ARM", "ArmState"), ("EV", "Id")):
        for fid in df.formats_named(name):
            if not df.count_format(fid) or column not in df.index.formats[fid].field_names:
                continue
            boots_of = boot_index(boots, df.offsets(fid))
            times = df.field(fid, "TimeUS").astype(np.int64)
            values = df.field(fid, column).astype(np.int64)
            for b, t, v in zip(boots_of, times, values, strict=True):
                if name == "ARM":
                    found.append(ArmChange(int(b), int(t), bool(v), name))
                elif v in (ARMED_EVENT, DISARMED_EVENT):
                    found.append(ArmChange(int(b), int(t), v == ARMED_EVENT, name))
    found.sort(key=lambda c: (c.boot, c.time_us, c.source))
    out: list[ArmChange] = []
    for c in found:
        if out and out[-1].boot == c.boot and out[-1].armed == c.armed:
            continue  # the same change from the other message, or a repeat
        out.append(c)
    return out


def armed_spans(
    changes: list[ArmChange], boots: list[Boot], logs_only_armed: bool = False
) -> list[Flight]:
    """Armed spans per boot. With `logs_only_armed` (LOG_DISARMED 0) every logged
    moment was armed, so a boot with no arming records at all, such as a truncated
    copy that lost the final disarm, is armed throughout."""
    flights: list[Flight] = []
    for boot in boots:
        mine = [c for c in changes if c.boot == boot.number]
        start: int | None = None
        started: Literal["armed", "log started armed"] = "armed"
        if (mine and not mine[0].armed) or (not mine and logs_only_armed):
            start, started = boot.first_us, "log started armed"
        for c in mine:
            if c.armed and start is None:
                start, started = c.time_us, "armed"
            elif not c.armed and start is not None:
                flights.append(Flight(boot.number, 0, start, c.time_us, started, "disarmed"))
                start = None
        if start is not None:
            flights.append(Flight(boot.number, 0, start, boot.last_us, started, "log ended armed"))
    return [
        Flight(f.boot, i + 1, f.arm_us, f.disarm_us, f.start, f.end) for i, f in enumerate(flights)
    ]


def mode_spans(
    df: DataflashLog, boots: list[Boot], pack: Pack, vehicle: Vehicle, firmware: str
) -> list[ModeSpan]:
    names: dict[str, str] = pack.enums(firmware)["modes"].get(vehicle, {})
    spans: list[ModeSpan] = []
    for fid in df.formats_named("MODE"):
        fields = df.index.formats[fid].field_names
        if not df.count_format(fid) or "ModeNum" not in fields:
            continue
        boots_of = boot_index(boots, df.offsets(fid))
        times = df.field(fid, "TimeUS").astype(np.int64)
        numbers = df.field(fid, "ModeNum").astype(np.int64)
        reasons = df.field(fid, "Rsn").astype(np.int64) if "Rsn" in fields else None
        for k in range(len(times)):
            n = int(numbers[k])
            spans.append(
                ModeSpan(
                    int(boots_of[k]),
                    int(times[k]),
                    0,
                    n,
                    names.get(str(n), f"MODE_{n}"),
                    None if reasons is None else int(reasons[k]),
                )
            )
    spans.sort(key=lambda m: (m.boot, m.start_us))
    # The same mode entered again (a repeated request) continues the span.
    spans = [
        m
        for k, m in enumerate(spans)
        if not k or (spans[k - 1].boot, spans[k - 1].number) != (m.boot, m.number)
    ]
    out: list[ModeSpan] = []
    for k, m in enumerate(spans):
        nxt = spans[k + 1] if k + 1 < len(spans) and spans[k + 1].boot == m.boot else None
        end = nxt.start_us if nxt else boots[m.boot].last_us
        out.append(ModeSpan(m.boot, m.start_us, end, m.number, m.name, m.reason))
    return out


def flights(
    df: DataflashLog, boots: list[Boot], pack: Pack, vehicle: Vehicle, firmware: str
) -> list[Flight]:
    """The log's flights, each with the modes flown during it."""
    modes = mode_spans(df, boots, pack, vehicle, firmware)
    out: list[Flight] = []
    only_armed = _first_param(df, "LOG_DISARMED") == 0
    for f in armed_spans(arm_changes(df, boots), boots, only_armed):
        flown = tuple(
            m
            for m in modes
            if m.boot == f.boot and m.start_us < f.disarm_us and m.end_us > f.arm_us
        )
        out.append(Flight(f.boot, f.number, f.arm_us, f.disarm_us, f.start, f.end, flown))
    return out


def _first_param(df: DataflashLog, name: str) -> float | None:
    for fid in df.formats_named("PARM"):
        if not df.count_format(fid):
            continue
        rows = df.rows(fid)
        hits = np.flatnonzero(rows["Name"] == name.encode().ljust(rows["Name"].itemsize, b"\0"))
        if len(hits):
            return float(rows["Value"][hits[0]])
    return None
