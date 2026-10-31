"""GNSS disturbances in a flight, and what each one was (spec: GNSS jamming and spoofing).

Every channel below trips on its own; trips within `merge_s` of each other are one
event, and each event is classified from which channels tripped and in what order
(rules in `knowledge/rules/gnss.json`).

Signal, per receiver:
- the fix below 3D; satellites under `min_sats`, or `sats_drop` below the flight's
  typical count; horizontal accuracy (GPA) past both `hacc_m` and `hacc_factor` times
  its usual value;
- the u-blox status (UBX1): the continuous-wave jamming indicator past `cw_jamming`
  (MON-HW, 0 to 255), or the jamming state at warning or worse (MON-RF, which an M10
  sends instead); the AGC count `agc_drop` below its usual level;
- GPS time standing still for `time_stop_s` while the log's clock runs.

Consistency, checks a spoofer cannot fake all at once:
- the primary estimator core rejecting GNSS: a position, velocity or height test ratio
  (XKF4) above 1, or a GPS glitch error (ERR GPS 2). The firmware logs each ratio as 100
  times its value (`logged_ratio_scale`, with its source in the rules);
- a receiver's position more than `track_m` from the estimator's, compared as local
  tracks from one origin;
- a one-sided horizontal velocity innovation (XKF3) averaging over `drift_mps` for
  `drift_window_s`: the estimator being dragged by a drifting position, the dangerous
  slow spoof that the test ratios miss;
- GNSS altitude moving `baro_m` away from the barometer against their usual offset, plus
  what the barometer's own error can reach at the aircraft's speed: its static pressure
  is off by up to `baro_dynamic_ratio` of the dynamic pressure, so in fast flight its
  height drifts by that share of v²/2g (12 to 14 m at 22 to 35 m/s on a heavy-lift quad);
- GPS time jumping against the log's clock by `time_jump_s`;
- two receivers more than `receivers_m` apart.

Classes: spoofing when a consistency check failed while the signal looked healthy (no
signal trip within `merge_s` of its start). Otherwise: onboard when the u-blox status
showed jamming and its indicator follows the motor current over the flight; jamming
when the u-blox status showed it, or the fix or accuracy went for `short_s` or more with
no u-blox status saying the band was quiet; receiver when only one of two receivers
degraded; obstruction for the rest (satellites lost with the accuracy held, brief
losses, or a quiet band). Each event also says how the aircraft coped: whether and how
long the estimator rejected GNSS, whether it kept following a drifting position, the
mode it changed to, and a heading reset driven by GNSS velocity.

A GNSS-off drill (the GPS-disable switch, aux function 65) is not a disturbance: its
trips are left out, and the no-GNSS analyzer measures it.

Positions, absolute altitudes, GPS time and status text are read on this laptop only.
Findings carry times, durations, counts, test ratios and the class; evidence that read a
position, an altitude, GPS time or text says so, and the location policy withholds its
values from the AI path. Only the largest gaps and resets of each event reach it, each in
whole metres (rules/scrubber.json "position_derived").
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import (
    Analyzer,
    Confidence,
    Evidence,
    Finding,
    MetricValue,
    Result,
    Segment,
    Severity,
)
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si
from flight_insights.dataflash.formats import text

Array = np.ndarray[Any, Any]
GRAVITY = 9.80665  # m/s²
SIGNAL = ("fix", "sats", "accuracy", "cw", "jam_state", "agc", "time_stopped")
UBLOX = ("cw", "jam_state", "agc")
LOST = ("fix", "accuracy", "time_stopped")
# independent sources of a consistency failure: two of them make spoofing likely
GROUPS = {"rejected": "estimator", "glitch": "estimator", "track": "estimator",
          "drift": "estimator", "baro": "barometer", "time_jump": "time",
          "receivers": "receivers"}  # fmt: skip
# what each channel read, for its evidence and the location policy
READ: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "fix": (("GPS",), ("Status",)),
    "sats": (("GPS",), ("NSats",)),
    "accuracy": (("GPA",), ("HAcc",)),
    "cw": (("UBX1",), ("jamInd",)),
    "jam_state": (("UBX1",), ("jamInd",)),
    "agc": (("UBX1",), ("agcCnt",)),
    "time_stopped": (("GPS",), ("GMS", "GWk")),
    "rejected": (("XKF4",), ("C", "PI", "SH", "SP", "SV")),
    "glitch": (("ERR",), ("ECode", "Subsys")),
    "track": (("GPS", "POS"), ("Lat", "Lng")),
    "drift": (("XKF3",), ("C", "IVE", "IVN")),
    "baro": (("BARO", "GPS"), ("Alt",)),
    "time_jump": (("GPS",), ("GMS", "GWk")),
    "receivers": (("GPS",), ("Lat", "Lng")),
}
WORDS = {"fix": "fix lost", "sats": "satellites lost", "accuracy": "accuracy degraded",
         "cw": "jamming indicator", "jam_state": "jamming state", "agc": "AGC fell",
         "time_stopped": "GPS time stopped", "rejected": "estimator rejected GNSS",
         "glitch": "GPS glitch", "track": "position jumped from the estimator's",
         "drift": "estimator dragged by a drift", "baro": "altitude left the barometer's",
         "time_jump": "GPS time jumped", "receivers": "receivers disagreed"}  # fmt: skip
# what a peak means, for the event's evidence
PEAKS = {"fix": "lowest_fix", "sats": "fewest_sats", "accuracy": "worst_hacc_m",
         "cw": "highest_cw", "jam_state": "worst_jam_state", "agc": "lowest_agc_ratio",
         "time_stopped": "longest_time_stop_s", "rejected": "highest_test_ratio",
         "drift": "largest_innovation_mps", "time_jump": "largest_time_jump_s"}  # fmt: skip
SEVERITY: Mapping[str, Severity] = {"jamming": "caution", "spoofing": "safety",
                                    "obstruction": "info", "onboard": "caution",
                                    "receiver": "caution"}  # fmt: skip
Kind = Literal["jamming", "spoofing", "obstruction", "onboard", "receiver"]


@dataclass(frozen=True)
class Trip:
    """One channel past its threshold, from its first sample to the sample that ended it."""

    channel: str
    start_us: int
    end_us: int
    peak: float  # its worst value, in the channel's own unit
    receiver: int | None = None  # the GNSS instance; None for the estimator's own checks

    @property
    def signal(self) -> bool:
        return self.channel in SIGNAL


@dataclass(frozen=True)
class Disturbance:
    start_us: int
    end_us: int
    trips: tuple[Trip, ...]
    kind: Kind
    confidence: Confidence

    def channels(self) -> list[str]:
        return sorted({t.channel for t in self.trips}, key=list(READ).index)


# --- reading ------------------------------------------------------------------------------


def _ints(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.int64)


def _floats(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.float64)


def instances(table: pa.Table, column: str) -> Iterator[tuple[int, Array]]:
    if column not in table.column_names:
        yield 0, np.ones(table.num_rows, bool)
        return
    instance = _ints(table, column)
    for i in np.unique(instance):
        yield int(i), instance == i


def spans(t: Array, mask: Array) -> list[tuple[int, int]]:
    """Each run of True as row indices: its first row, and the row that ended it (one
    past its last; the log's last row when it ends there)."""
    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.astype(np.int8), [0]))))
    return [(int(a), int(min(b, len(t) - 1))) for a, b in zip(edges[::2], edges[1::2], strict=True)]


