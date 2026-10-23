"""Is the log whole? (spec: Data ingestion, pipeline step 4 "Validate").

Flags what makes later analysis wrong without making it fail:

- logging dropouts: messages the logger had to drop (DSF.Dp counts them);
- timestamp gaps: stretches with no messages at all while the vehicle was armed;
- raw-IMU sample loss: holes in the GYR/ACC sample clock (SampleUS). Filter
  analysis on a log with dropped IMU samples gives false spectra, so this gates it.
  The clock jitters (samples arrive in FIFO batches or as the scheduler runs), so a
  hole is measured against its longest usual step, and stretches where raw logging
  was switched off in flight are not loss;
- IMU errors: the sensor drivers' own error counters (IMU.EG/EA) going up;
- clock resets: time jumping back, which means another boot in the same file.

Every issue names the message it was found in, as its evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

import numpy as np

from flight_insights.dataflash.log import DataflashLog

IssueKind = Literal[
    "logging dropout", "timestamp gap", "logging paused", "imu sample loss", "imu errors",
    "clock reset",
]  # fmt: skip
Ints = np.ndarray[tuple[int], np.dtype[np.int64]]

RESET_US = 1_000_000  # time going back by more than this is a new boot
GAP_US = 500_000  # no message at all for this long is a gap
SAMPLE_GAP = 1.5  # a raw-IMU interval this many times the longest usual one has lost samples
RAW_IMU_BIT = 1 << 19  # LOG_BITMASK "Raw IMU": GYR and ACC
GYRO_RAW_OPTS = 0b11  # INS_RAW_LOG_OPT "primary gyro only", "all gyros": GYR without the bit
MERGE_US = 1_000_000  # issues of one stream this close together are reported as one


@dataclass(frozen=True)
class Issue:
    kind: IssueKind
    start_us: int
    end_us: int
    count: int  # messages dropped, samples lost, errors counted; 1 for gaps and resets
    message: str  # where it was found
    boot: int = 0
    instance: int | None = None  # sensor instance, for per-sensor issues
    detail: str = ""


@dataclass(frozen=True)
class Boot:
    """One power-on's stretch of the file."""

    number: int
    start_offset: int  # file offsets of its first and last message
    end_offset: int
    first_us: int
    last_us: int

    def holds(self, offset: int) -> bool:
        return self.start_offset <= offset <= self.end_offset


@dataclass
class Validation:
    boots: list[Boot]
    issues: list[Issue] = field(default_factory=list[Issue])

    def of_kind(self, kind: IssueKind) -> list[Issue]:
        return [i for i in self.issues if i.kind == kind]

    @property
    def imu_samples_complete(self) -> bool:
        """False: raw-IMU samples were lost, so spectra from them are not trustworthy."""
        return not self.of_kind("imu sample loss")

    def of_flight(self, boot: int, arm_us: int, disarm_us: int) -> list[Issue]:
        """The issues of one boot that overlap a flight's armed span."""
        return [
            i
            for i in self.issues
            if i.boot == boot and i.start_us <= disarm_us and i.end_us >= arm_us
        ]


@dataclass(frozen=True)
class Timeline:
    """Every timestamped message in file order: (file offset, TimeUS)."""

    offsets: Ints
    times: Ints


def timeline(df: DataflashLog) -> Timeline:
    offsets: list[Ints] = []
    times: list[Ints] = []
    for fid, fmt in enumerate(df.index.formats):
        if not df.count_format(fid) or "TimeUS" not in fmt.field_names:
            continue
        if fmt.dtype.fields is None or fmt.dtype.fields["TimeUS"][0].kind not in "iu":
            continue
        offsets.append(df.offsets(fid))
        times.append(df.field(fid, "TimeUS").astype(np.int64))
    if not offsets:
        return Timeline(np.empty(0, np.int64), np.empty(0, np.int64))
    o, t = np.concatenate(offsets), np.concatenate(times)
    order = np.argsort(o, kind="stable")
    return Timeline(o[order], t[order])


def find_boots(tl: Timeline) -> list[Boot]:
    """Split the file wherever time jumps back by more than RESET_US."""
    boots: list[Boot] = []
    start = 0
    n = len(tl.times)
    while start < n:
        t = tl.times[start:]
        back = np.flatnonzero(t < np.maximum.accumulate(t) - RESET_US)
        end = start + (int(back[0]) if len(back) else len(t))
        seg = tl.times[start:end]
        boots.append(
            Boot(
                len(boots),
                int(tl.offsets[start]),
                int(tl.offsets[end - 1]),
                int(seg.min()),
                int(seg.max()),
            )
        )
        start = end
    return boots


def boot_index(boots: list[Boot], offsets: Ints) -> Ints:
    """The boot each file offset belongs to."""
    if not boots:
        return np.zeros(len(offsets), np.int64)
    starts = np.array([b.start_offset for b in boots], np.int64)
    return np.maximum(np.searchsorted(starts, offsets, side="right") - 1, 0).astype(np.int64)


