"""The ground station's clock against the vehicle's (spec: Link and ground-station
analysis, "Pairing and alignment").

A tlog stamps each packet with the ground station's clock when it was recorded. Every
vehicle message that carries `time_boot_ms` also says when the vehicle sent it, on the
same boot clock as the onboard log's TimeUS. For each pair,

    recorded = offset + (1 + drift) * sent + delay,

where the delay (the radio link, the station's serial port and software) is never
negative. So the least delay sits on a line: the lower envelope of recorded - sent over
the boot clock. The fit takes the least value of each 10 s window and fits a line to
those by least squares; a window whose least value lies far above the line was delayed
whole (the station stalled, or the link held packets back) and is dropped. The offset
found includes the link's least delay, which one-way timestamps cannot separate; it is
what places a station's records on the vehicle's time line.

A tlog may hold several boots of the vehicle: the boot clock starts again from zero, so
the station's clock at the boot instant (recorded - sent) jumps. Each boot is fitted on
its own.

The pairing is then checked against the onboard log: the attitude the station recorded
(ATTITUDE, on its own time_boot_ms) is cross-correlated with the onboard attitude
(ATT, on TimeUS). Both are the same boot clock, so the peak should sit at zero lag; a
clear peak elsewhere is applied as a correction, and no clear peak leaves the pairing
unconfirmed.

Packets lost on the way down are counted where the tlog is read (decode.py), from each
sender's MAVLink sequence numbers.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.tlog.decode import SeqCounter

Array = np.ndarray[Any, Any]
WINDOW_S = 10.0  # the least delay is taken over each window of the boot clock
NEW_BOOT_US = 5_000_000  # recorded - sent jumping by more than this: the vehicle rebooted
SAME_BOOT_US = 2_000_000  # boot times from SYSTEM_TIME agree to well within this
STALL_SPREAD = 4.0  # a window this many robust spreads above the line was delayed whole
STALL_FLOOR_US = 20_000.0  # ...and at least this far
LAG_SPAN_MS = 1000  # attitude lags tried, either way
LAG_STEP_MS = 5
CONFIRM_R = 0.9  # the correlation that confirms a pairing
LAG_TOLERANCE_MS = 50.0  # a peak within this of zero is the same clock


@dataclass(frozen=True)
class BootRecords:
    """One boot of one vehicle, as a tlog recorded it: when each timed packet was sent
    (boot clock) and recorded (station clock), in µs."""

    sysid: int
    compid: int
    sent_us: Array
    recorded_us: Array
    records: tuple[int, int]  # the first and last packet's number in the file
    system_time: list[tuple[int, int]]  # (time_unix_usec, time_boot_ms) pairs, GPS UTC


@dataclass(frozen=True)
class Clock:
    """recorded = offset_us + (1 + drift_ppm / 1e6) * sent, for the least delay."""

    offset_us: float
    drift_ppm: float
    windows: int  # windows fitted
    stalls: int  # windows dropped as delayed whole
    spread_ms: float  # RMS of the fitted windows' least delays about the line
    delay_ms: tuple[float, float]  # median and 95th percentile delay above the line

    def to_station(self, boot_us: Array | float) -> Array:
        return self.offset_us + (1.0 + self.drift_ppm / 1e6) * np.asarray(boot_us, np.float64)

    def to_boot(self, station_us: Array | float) -> Array:
        return (np.asarray(station_us, np.float64) - self.offset_us) / (1.0 + self.drift_ppm / 1e6)


def _column(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.float64)


def boots(tables: dict[str, pa.Table], vehicles: Sequence[tuple[int, int]]) -> list[BootRecords]:
    """Each vehicle's boots: every packet it sent with `time_boot_ms`, split where the
    station's clock at the boot instant jumps."""
    out: list[BootRecords] = []
    for sysid, compid in vehicles:
        sent: list[Array] = []
        recorded: list[Array] = []
        numbers: list[Array] = []
        for table in tables.values():
            if "time_boot_ms" not in table.column_names:
                continue
            mine = (_column(table, "src_sysid") == sysid) & (_column(table, "src_compid") == compid)
            t = _column(table, "tlog_time_us")
            keep = mine & np.isfinite(t)
            sent.append(_column(table, "time_boot_ms")[keep] * 1000.0)
            recorded.append(t[keep])
            numbers.append(_column(table, "tlog_record")[keep])
        if not sent or not sum(len(s) for s in sent):
            continue
        s, r, n = np.concatenate(sent), np.concatenate(recorded), np.concatenate(numbers)
        order = np.argsort(n, kind="stable")  # as the station recorded them
        s, r, n = s[order], r[order], n[order]
        cuts = np.flatnonzero(np.abs(np.diff(r - s)) > NEW_BOOT_US) + 1
        times = _system_time(tables, sysid, compid)
        for a, b in zip([0, *cuts], [*cuts, len(s)], strict=True):
            first, last = int(n[a]), int(n[b - 1])
            inside = [(u, ms) for k, u, ms in times if first <= k <= last]
            out.append(BootRecords(sysid, compid, s[a:b], r[a:b], (first, last), inside))
    return out