def _trips(
    channel: str, t: Array, mask: Array, values: Array, worst: Callable[[Array], Any],
    rules: Mapping[str, Any], receiver: int | None = None,
) -> list[Trip]:  # fmt: skip
    """The channel's trips: runs of `mask`, joined when less than merge_s apart (a fix
    that flickers is lost all along), kept when they last min_trip_s."""
    merge_us, min_us = float(rules["merge_s"]) * 1e6, float(rules["min_trip_s"]) * 1e6
    joined: list[tuple[int, int]] = []
    for a, b in spans(t, mask):
        if joined and t[a] - t[joined[-1][1]] <= merge_us:
            joined[-1] = (joined[-1][0], b)
        else:
            joined.append((a, b))
    return [
        Trip(channel, int(t[a]), int(t[b]), float(worst(values[a:b + 1][mask[a:b + 1]])), receiver)
        for a, b in joined if t[b] - t[a] >= min_us
    ]  # fmt: skip


def sample_at(t: Array, values: Array, at: Array, within_us: float) -> tuple[Array, Array]:
    """`values` sampled at `at` by interpolation, and which `at` had a sample within
    `within_us` on both sides to interpolate from."""
    if len(t) < 2:
        return np.full(len(at), np.nan), np.zeros(len(at), bool)
    k = np.clip(np.searchsorted(t, at), 1, len(t) - 1)
    near = (at >= t[0]) & (at <= t[-1]) & (t[k] - t[k - 1] <= 2 * within_us)
    return np.interp(at, t, values), near


# --- signal ---------------------------------------------------------------------------------


@dataclass
class Receiver:
    instance: int
    t: Array
    status: Array
    first_fix_us: int | None
    generation: str | None = None  # u-blox hardware generation, from its version message
    ublox: str | None = None  # "cw" or "state": how its UBX1 jamInd reads
    current_r: float | None = None  # its jamming indicator against motor current


def receivers(flight: FlightData, rules: Mapping[str, Any]) -> dict[int, Receiver]:
    gps = flight.table("GPS", ["TimeUS", "I", "Status"])
    if gps is None or gps.num_rows == 0 or "Status" not in gps.column_names:
        return {}
    out: dict[int, Receiver] = {}
    min_fix = int(rules["signal"]["min_fix"])
    for i, rows in instances(gps, "I"):
        t, status = _ints(gps, "TimeUS")[rows], _ints(gps, "Status")[rows]
        fixed = np.flatnonzero(status >= min_fix)
        out[i] = Receiver(i, t, status, int(t[fixed[0]]) if len(fixed) else None)
    return out


def signal(flight: FlightData, rules: Mapping[str, Any], found: Mapping[int, Receiver]
           ) -> list[Trip]:  # fmt: skip
    s, c = rules["signal"], rules["consistency"]
    gps = flight.table("GPS", ["TimeUS", "I", "Status", "NSats", "GMS", "GWk"])
    out: list[Trip] = []
    if gps is None or "NSats" not in gps.column_names:
        return out
    for i, rows in instances(gps, "I"):
        r = found.get(i)
        if r is None or r.first_fix_us is None:
            continue
        t, status, sats = (
            _ints(gps, "TimeUS")[rows],
            _ints(gps, "Status")[rows],
            _floats(gps, "NSats")[rows],
        )
        had = t >= r.first_fix_us  # from its first fix on
        fixed = status >= int(s["min_fix"])
        out += _trips("fix", t, had & ~fixed, status, np.min, rules, i)
        typical = float(np.median(sats[fixed]))
        low = had & ((sats < float(s["min_sats"])) | (sats <= typical - float(s["sats_drop"])))
        out += _trips("sats", t, low, sats, np.min, rules, i)
        if {"GMS", "GWk"} <= set(gps.column_names):
            out += _gps_time(t, _ints(gps, "GWk")[rows], _ints(gps, "GMS")[rows], fixed, s, c, i)
    gpa = flight.table("GPA", ["TimeUS", "I", "HAcc"])
    if gpa is not None and gpa.num_rows and "HAcc" in gpa.column_names:
        for i, rows in instances(gpa, "I"):
            r = found.get(i)
            if r is None or r.first_fix_us is None:
                continue
            t, hacc = _ints(gpa, "TimeUS")[rows], si.values(gpa, "HAcc")[rows]
            valid = (t >= r.first_fix_us) & np.isfinite(hacc) & (hacc > 0)
            if not np.any(valid):
                continue
            limit = max(float(s["hacc_m"]), float(s["hacc_factor"]) * float(np.median(hacc[valid])))
            out += _trips("accuracy", t, valid & (hacc > limit), hacc, np.max, rules, i)
    return out


