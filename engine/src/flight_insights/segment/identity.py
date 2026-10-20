"""Natural identity: which flight this is, whatever file it came in (spec: Shared
storage, "No duplicates").

A flight is identified by its airframe, its boot and log start time and its
firmware, not by its file. Two files holding the same flight, such as a complete
log and a truncated copy, give the same key, so the complete one is kept and the
other linked to it.

The boot is known by the autopilot's boot count (STAT_BOOTCNT) and by when it
happened in UTC: the RTC message where the log has one, or else the first 3D GPS
fix, minus the time since boot.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

import numpy as np

from flight_insights.dataflash.formats import text
from flight_insights.dataflash.log import DataflashLog
from flight_insights.segment.flights import Flight
from flight_insights.segment.validate import Boot, boot_index

GPS_EPOCH_UNIX_S = 315_964_800  # 1980-01-06T00:00:00Z
GPS_UTC_LEAP_S = 18  # GPS time minus UTC since 2017-01-01; unchanged since
WEEK_S = 7 * 24 * 3600
FIX_3D = 3
KEY_VERSION = "fl1"


@dataclass(frozen=True)
class BootIdentity:
    boot: int
    firmware: str  # "4.7.2"
    git_hash: str  # the build's commit, from VER.GH
    boot_count: int | None  # STAT_BOOTCNT
    boot_utc_us: int | None  # UTC microseconds at the boot's TimeUS 0
    utc_source: Literal["RTC", "GPS"] | None


def _first_per_boot(df: DataflashLog, boots: list[Boot], name: str, column: str) -> dict[int, int]:
    """The first value of a column in each boot."""
    out: dict[int, int] = {}
    for fid in df.formats_named(name):
        if not df.count_format(fid) or column not in df.index.formats[fid].field_names:
            continue
        boots_of = boot_index(boots, df.offsets(fid))
        values = df.field(fid, column)
        for b in np.unique(boots_of):
            out.setdefault(int(b), int(values[np.flatnonzero(boots_of == b)[0]]))
    return out


def _boot_counts(df: DataflashLog, boots: list[Boot]) -> dict[int, int]:
    out: dict[int, int] = {}
    for fid in df.formats_named("PARM"):
        if not df.count_format(fid):
            continue
        rows = df.rows(fid)
        boots_of = boot_index(boots, df.offsets(fid))
        for k in range(len(rows)):
            if int(boots_of[k]) not in out and text(rows["Name"][k].tobytes()) == "STAT_BOOTCNT":
                out[int(boots_of[k])] = round(float(rows["Value"][k]))
    return out


def _utc(df: DataflashLog, boots: list[Boot]) -> dict[int, tuple[int, Literal["RTC", "GPS"]]]:
    out: dict[int, tuple[int, Literal["RTC", "GPS"]]] = {}
    for fid in df.formats_named("RTC"):
        if not df.count_format(fid) or "Epoch" not in df.index.formats[fid].field_names:
            continue
        boots_of = boot_index(boots, df.offsets(fid))
        times = df.field(fid, "TimeUS").astype(np.int64)
        epoch = df.field(fid, "Epoch").astype(np.int64)
        for k in np.flatnonzero(epoch > 0):
            out.setdefault(int(boots_of[k]), (int(epoch[k] - times[k]), "RTC"))
    for fid in df.formats_named("GPS"):
        fields = df.index.formats[fid].field_names
        if not df.count_format(fid) or not {"GWk", "GMS", "Status"} <= set(fields):
            continue
        boots_of = boot_index(boots, df.offsets(fid))
        times = df.field(fid, "TimeUS").astype(np.int64)
        week = df.field(fid, "GWk").astype(np.int64)
        ms = df.field(fid, "GMS").astype(np.int64)
        status = df.field(fid, "Status").astype(np.int64)
        for k in np.flatnonzero((status >= FIX_3D) & (week > 0)):
            gps_us = (GPS_EPOCH_UNIX_S + int(week[k]) * WEEK_S - GPS_UTC_LEAP_S) * 1_000_000 + int(
                ms[k]
            ) * 1000
            out.setdefault(int(boots_of[k]), (gps_us - int(times[k]), "GPS"))
    return out


def boot_identities(df: DataflashLog, boots: list[Boot]) -> list[BootIdentity]:
    """One identity per boot in the file. A boot without VER has no firmware to name."""
    counts = _boot_counts(df, boots)
    utc = _utc(df, boots)
    major = _first_per_boot(df, boots, "VER", "Maj")
    minor = _first_per_boot(df, boots, "VER", "Min")
    patch = _first_per_boot(df, boots, "VER", "Pat")
    git = _first_per_boot(df, boots, "VER", "GH")
    out: list[BootIdentity] = []
    for b in boots:
        n = b.number
        firmware = f"{major[n]}.{minor[n]}.{patch[n]}" if n in major else ""
        when = utc.get(n)
        out.append(
            BootIdentity(
                n,
                firmware,
                f"{git[n]:08x}" if n in git else "",
                counts.get(n),
                when[0] if when else None,
                when[1] if when else None,
            )
        )
    return out


def flight_key(boot: BootIdentity, flight: Flight, board_id_hash: str | None) -> str:
    """The same flight gives the same key from any file that holds its start."""
    parts = [
        board_id_hash or "",
        boot.firmware,
        boot.git_hash,
        "" if boot.boot_count is None else str(boot.boot_count),
        "" if boot.boot_utc_us is None else str(boot.boot_utc_us),
        str(flight.arm_us),
    ]
    return f"{KEY_VERSION}-" + hashlib.sha256("|".join(parts).encode()).hexdigest()