def validate(df: DataflashLog, armed: list[tuple[int, int, int]] | None = None) -> Validation:
    """Check the log. `armed` lists (boot, arm_us, disarm_us) spans; gaps outside them
    are logging pauses (normal when the logger stops while disarmed), not faults."""
    tl = timeline(df)
    boots = find_boots(tl)
    out = Validation(boots)
    for b in boots[1:]:
        out.issues.append(
            Issue(
                "clock reset",
                b.first_us,
                b.first_us,
                1,
                "all",
                b.number,
                detail=f"time went back: boot {b.number + 1} of the same file",
            )
        )
    gaps = _gaps(tl, boots, armed or [])
    out.issues += gaps
    out.issues += _dropouts(df, boots)
    out.issues += _sample_loss(df, boots, [g for g in gaps if g.kind == "logging paused"])
    out.issues += _imu_errors(df, boots)
    out.issues = _merged(out.issues)
    out.issues.sort(key=lambda i: (i.boot, i.start_us, i.kind))
    return out


def _merged(issues: list[Issue]) -> list[Issue]:
    """Join issues of one stream that follow each other within MERGE_US, and word them."""
    streams: dict[tuple[str, str, int, int | None, str], list[Issue]] = {}
    for i in issues:
        streams.setdefault((i.kind, i.message, i.boot, i.instance, i.detail), []).append(i)
    out: list[Issue] = []
    for group in streams.values():
        group.sort(key=lambda i: i.start_us)
        run = group[0]
        for i in group[1:]:
            if run.kind in ("logging dropout", "imu sample loss", "imu errors") and (
                i.start_us - run.end_us <= MERGE_US
            ):
                run = replace(run, end_us=max(run.end_us, i.end_us), count=run.count + i.count)
            else:
                out.append(_worded(run))
                run = i
        out.append(_worded(run))
    return out


def _worded(i: Issue) -> Issue:
    where = f"{i.message}[{i.instance}]" if i.instance is not None else i.message
    span = (i.end_us - i.start_us) / 1e6
    match i.kind:
        case "logging dropout":
            detail = f"{i.count} messages dropped by the logger over {span:.1f} s"
        case "imu sample loss":
            detail = f"{where}: about {i.count} samples missing over {span:.1f} s"
        case "imu errors":
            detail = f"{where} {i.detail} error count rose by {i.count}"
        case _:
            return i
    return replace(i, detail=detail)


def _gaps(tl: Timeline, boots: list[Boot], armed: list[tuple[int, int, int]]) -> list[Issue]:
    issues: list[Issue] = []
    for b in boots:
        sel = (tl.offsets >= b.start_offset) & (tl.offsets <= b.end_offset)
        t = np.maximum.accumulate(tl.times[sel])
        jumps = np.flatnonzero(np.diff(t) > GAP_US)
        for j in jumps:
            start, end = int(t[j]), int(t[j + 1])
            in_flight = any(boot == b.number and a <= start and end <= d for boot, a, d in armed)
            kind: IssueKind = "timestamp gap" if in_flight else "logging paused"
            detail = f"no messages for {(end - start) / 1e6:.3f} s"
            issues.append(Issue(kind, start, end, 1, "all", b.number, detail=detail))
    return issues


def _dropouts(df: DataflashLog, boots: list[Boot]) -> list[Issue]:
    issues: list[Issue] = []
    for fid in df.formats_named("DSF"):
        if not df.count_format(fid) or "Dp" not in df.index.formats[fid].field_names:
            continue
        boot_ids = boot_index(boots, df.offsets(fid))
        times = df.field(fid, "TimeUS").astype(np.int64)
        dropped = df.field(fid, "Dp").astype(np.int64)
        for boot in np.unique(boot_ids):
            t, dp = times[boot_ids == boot], dropped[boot_ids == boot]
            # Dp counts from the start of the log file; each rise is a dropout.
            rise = np.diff(np.concatenate([[0], np.maximum.accumulate(dp)]))
            for k in np.flatnonzero(rise > 0):
                start = int(t[k - 1]) if k else int(t[k])
                issues.append(
                    Issue("logging dropout", start, int(t[k]), int(rise[k]), "DSF", int(boot))
                )
    return issues


def _raw_off(df: DataflashLog, boots: list[Boot], name: str) -> dict[int, list[tuple[int, int]]]:
    """Per boot, the stretches when raw `name` (GYR or ACC) was not being logged because
    LOG_BITMASK or INS_RAW_LOG_OPT said so, as when raw logging is switched on for the
    steady part of a filter test flight only. Read from the parameters the log records,
    changes included."""
    events: dict[int, list[tuple[int, str, float]]] = {}
    for fid in df.formats_named("PARM"):
        if not df.count_format(fid):
            continue
        rows = df.rows(fid)
        boot_ids = boot_index(boots, df.offsets(fid))
        for param in ("LOG_BITMASK", "INS_RAW_LOG_OPT"):
            key = param.encode().ljust(rows["Name"].itemsize, b"\0")
            for k in np.flatnonzero(rows["Name"] == key):
                events.setdefault(int(boot_ids[k]), []).append(
                    (int(rows["TimeUS"][k]), param, float(rows["Value"][k]))
                )
    last_us = {b.number: b.last_us for b in boots}
    out: dict[int, list[tuple[int, int]]] = {}
    for boot, changes in events.items():
        values: dict[str, int] = {}
        off_since: int | None = None
        for t, param, value in sorted(changes):
            values[param] = int(value)
            on = bool(values.get("LOG_BITMASK", RAW_IMU_BIT) & RAW_IMU_BIT) or (
                name == "GYR" and bool(values.get("INS_RAW_LOG_OPT", 0) & GYRO_RAW_OPTS)
            )
            if not on and off_since is None:
                off_since = t
            elif on and off_since is not None:
                out.setdefault(boot, []).append((off_since, t))
                off_since = None
        if off_since is not None:
            out.setdefault(boot, []).append((off_since, last_us[boot]))
    return out