def _gps_time(
    t: Array, week: Array, ms: Array, fixed: Array, s: Mapping[str, Any],
    c: Mapping[str, Any], receiver: int,
) -> list[Trip]:  # fmt: skip
    """GPS time standing still while the log's clock runs (signal), and jumping against
    it (consistency). Only samples with a week, that is with GPS time, count."""
    keep = week > 0
    t, gps_us, fixed = t[keep], week[keep] * 604_800_000_000 + ms[keep] * 1000, fixed[keep]
    if len(t) < 2:
        return []
    still = np.concatenate(([False], np.diff(gps_us) == 0))
    stop_us = float(s["time_stop_s"]) * 1e6
    out = [Trip("time_stopped", int(t[a]), int(t[b]), (t[b] - t[a]) / 1e6, receiver)
           for a, b in spans(t, still) if t[b] - t[a] >= stop_us]  # fmt: skip
    # a jump: GPS time against the log's clock, between fixed samples, not where it
    # resumes after standing still (catching up is not a jump)
    slip = (np.diff(gps_us) - np.diff(t)) / 1e6
    jump = (np.abs(slip) > float(c["time_jump_s"])) & fixed[1:] & fixed[:-1] & ~still[:-1]
    out += [Trip("time_jump", int(t[k + 1]), int(t[k + 1]), float(slip[k]), receiver)
            for k in np.flatnonzero(jump)]  # fmt: skip
    return out


def switched_off(flight: FlightData, function: int) -> list[tuple[int, int]]:
    """When an aux function's switch was high: AUXF from position 2 to the next change
    (or disarming)."""
    auxf = flight.table("AUXF", ["TimeUS", "function", "pos"])
    if auxf is None or auxf.num_rows == 0 or "function" not in auxf.column_names:
        return []
    t, fn, pos = _ints(auxf, "TimeUS"), _ints(auxf, "function"), _ints(auxf, "pos")
    out: list[tuple[int, int]] = []
    began: int | None = None
    for k in np.argsort(t, kind="stable"):
        if fn[k] != function:
            continue
        if pos[k] == 2 and began is None:
            began = int(t[k])
        elif pos[k] != 2 and began is not None:
            out.append((began, int(t[k])))
            began = None
    if began is not None:
        out.append((began, max(began, flight.disarm_us)))
    return out


def generations(flight: FlightData, rules: Mapping[str, Any]) -> dict[int, str]:
    """Each u-blox receiver's hardware generation, from the version message the driver
    logs: "u-blox <instance + 1> HW: <version> SW: <version>". The text stays here."""
    msg = flight.table("MSG", ["TimeUS", "Message"])
    if msg is None or "Message" not in msg.column_names:
        return {}
    known = {k.upper(): v for k, v in rules["ublox"]["hardware"].items()}
    out: dict[int, str] = {}
    for raw in msg.column("Message").to_pylist():
        line = text(raw) if isinstance(raw, bytes) else str(raw)
        found = re.match(r"u-blox (\d+) HW: ([0-9A-Fa-f]+)", line)
        if found and (generation := known.get(found.group(2).upper())):
            out[int(found.group(1)) - 1] = generation
    return out


def ublox(flight: FlightData, rules: Mapping[str, Any], found: Mapping[int, Receiver]
          ) -> list[Trip]:  # fmt: skip
    """The u-blox status: jamming indicator or state, and the AGC count. An M10's UBX1
    comes from MON-RF, whose jamInd is the jamming state (0 to 3); every other receiver's
    from MON-HW, the continuous-wave indicator (0 to 255)."""
    u = rules["ublox"]
    ubx = flight.table("UBX1", ["TimeUS", "Instance", "jamInd", "agcCnt"])
    if ubx is None or ubx.num_rows == 0 or "jamInd" not in ubx.column_names:
        return []
    gens = generations(flight, rules)
    out: list[Trip] = []
    for i, rows in instances(ubx, "Instance"):
        t, jam = _ints(ubx, "TimeUS")[rows], _floats(ubx, "jamInd")[rows]
        state = gens.get(i) in u["state_generations"] and np.max(jam) <= 3
        if state:
            out += _trips("jam_state", t, jam >= float(u["state_warning"]), jam, np.max, rules, i)
        else:
            out += _trips("cw", t, jam > float(u["cw_jamming"]), jam, np.max, rules, i)
        # the AGC turns down against a jammer, so its fall is the state's continuous measure
        indicator = jam
        if "agcCnt" in ubx.column_names:
            agc = _floats(ubx, "agcCnt")[rows]
            usual = float(np.percentile(agc[agc > 0], 90)) if np.any(agc > 0) else 0.0
            if usual > 0:
                low = agc < (1 - float(u["agc_drop"])) * usual
                out += _trips("agc", t, low, agc / usual, np.min, rules, i)
            indicator = -agc if state else jam
        if (r := found.get(i)) is not None:
            r.generation, r.ublox = gens.get(i), "state" if state else "cw"
            r.current_r = _current_r(flight, t, indicator)
    return out