def _system_time(
    tables: dict[str, pa.Table], sysid: int, compid: int
) -> list[tuple[int, int, int]]:
    """(packet number, time_unix_usec, time_boot_ms) of each SYSTEM_TIME with GPS time."""
    table = tables.get("SYSTEM_TIME")
    if table is None:
        return []
    mine = (_column(table, "src_sysid") == sysid) & (_column(table, "src_compid") == compid)
    k, unix, ms = (
        np.asarray(table.column(n).to_numpy(zero_copy_only=False), np.int64)[mine]
        for n in ("tlog_record", "time_unix_usec", "time_boot_ms")
    )
    return [(int(a), int(b), int(c)) for a, b, c in zip(k, unix, ms, strict=True) if b > 0]


def losses(
    tables: dict[str, pa.Table], sysid: int, compid: int, records: tuple[int, int]
) -> tuple[int, int, int]:
    """(received, lost, out of order) for one sender between two packet numbers, from its
    sequence numbers in the order the station recorded them."""
    numbers: list[Array] = []
    seqs: list[Array] = []
    for table in tables.values():
        if "src_seq" not in table.column_names:
            continue
        n = np.asarray(table.column("tlog_record").to_numpy(zero_copy_only=False), np.int64)
        mine = (_column(table, "src_sysid") == sysid) & (_column(table, "src_compid") == compid)
        mine &= (n >= records[0]) & (n <= records[1])
        numbers.append(n[mine])
        seqs.append(
            np.asarray(table.column("src_seq").to_numpy(zero_copy_only=False), np.int64)[mine]
        )
    if not numbers:
        return 0, 0, 0
    n, q = np.concatenate(numbers), np.concatenate(seqs)
    counter = SeqCounter()
    for seq in q[np.argsort(n, kind="stable")].tolist():
        counter.add(int(seq))
    return len(q), counter.lost, counter.out_of_order


def boot_utc_us(b: BootRecords) -> int | None:
    """The vehicle's own UTC at its boot (GPS time, from SYSTEM_TIME), or None."""
    starts = [unix - ms * 1000 for unix, ms in b.system_time]
    return int(np.median(starts)) if starts else None


def fit(b: BootRecords, window_s: float = WINDOW_S) -> Clock | None:
    """The station's clock against the vehicle's for one boot, or None with too few
    packets to say."""
    if len(b.sent_us) < 2:
        return None
    gap = b.recorded_us - b.sent_us
    edges = np.floor((b.sent_us - b.sent_us.min()) / (window_s * 1e6)).astype(np.int64)
    order = np.lexsort((gap, edges))
    first = np.concatenate([[True], np.diff(edges[order]) > 0])
    x, y = b.sent_us[order][first], gap[order][first]  # each window's least delay
    keep = np.ones(len(x), dtype=bool)
    slope, level = 0.0, float(y.min())
    for _ in range(5):
        if keep.sum() >= 3:
            slope, level = (float(v) for v in np.polyfit(x[keep], y[keep], 1))
        else:
            slope, level = 0.0, float(y[keep].min())
        resid = y - (level + slope * x)
        mad = float(np.median(np.abs(resid[keep] - np.median(resid[keep])))) * 1.4826
        stalled = resid > max(STALL_SPREAD * mad, STALL_FLOOR_US)
        if np.array_equal(~stalled, keep):
            break
        keep = ~stalled
    line = level + slope * x
    spread = float(np.sqrt(np.mean((y[keep] - line[keep]) ** 2))) / 1000.0
    above = (gap - (level + slope * b.sent_us)) / 1000.0
    return Clock(
        offset_us=level,
        drift_ppm=slope * 1e6,
        windows=int(keep.sum()),
        stalls=int((~keep).sum()),
        spread_ms=spread,
        delay_ms=(float(np.median(above)), float(np.percentile(above, 95))),
    )


@dataclass(frozen=True)
class Confirmation:
    lag_ms: float  # where the onboard attitude best matches the station's
    r: float  # the correlation there


def confirm(
    station: tuple[Array, Array, Array], onboard: tuple[Array, Array, Array]
) -> Confirmation | None:
    """The lag at which the onboard attitude best matches the attitude the station
    recorded, both as (boot µs, roll deg, pitch deg); None where they do not overlap or
    the aircraft barely moved."""
    ts, rs, ps = station
    to, ro, po = onboard
    if len(ts) < 10 or len(to) < 10:
        return None
    pad = LAG_SPAN_MS * 1000.0
    inside = (ts >= to.min() + pad) & (ts <= to.max() - pad)
    if inside.sum() < 10:
        return None
    t, a = ts[inside], np.concatenate([rs[inside], ps[inside]])
    if float(np.std(a)) < 0.2:
        return None  # level the whole time: nothing to match
    best: tuple[float, float] | None = None
    for lag in range(-LAG_SPAN_MS, LAG_SPAN_MS + 1, LAG_STEP_MS):
        at = t + lag * 1000.0
        b = np.concatenate([np.interp(at, to, ro), np.interp(at, to, po)])
        if float(np.std(b)) == 0.0:
            continue
        r = float(np.corrcoef(a, b)[0, 1])
        if math.isfinite(r) and (best is None or r > best[1]):
            best = (float(lag), r)
    return None if best is None else Confirmation(*best)