def _spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sorted, with overlapping spans joined."""
    out: list[tuple[int, int]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _sample_loss(df: DataflashLog, boots: list[Boot], paused: list[Issue]) -> list[Issue]:
    """Holes in each raw-IMU sample clock. A hole across a logging pause is not sample
    loss: nothing at all was logged then (the logger stops while disarmed), and the
    pause is reported on its own. Nor is a hole while raw logging was switched off."""
    issues: list[Issue] = []
    for name in ("GYR", "ACC"):
        raw_off = _raw_off(df, boots, name)
        for fid in df.formats_named(name):
            if not df.count_format(fid):
                continue
            if not {"SampleUS", "I"} <= set(df.index.formats[fid].field_names):
                continue
            sample = df.field(fid, "SampleUS").astype(np.int64)
            inst = df.field(fid, "I").astype(np.int64)
            boot_ids = boot_index(boots, df.offsets(fid))
            for boot in np.unique(boot_ids):
                pauses = _spans(
                    [(p.start_us, p.end_us) for p in paused if p.boot == boot]
                    + raw_off.get(int(boot), [])
                )
                for i in np.unique(inst):
                    sel = (inst == i) & (boot_ids == boot)
                    issues += _holes(sample[sel], name, int(i), int(boot), pauses)
    return issues


def _holes(
    sample_us: Ints, name: str, instance: int, boot: int, pauses: list[tuple[int, int]]
) -> list[Issue]:
    """Runs of lost samples in one sensor's sample clock, holes within MERGE_US of each
    other counted as one run, holes overlapping a logging pause (sorted (start, end)
    spans) left out. Vectorised: a starved logger can leave millions of holes."""
    if len(sample_us) < 3:
        return []
    d = np.diff(sample_us)
    # The longest usual step, not the median: a clock that alternates short and long
    # steps (FIFO batches, scheduling) has lost nothing. Lost samples are counted in
    # the mean usual step, the clock's real period.
    usual = float(np.percentile(d, 90))
    if usual <= 0:
        return []
    regular = d[(d > 0) & (d <= SAMPLE_GAP * usual)]
    period = float(regular.mean()) if len(regular) else usual
    j = np.flatnonzero(d > SAMPLE_GAP * usual)
    if pauses and len(j):
        # The last pause starting before a hole ends is the one that can overlap it.
        p_start = np.array([a for a, _ in pauses], np.int64)
        p_end = np.array([b for _, b in pauses], np.int64)
        k = np.searchsorted(p_start, sample_us[j + 1], side="left") - 1
        j = j[(k < 0) | (p_end[np.maximum(k, 0)] <= sample_us[j])]
    if not len(j):
        return []
    lost = np.maximum(np.rint(d[j] / period).astype(np.int64) - 1, 1)
    starts, ends = sample_us[j], sample_us[j + 1]
    first = np.flatnonzero(np.concatenate([[True], starts[1:] - ends[:-1] > MERGE_US]))
    last = np.concatenate([first[1:] - 1, [len(j) - 1]])
    counts = np.add.reduceat(lost, first)
    return [
        Issue("imu sample loss", int(starts[a]), int(ends[b]), int(n), name, boot, instance)
        for a, b, n in zip(first, last, counts, strict=True)
    ]


def _imu_errors(df: DataflashLog, boots: list[Boot]) -> list[Issue]:
    issues: list[Issue] = []
    for fid in df.formats_named("IMU"):
        if not df.count_format(fid):
            continue
        if not {"I", "EG", "EA"} <= set(df.index.formats[fid].field_names):
            continue
        times = df.field(fid, "TimeUS").astype(np.int64)
        inst = df.field(fid, "I").astype(np.int64)
        boot_ids = boot_index(boots, df.offsets(fid))
        for col, sensor in (("EG", "gyro"), ("EA", "accelerometer")):
            count = df.field(fid, col).astype(np.int64)
            for boot in np.unique(boot_ids):
                for i in np.unique(inst):
                    sel = np.flatnonzero((inst == i) & (boot_ids == boot))
                    rise = np.diff(count[sel])
                    for k in np.flatnonzero(rise > 0):
                        issues.append(
                            Issue(
                                "imu errors",
                                int(times[sel[k]]),
                                int(times[sel[k + 1]]),
                                int(rise[k]),
                                "IMU",
                                int(boot),
                                int(i),
                                sensor,
                            )
                        )
    return issues