def _current_r(flight: FlightData, t: Array, indicator: Array) -> float | None:
    """How closely the jamming indicator follows motor current while armed: the battery's
    current at the indicator's samples. None when either stays flat."""
    bat = flight.table("BAT", ["TimeUS", "Inst", "Curr"])
    if bat is None or bat.num_rows < 2 or "Curr" not in bat.column_names:
        return None
    rows = _ints(bat, "Inst") == 0 if "Inst" in bat.column_names else np.ones(bat.num_rows, bool)
    bt, curr = _ints(bat, "TimeUS")[rows], si.values(bat, "Curr")[rows]
    armed = (t >= flight.arm_us) & (t <= flight.disarm_us)
    current, near = sample_at(bt, curr, t, 1e6)
    keep = armed & near & np.isfinite(current) & np.isfinite(indicator)
    if np.count_nonzero(keep) < 20 or np.std(current[keep]) == 0 or np.std(indicator[keep]) == 0:
        return None
    return float(np.corrcoef(current[keep], indicator[keep])[0, 1])


# --- consistency ------------------------------------------------------------------------------


def estimator(flight: FlightData, rules: Mapping[str, Any], names: Mapping[str, Any]
              ) -> list[Trip]:  # fmt: skip
    c = rules["consistency"]
    out: list[Trip] = []
    x4 = flight.table("XKF4", ["TimeUS", "C", "SV", "SP", "SH", "PI"])
    primary_t, primary = np.zeros(0, np.int64), np.zeros(0, np.int64)
    if x4 is not None and x4.num_rows and {"C", "SV", "SP", "SH"} <= set(x4.column_names):
        t, core = _ints(x4, "TimeUS"), _ints(x4, "C")
        pi = _ints(x4, "PI") if "PI" in x4.column_names else np.zeros(len(t), np.int64)
        rows = core == pi
        primary_t, primary = t[rows], pi[rows]
        ratio = np.max([logged_ratio(x4, f, c)[rows] for f in ("SV", "SP", "SH")], axis=0)
        out += _trips("rejected", t[rows], ratio > float(c["test_ratio"]), ratio, np.max, rules)
    x3 = flight.table("XKF3", ["TimeUS", "C", "IVN", "IVE"])
    if x3 is not None and x3.num_rows and {"C", "IVN", "IVE"} <= set(x3.column_names):
        t = _ints(x3, "TimeUS")
        if len(primary_t):  # the core that was primary at each sample
            k = np.clip(np.searchsorted(primary_t, t, side="right") - 1, 0, len(primary_t) - 1)
            rows = _ints(x3, "C") == primary[k]
        else:
            rows = _ints(x3, "C") == 0
        out += _drift(t[rows], si.values(x3, "IVN")[rows], si.values(x3, "IVE")[rows], rules)
    out += _glitches(flight, rules, names)
    return out


def logged_ratio(table: pa.Table, name: str, c: Mapping[str, Any]) -> Array:
    """A test ratio. 4.7.0 logs 100 times it with a multiplier of 1 (rules: its source);
    a log whose own multiplier scales the field is read as it says."""
    unscaled = si.scale_of(si.field_meta(table, name)) == 1.0
    return si.values(table, name) / (float(c["logged_ratio_scale"]) if unscaled else 1.0)


def _drift(t: Array, north: Array, east: Array, rules: Mapping[str, Any]) -> list[Trip]:
    """A one-sided velocity innovation: its trailing mean over the window, once full."""
    if len(t) < 2:
        return []
    c = rules["consistency"]
    window = float(c["drift_window_s"]) * 1e6
    start = np.searchsorted(t, t - window, side="left")
    count = np.arange(1, len(t) + 1) - start

    def mean(v: Array) -> Array:
        total = np.concatenate(([0.0], np.cumsum(np.nan_to_num(v))))
        return (total[1:] - total[start]) / count

    size = np.hypot(mean(north), mean(east))
    full = t - t[start] >= 0.8 * window
    return _trips("drift", t, full & (size > float(c["drift_mps"])), size, np.max, rules)


def _glitches(flight: FlightData, rules: Mapping[str, Any], names: Mapping[str, Any]) -> list[Trip]:
    """ERR GPS glitch to its resolution (or the end of the flight)."""
    err = flight.table("ERR", ["TimeUS", "Subsys", "ECode"])
    gps_id = next(
        (int(k) for k, v in names.get("error_subsystems", {}).items() if v == "GPS"), None
    )
    if err is None or err.num_rows == 0 or gps_id is None:
        return []
    t, sub, code = _ints(err, "TimeUS"), _ints(err, "Subsys"), _ints(err, "ECode")
    out: list[Trip] = []
    began: int | None = None
    for k in np.argsort(t, kind="stable"):
        if sub[k] != gps_id:
            continue
        if code[k] in rules["glitch_codes"] and began is None:
            began = int(t[k])
        elif code[k] == 0 and began is not None:
            out.append(Trip("glitch", began, int(t[k]), 1.0))
            began = None
    if began is not None:
        out.append(Trip("glitch", began, max(began, flight.disarm_us), 1.0))
    return out


@dataclass
class Positions:
    trips: list[Trip]
    resets: list[tuple[int, float]]  # the estimator's position jumping: when, and how far


def positions(flight: FlightData, rules: Mapping[str, Any], found: Mapping[int, Receiver]
              ) -> Positions:  # fmt: skip
    """Receivers against the estimator and each other, and the estimator's resets, as
    local tracks from one origin. Only samples with a 3D fix count for a receiver."""
    c = rules["consistency"]
    tracks = flight.local_tracks()
    out = Positions([], [])
    pos = tracks.get("POS")
    fixed: dict[int, tuple[Array, Array, Array]] = {}
    for name, track in tracks.items():
        if not name.startswith("GPS") or int(name[3:]) not in found:
            continue
        r = found[int(name[3:])]
        t = track.times_us
        k = np.clip(np.searchsorted(r.t, t), 0, len(r.t) - 1)
        ok = (r.t[k] == t) & (r.status[k] >= int(rules["signal"]["min_fix"]))
        e, n = track.east_north_m()
        fixed[r.instance] = (t[ok], e[ok], n[ok])
        if pos is not None:
            pe, pn = pos.east_north_m()
            at_e, near = sample_at(pos.times_us, pe, t, 0.5e6)
            at_n, _ = sample_at(pos.times_us, pn, t, 0.5e6)
            gap = np.hypot(e - at_e, n - at_n)
            out.trips += _trips("track", t, ok & near & (gap > float(c["track_m"])), gap, np.max,
                                rules, r.instance)  # fmt: skip
    if len(fixed) >= 2:
        (t0, e0, n0), (t1, e1, n1) = (fixed[i] for i in sorted(fixed)[:2])
        at_e, near = sample_at(t1, e1, t0, 0.5e6)
        at_n, _ = sample_at(t1, n1, t0, 0.5e6)
        gap = np.hypot(e0 - at_e, n0 - at_n)
        out.trips += _trips(
            "receivers", t0, near & (gap > float(c["receivers_m"])), gap, np.max, rules
        )
    if pos is not None and len(pos) > 1:
        e, n = pos.east_north_m()
        t = pos.times_us
        step, dt = np.hypot(np.diff(e), np.diff(n)), np.diff(t) / 1e6
        jump = (step > float(rules["reset"]["m"])) & (
            step > float(rules["reset"]["mps"]) * np.maximum(dt, 1e-3)
        )
        out.resets = [(int(t[k + 1]), float(step[k])) for k in np.flatnonzero(jump)]
    return out


def baro(flight: FlightData, rules: Mapping[str, Any], found: Mapping[int, Receiver]) -> list[Trip]:
    """The first receiver's altitude against the first barometer's, around their usual
    offset over the armed flight, with the barometer's error at speed allowed for."""
    gps = flight.table("GPS")
    bar = flight.table("BARO", ["TimeUS", "I", "Alt"])
    r = found.get(0)
    if (
        gps is None
        or bar is None
        or r is None
        or "Alt" not in gps.column_names
        or "Alt" not in bar.column_names
    ):
        return []
    rows = next((rows for i, rows in instances(gps, "I") if i == 0), None)
    if rows is None:
        return []
    t, alt = _ints(gps, "TimeUS")[rows], si.values(gps, "Alt")[rows]
    brows = next((rows for i, rows in instances(bar, "I") if i == 0), np.ones(bar.num_rows, bool))
    baro_alt, near = sample_at(_ints(bar, "TimeUS")[brows], si.values(bar, "Alt")[brows], t, 0.5e6)
    k = np.clip(np.searchsorted(r.t, t), 0, len(r.t) - 1)
    ok = near & (r.t[k] == t) & (r.status[k] >= int(rules["signal"]["min_fix"])) & np.isfinite(alt)
    armed = ok & (t >= flight.arm_us) & (t <= flight.disarm_us)
    if np.count_nonzero(armed) < 10:
        return []
    off = alt - baro_alt
    dev = np.abs(off - float(np.median(off[armed])))
    spec = rules["consistency"]
    speed = _nearby_max(t, _speed(gps, rows), float(spec["baro_speed_window_s"]) * 1e6)
    allowed = float(spec["baro_m"]) + float(spec["baro_dynamic_ratio"]) * speed**2 / (2 * GRAVITY)
    return _trips("baro", t, ok & (dev > allowed), dev, np.max, rules, 0)


def _speed(gps: pa.Table, rows: Array) -> Array:
    """The receiver's speed over the ground, horizontal and vertical, in m/s (0 where the
    log has no speed: the barometer is then held to the plain gap)."""
    out = np.zeros(int(np.count_nonzero(rows)))
    for name in ("Spd", "VZ"):
        if name in gps.column_names:
            v = si.values(gps, name)[rows]
            out += np.where(np.isfinite(v), v, 0.0) ** 2
    return np.sqrt(out)


def _nearby_max(t: Array, values: Array, within_us: float) -> Array:
    """Each sample's largest value within `within_us` either side: the barometer's
    filter lags the speed that moves it."""
    lo = np.searchsorted(t, t - within_us, "left")
    hi = np.searchsorted(t, t + within_us, "right")
    return np.array([values[a:b].max() for a, b in zip(lo, hi, strict=True)])


# --- events -------------------------------------------------------------------------------


def group(trips: Sequence[Trip], merge_us: float) -> list[list[Trip]]:
    """Trips within `merge_us` of each other, chained, are one event."""
    out: list[list[Trip]] = []
    end = 0
    for trip in sorted(trips, key=lambda x: (x.start_us, list(READ).index(x.channel))):
        if out and trip.start_us <= end + merge_us:
            out[-1].append(trip)
            end = max(end, trip.end_us)
        else:
            out.append([trip])
            end = trip.end_us
    return out


def classify(trips: Sequence[Trip], rules: Mapping[str, Any], found: Mapping[int, Receiver]
             ) -> tuple[Kind, Confidence]:  # fmt: skip
    merge_us, short_us = float(rules["merge_s"]) * 1e6, float(rules["short_s"]) * 1e6
    sig = [t for t in trips if t.signal]
    # a consistency failure that began while the signal looked healthy: what a spoofer
    # (or a receiver's own jump) does; one that began inside a signal loss is its effect
    healthy = [c for c in trips if not c.signal and not any(
        s.start_us - merge_us <= c.start_us <= s.end_us + merge_us for s in sig)]  # fmt: skip
    if healthy:
        groups = {GROUPS[c.channel] for c in healthy}
        span = max(c.end_us for c in healthy) - min(c.start_us for c in healthy)
        if len(groups) >= 2:
            return "spoofing", "high"
        return "spoofing", "medium" if len(
            {c.channel for c in healthy}
        ) >= 2 or span >= short_us else "low"
    others = {s.channel for s in sig} - set(UBLOX)
    jammed = [s for s in sig if s.channel in UBLOX]
    if jammed:
        follows = float(rules["onboard_correlation"])
        if any((r := found.get(s.receiver if s.receiver is not None else -1)) is not None
               and r.current_r is not None and r.current_r >= follows for s in jammed):  # fmt: skip
            return "onboard", "medium"
        return "jamming", "high" if others else "medium"
    hit = {s.receiver for s in sig if s.receiver is not None}
    if sum(r.first_fix_us is not None for r in found.values()) >= 2 and len(hit) == 1:
        return "receiver", "low"
    # its u-blox status was logged and stayed quiet: broadband jamming would have moved
    # the AGC, so a loss with a quiet band is something in the way
    quiet = any(found[i].ublox is not None for i in hit if i in found)
    span = max(t.end_us for t in trips) - min(t.start_us for t in trips)
    if not quiet and others & set(LOST) and span >= short_us:
        return "jamming", "medium" if len(others) >= 2 else "low"
    return "obstruction", "medium" if quiet else "low"


def disturbances(trips: Sequence[Trip], flight: FlightData, rules: Mapping[str, Any],
                 found: Mapping[int, Receiver]) -> list[Disturbance]:  # fmt: skip
    """The armed flight's events: trips clipped to arming and disarming, then grouped."""
    armed = [
        Trip(t.channel, max(t.start_us, flight.arm_us), min(t.end_us, flight.disarm_us),
             t.peak, t.receiver)
        for t in trips if t.end_us >= flight.arm_us and t.start_us <= flight.disarm_us
    ]  # fmt: skip
    out: list[Disturbance] = []
    for g in group(armed, float(rules["merge_s"]) * 1e6):
        kind, confidence = classify(g, rules, found)
        out.append(Disturbance(min(t.start_us for t in g), max(t.end_us for t in g), tuple(g),
                               kind, confidence))  # fmt: skip
    return out


# --- how the aircraft coped -------------------------------------------------------------------


@dataclass(frozen=True)
class Coping:
    rejected_s: float  # how long the primary core rejected GNSS
    followed: bool  # dragged by a drift it never rejected
    mode: str | None  # the first mode change from the event to response_s after it
    reason: str | None
    failsafes: tuple[str, ...]  # failsafe and check errors in the same window
    yaw_resets: tuple[int, ...]  # heading resets from GNSS velocity, during the event
    resets: tuple[float, ...]  # the estimator's position jumps, metres

    def words(self) -> str:
        out: list[str] = []
        if self.rejected_s > 0:
            out.append(f"the estimator rejected GNSS for {self.rejected_s:g} s")
        elif self.followed:
            out.append("the estimator kept following the drifting position and never rejected it")
        else:
            out.append("the estimator did not reject GNSS")
        if self.failsafes:
            out.append(f"{_plain(', '.join(self.failsafes))} triggered")
        if self.mode is not None:
            out.append(f"the mode changed to {self.mode} by {_plain(self.reason or 'UNKNOWN')}")
        return "; ".join(out)


def _plain(name: str) -> str:
    """An enum name as words: "FAILSAFE_EKFINAV" reads "EKFINAV failsafe"."""
    words = [f"{w.removeprefix('FAILSAFE_')} failsafe" if w.startswith("FAILSAFE_") else w
             for w in name.split(", ")]  # fmt: skip
    out = ", ".join(words).replace("EKFCHECK", "EKF check").replace("_", " ")
    return " ".join(w if w in ("GCS", "RC", "EKF", "EKFINAV", "GPS") else w.lower()
                    for w in out.split(" "))  # fmt: skip


def coping(flight: FlightData, d: Disturbance, rules: Mapping[str, Any], names: Mapping[str, Any],
           pos: Positions, yaw: Sequence[int]) -> Coping:  # fmt: skip
    after = d.end_us + float(rules["response_s"]) * 1e6
    rejected = [t for t in d.trips if t.channel == "rejected"]
    mode = reason = None
    table = flight.table("MODE", ["TimeUS", "ModeNum", "Rsn"])
    if table is not None and table.num_rows and "Rsn" in table.column_names:
        t, num, rsn = _ints(table, "TimeUS"), _ints(table, "ModeNum"), _ints(table, "Rsn")
        for k in np.argsort(t, kind="stable"):
            if d.start_us <= t[k] <= after:
                mode = (
                    names.get("modes", {})
                    .get("copter", {})
                    .get(str(int(num[k])), f"MODE_{int(num[k])}")
                )
                reason = names.get("mode_reasons", {}).get(str(int(rsn[k])), "UNKNOWN")
                break
    failsafes: list[str] = []
    err = flight.table("ERR", ["TimeUS", "Subsys", "ECode"])
    if err is not None and err.num_rows:
        subsystems: Mapping[str, str] = names.get("error_subsystems", {})
        t, sub, code = _ints(err, "TimeUS"), _ints(err, "Subsys"), _ints(err, "ECode")
        for k in np.argsort(t, kind="stable"):
            name = subsystems.get(str(int(sub[k])), "")
            failed = code[k] != 0 and name.startswith(("FAILSAFE_", "EKFCHECK"))
            if d.start_us <= t[k] <= after and failed and name not in failsafes:
                failsafes.append(name)
    channels = {t.channel for t in d.trips}
    merged = d.end_us + float(rules["merge_s"]) * 1e6
    return Coping(
        rejected_s=round(sum(t.end_us - t.start_us for t in rejected) / 1e6, 1),
        followed="drift" in channels and not channels & {"rejected", "glitch"},
        mode=mode, reason=reason, failsafes=tuple(failsafes),
        yaw_resets=tuple(y for y in yaw if d.start_us <= y <= merged),
        resets=tuple(m for t, m in pos.resets if d.start_us <= t <= merged),
    )  # fmt: skip


def yaw_resets(flight: FlightData, rules: Mapping[str, Any]) -> list[int]:
    """When the estimator reset its heading from GNSS velocity (the text stays here)."""
    msg = flight.table("MSG", ["TimeUS", "Message"])
    if msg is None or "Message" not in msg.column_names:
        return []
    words: Sequence[str] = rules["yaw_reset"]
    t = _ints(msg, "TimeUS")
    said = [text(raw) if isinstance(raw, bytes) else str(raw)
            for raw in msg.column("Message").to_pylist()]  # fmt: skip
    return [int(t[k]) for k, line in enumerate(said) if any(w in line for w in words)]


# --- the analyzer ---------------------------------------------------------------------------


def _fields(channels: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    messages = sorted({m for c in channels for m in READ[c][0]})
    fields = sorted({f for c in channels for f in READ[c][1]})
    return tuple(messages), tuple(fields)


LOCAL = {"track": "position", "receivers": "position", "baro": "altitude",
         "time_stopped": "time", "time_jump": "time"}  # fmt: skip


def _event_evidence(flight: FlightData, n: int, d: Disturbance, c: Coping) -> list[Evidence]:
    def at(us: int) -> float:
        return round((us - flight.arm_us) / 1e6, 1)

    seg = Segment("gnss", d.start_us, d.end_us)
    channels = d.channels()
    clear = [ch for ch in channels if ch not in LOCAL]
    peaks: dict[str, float] = {}
    for t in d.trips:
        key = PEAKS.get(t.channel)
        if key is None or t.channel in LOCAL:
            continue
        better = min if key.startswith(("lowest", "fewest")) else max
        peaks[key] = round(better(peaks.get(key, t.peak), t.peak), 2)
    read = _fields(clear)
    messages = tuple(
        sorted({*read[0], *(("MODE",) if c.mode else ()), *(("ERR",) if c.failsafes else ())})
    )
    fields = tuple(sorted({*read[1], *(("ModeNum", "Rsn") if c.mode else ()),
                           *(("ECode", "Subsys") if c.failsafes else ())}))  # fmt: skip
    out = [Evidence(
        id=f"gnss:event:{n}",
        kind="fact",
        summary=f"GNSS {d.kind} ({d.confidence} confidence) at {at(d.start_us)} s for "
        f"{round((d.end_us - d.start_us) / 1e6, 1)} s: {', '.join(WORDS[x] for x in channels)}; "
        f"{c.words()}",
        messages=messages, fields=fields, segment=seg,
        values={"start_s": at(d.start_us), "end_s": at(d.end_us),
                "duration_s": round((d.end_us - d.start_us) / 1e6, 1), "class": d.kind,
                "confidence": d.confidence, "channels": channels,
                "receivers": sorted({t.receiver + 1 for t in d.trips if t.receiver is not None}),
                "rejected_s": c.rejected_s, "followed_drift": c.followed,
                "mode": c.mode, "mode_reason": c.reason, "failsafes": list(c.failsafes), **peaks},
    )]  # fmt: skip
    gaps = {t.channel: 0.0 for t in d.trips if LOCAL.get(t.channel) == "position"}
    for t in d.trips:
        if t.channel in gaps:
            gaps[t.channel] = max(gaps[t.channel], t.peak)
    if gaps or c.resets:
        values: dict[str, Any] = {f"largest_{k}_gap_m": round(v) for k, v in gaps.items()}
        if c.resets:
            values |= {"estimator_resets": len(c.resets), "largest_reset_m": round(max(c.resets))}
        out.append(Evidence(
            id=f"gnss:event:{n}:position", kind="fact",
            summary="; ".join([*(f"{WORDS[k]} by up to {round(v)} m" for k, v in gaps.items()),
                               *([f"the estimator's position reset {len(c.resets)} times, by up to "
                                  f"{round(max(c.resets))} m"] if c.resets else [])]),
            messages=("GPS", "POS"), fields=("Lat", "Lng"), segment=seg, values=values,
        ))  # fmt: skip
    baro_peak = max((t.peak for t in d.trips if t.channel == "baro"), default=None)
    if baro_peak is not None:
        out.append(Evidence(
            id=f"gnss:event:{n}:altitude", kind="fact",
            summary=f"GNSS altitude left the barometer's by up to {round(baro_peak)} m",
            messages=("BARO", "GPS"), fields=("Alt",), segment=seg,
            values={"largest_altitude_gap_m": round(baro_peak)},
        ))  # fmt: skip
    times = [t for t in d.trips if LOCAL.get(t.channel) == "time"]
    if times:
        stop = max((t.peak for t in times if t.channel == "time_stopped"), default=0.0)
        jump = max((abs(t.peak) for t in times if t.channel == "time_jump"), default=0.0)
        out.append(Evidence(
            id=f"gnss:event:{n}:time", kind="fact",
            summary=f"GPS time stood still for up to {stop:.1f} s and jumped by up to {jump:.1f} s "
            "against the log's clock",
            messages=("GPS",), fields=("GMS", "GWk"), segment=seg,
            values={"longest_time_stop_s": round(stop, 1), "largest_time_jump_s": round(jump, 1)},
        ))  # fmt: skip
    return out


def _first(flight: FlightData, name: str, columns: list[str], since_us: int
           ) -> tuple[pa.Table, Array] | None:  # fmt: skip
    """The first receiver's rows of a GNSS message, from `since_us` to disarming."""
    table = flight.table(name, ["TimeUS", "I", *columns])
    if table is None or any(c not in table.column_names for c in columns):
        return None
    rows = next((rows for i, rows in instances(table, "I") if i == 0), None)
    if rows is None:
        return None
    t = _ints(table, "TimeUS")
    rows = rows & (t >= max(flight.arm_us, since_us)) & (t <= flight.disarm_us)
    return (table, rows) if np.any(rows) else None


def _flight_evidence(
    flight: FlightData, found: Mapping[int, Receiver], events: Sequence[Disturbance]
) -> tuple[Evidence, dict[str, float]]:
    """The first receiver over the armed flight, for the metrics."""
    disturbed = round(sum((d.end_us - d.start_us) / 1e6 for d in events), 1)
    values: dict[str, Any] = {
        "events": len(events),
        "receivers": len(found),
        "disturbed_s": disturbed,
    }
    # which receivers logged their u-blox status (their generation comes from status
    # text, which stays here)
    values["ublox_status"] = sorted(i + 1 for i, r in found.items() if r.ublox is not None)
    metrics = {"gnss.disturbed_s": disturbed}
    words = [f"{len(events)} GNSS disturbances over {disturbed} s while armed"]
    since = found[0].first_fix_us if 0 in found else None
    if since is not None and (gps := _first(flight, "GPS", ["NSats"], since)) is not None:
        fewest = float(np.min(_floats(gps[0], "NSats")[gps[1]]))
        metrics["gnss.sats_min"] = values["fewest_sats"] = fewest
        words.append(f"fewest satellites {fewest:g}")
    if since is not None and (gpa := _first(flight, "GPA", ["HAcc"], since)) is not None:
        hacc = si.values(gpa[0], "HAcc")[gpa[1]]
        if np.any(hacc > 0):
            p95 = round(float(np.percentile(hacc[hacc > 0], 95)), 2)
            metrics["gnss.hacc_p95"] = values["hacc_p95_m"] = p95
            words.append(f"95% of the time the accuracy was within {p95} m")
    ev = Evidence(
        id="gnss:flight", kind="fact", summary="; ".join(words),
        messages=("GPA", "GPS", "XKF3", "XKF4"),
        fields=("C", "HAcc", "IVE", "IVN", "NSats", "PI", "SH", "SP", "Status", "SV"),
        segment=flight.armed, values=values,
    )  # fmt: skip
    return ev, metrics


def _gnss(flight: FlightData, reg: Registry) -> Result:
    if not flight.logged:
        return Result(skipped="a parameter file has no flight to judge")
    rules: Mapping[str, Any] = reg.rules["gnss"]
    names = reg.names(flight.firmware)
    found = receivers(flight, rules)
    if not found:
        return Result(skipped="the log holds no GNSS receiver")
    pos = positions(flight, rules, found)
    trips = [*signal(flight, rules, found), *ublox(flight, rules, found),
             *estimator(flight, rules, names), *pos.trips, *baro(flight, rules, found)]  # fmt: skip
    # a GNSS-off drill is not a disturbance: the no-GNSS analyzer measures it
    off = switched_off(flight, int(reg.rules["no_gnss"]["aux"]["gps_disable"]))
    merge_us = float(rules["merge_s"]) * 1e6
    trips = [t for t in trips if not any(a <= t.start_us <= b + merge_us for a, b in off)]
    events = disturbances(trips, flight, rules, found)
    yaw = yaw_resets(flight, rules)
    out = Result()
    for n, d in enumerate(events, 1):
        c = coping(flight, d, rules, names, pos, yaw)
        evidence = _event_evidence(flight, n, d, c)
        out.evidence += evidence
        start_s = round((d.start_us - flight.arm_us) / 1e6, 1)
        params: dict[str, Any] = {
            "start_s": start_s, "duration_s": round((d.end_us - d.start_us) / 1e6, 1),
            "channels": ", ".join(WORDS[x] for x in d.channels()), "response": c.words(),
            "receivers": " and ".join(f"GPS {t + 1}" for t in sorted(
                {t.receiver for t in d.trips if t.receiver is not None})) or "GNSS",
        }  # fmt: skip
        if d.kind == "onboard":
            follows = [found[i].current_r for i in {t.receiver for t in d.trips} if i in found]
            params["r"] = round(max(r for r in follows if r is not None), 2)
        finding = Finding(f"gnss.{d.kind}", SEVERITY[d.kind], d.confidence, tuple(evidence), params)
        out.findings.append(finding)
        if d.kind == "spoofing" and c.yaw_resets:
            at_s = round((c.yaw_resets[0] - flight.arm_us) / 1e6, 1)
            yev = Evidence(
                id=f"gnss:event:{n}:yaw", kind="fact",
                summary=f"{len(c.yaw_resets)} heading resets from GNSS velocity, first at {at_s} s",
                messages=("MSG",), fields=("Message",), segment=evidence[0].segment,
                values={"yaw_resets": len(c.yaw_resets), "first_s": at_s},
            )  # fmt: skip
            out.evidence.append(yev)
            params = {"at_s": at_s, "count": len(c.yaw_resets), "start_s": start_s}
            out.findings.append(
                Finding("gnss.yaw_reset", "safety", "medium", (yev, evidence[0]), params)
            )
    ev, metrics = _flight_evidence(flight, found, events)
    out.evidence.append(ev)
    for metric, value in metrics.items():
        out.metrics.append(
            MetricValue(metric, "flight", value, reg.metrics[metric].zone(value), ev.id)
        )
    return out


GNSS = Analyzer(
    name="gnss",
    version=3,
    run=_gnss,
    metrics=("gnss.disturbed_s", "gnss.sats_min", "gnss.hacc_p95"),
    findings=("gnss.jamming", "gnss.spoofing", "gnss.obstruction", "gnss.onboard",
              "gnss.receiver", "gnss.yaw_reset"),
)  # fmt: skip
