"""Failsafe precursors: the warning each failsafe gave before it fired, and how close the
battery failsafe came on flights where it did not (spec: Failsafe causes and RF early
warning, "Lead-time analysis" and "Near misses").

Each precursor is a series with a caution band (rules/precursors.json). Where a metric
defines the band, its target zone is the band, so the warnings agree with the metric:

- **Ground station:** how long the GCS has gone unheard against `FS_GCS_TIMEOUT`
  (`MAV.mgs`); packets missed on the way up (`MAV.rxdp`) and down (the tlog's sequence
  numbers) over a sliding window; the radio's TX buffer (`RAD` or RADIO_STATUS).
- **Battery:** how long the failsafe's voltage has stayed under `BATT_LOW_VOLT` against
  `BATT_LOW_TIMER`; the sag-compensated voltage (`VoltR`) close to `BATT_LOW_VOLT`; the
  charge used against `BATT_CAPACITY` less `BATT_LOW_MAH`.
- **Radio:** the receiver's own failsafe flag (`RCI2.Flags` bit 2).

Before each failsafe, the first time each of its precursors left its band within the
look-back window gives that precursor's lead time. The near misses for the ground station
and the receiver are the link analyzer's (`link.gcs_silence`, `link.rc_receiver_failsafes`);
the battery's are here. The EKF's come with the GNSS work. Positions are never read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.failsafe_onset import MONITORS, last_seen
from flight_insights.analyzers.failsafes import Occurrence, occurrences
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.links import radio_table
from flight_insights.analyzers.model import Analyzer, Evidence, Finding, MetricValue, Result
from flight_insights.analyzers.registry import Metric, Registry
from flight_insights.tlog.decode import SeqCounter

Array = np.ndarray[Any, Any]
COUNTER = 1 << 16  # MAV's packet counts are 16-bit
PROTOCOL_FAILSAFE = 1 << 2  # RCI2.Flags
IGNORE_RECEIVER, IGNORE_RECEIVER_FAILSAFE = 1 << 0, 1 << 2  # RC_OPTIONS


@dataclass(frozen=True)
class Series:
    """One precursor: when it was sampled, and whether it was outside its band then."""

    id: str
    what: str  # in words
    times: Array
    warned: Array
    messages: tuple[str, ...]
    fields: tuple[str, ...]


def _ints(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.int64)


def _floats(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.float64)


def _param(flight: FlightData, name: str, fallback: float = 0.0) -> float:
    return float(flight.params.get(name, flight.defaults.get(name, fallback)))


def off_target(metric: Metric, values: Array) -> Array:
    """Where values fall outside the metric's target zone (NaN never does)."""
    inside = np.zeros(len(values), bool)
    for low, high in metric.zones.get("target", ()):
        inside |= (values >= (low if low is not None else -np.inf)) & (
            values < (high if high is not None else np.inf)
        )
    return ~inside & np.isfinite(values)


def windowed_loss(times: Array, got: Array, missed: Array, window_us: int, least: int) -> Array:
    """The share missed (%) over the window ending at each sample, from each sample's
    counts; NaN where fewer than `least` packets fell in the window."""
    g = np.concatenate(([0], np.cumsum(got)))
    m = np.concatenate(([0], np.cumsum(missed)))
    k = np.arange(len(times))
    j = np.searchsorted(times, times - window_us, side="right")  # first sample inside
    got_w, missed_w = g[k + 1] - g[j], m[k + 1] - m[j]
    total = got_w + missed_w
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(total >= least, 100.0 * missed_w / total, np.nan)


def run_lengths(times: Array, mask: Array) -> Array:
    """At each sample, how long (µs) the mask has held, from the first sample of its run."""
    out = np.zeros(len(times), np.int64)
    start: int | None = None
    for k in range(len(times)):
        if mask[k]:
            start = int(times[k]) if start is None else start
            out[k] = int(times[k]) - start
        else:
            start = None
    return out


# --- the ground station -----------------------------------------------------------------


def _mav(flight: FlightData) -> tuple[Array, Array, dict[str, Array]] | None:
    """MAV in time order: its times, links and the counts it carries."""
    mav = flight.table("MAV")
    if mav is None or mav.num_rows == 0 or "chan" not in mav.column_names:
        return None
    times = _ints(mav, "TimeUS")
    order = np.argsort(times, kind="stable")
    counts = {n: _ints(mav, n)[order] for n in ("mgs", "rxp", "rxdp") if n in mav.column_names}
    return times[order], _ints(mav, "chan")[order], counts


def _gcs(flight: FlightData, reg: Registry, rules: Mapping[str, Any]) -> list[Series]:
    out: list[Series] = []
    spec: Mapping[str, Any] = rules["precursors"]["FAILSAFE_GCS"]
    found = _mav(flight)
    if found is not None:
        times, chan, mav = found
        if "mgs" in mav:
            seen = last_seen(times, chan, mav["mgs"])
            timeout = _param(flight, "FS_GCS_TIMEOUT", 5.0)
            heard = seen > 0
            if np.any(heard) and timeout > 0:
                share = np.where(heard, (times - seen) / 1e6 / timeout, np.nan)
                metric = reg.metrics[spec["gcs_silence"]["metric"]]
                out.append(Series(
                    "gcs_silence", "the ground station unheard", times[heard],
                    off_target(metric, share)[heard], ("MAV",), ("mgs",),
                ))  # fmt: skip
        if "rxp" in mav and "rxdp" in mav:
            window = round(float(rules["window_s"]) * 1e6)
            least = int(rules["window_packets"])
            parts: list[tuple[Array, Array]] = []
            for c in np.unique(chan):
                rows = chan == c
                t = times[rows]
                got = np.concatenate(([0], np.diff(mav["rxp"][rows]) % COUNTER))
                missed = np.concatenate(([0], np.diff(mav["rxdp"][rows]) % COUNTER))
                parts.append((t, windowed_loss(t, got, missed, window, least)))
            t = np.concatenate([p[0] for p in parts])
            loss = np.concatenate([p[1] for p in parts])
            order = np.argsort(t, kind="stable")
            metric = reg.metrics[spec["uplink_loss"]["metric"]]
            out.append(Series(
                "uplink_loss", "packets missed on the way up", t[order],
                off_target(metric, loss[order]), ("MAV",), ("rxp", "rxdp"),
            ))  # fmt: skip
    down = _downlink(flight, reg, rules, spec)
    if down is not None:
        out.append(down)
    radio = radio_table(flight)
    if radio is not None:
        table, end = radio
        column = "TxBuf" if end == "aircraft" else "txbuf"
        if column in table.column_names:
            message = "RAD" if end == "aircraft" else "RADIO_STATUS"
            below = float(spec["txbuf"]["below"])
            out.append(Series(
                "txbuf", f"the radio's buffer under {below:g}% free", _ints(table, "TimeUS"),
                _floats(table, column) < below, (message,), (column,),
            ))  # fmt: skip
    return out


def _downlink(
    flight: FlightData, reg: Registry, rules: Mapping[str, Any], spec: Mapping[str, Any]
) -> Series | None:
    """Packets from the vehicle the station never received, over the window, on the
    vehicle's clock. With several stations, each sample is one station's view."""
    window = round(float(rules["window_s"]) * 1e6)
    parts: list[tuple[Array, Array]] = []
    for session, packets in flight.tlog_packets():
        mapped = np.asarray(packets.column("TimeUS").to_numpy(zero_copy_only=False), np.float64)
        mine = (
            (_ints(packets, "src_sysid") == session["sysid"])
            & (_ints(packets, "src_compid") == session["compid"])
            & np.isfinite(mapped)
        )  # fmt: skip
        if np.count_nonzero(mine) < 2:
            continue
        counter, lost = SeqCounter(), list[int]()
        for seq in _ints(packets, "src_seq")[mine]:
            before = counter.lost
            counter.add(int(seq))
            lost.append(counter.lost - before)
        t = mapped[mine].astype(np.int64)
        order = np.argsort(t, kind="stable")
        t, missed = t[order], np.asarray(lost, np.int64)[order]
        ones = np.ones(len(t), np.int64)
        loss = windowed_loss(t, ones, missed, window, int(rules["window_packets"]))
        parts.append((t, loss))
    if not parts:
        return None
    t = np.concatenate([p[0] for p in parts])
    loss = np.concatenate([p[1] for p in parts])
    order = np.argsort(t, kind="stable")
    metric = reg.metrics[spec["downlink_loss"]["metric"]]
    return Series(
        "downlink_loss", "packets lost on the way down", t[order],
        off_target(metric, loss[order]), (), (),
    )  # fmt: skip


# --- the battery ------------------------------------------------------------------------


@dataclass(frozen=True)
class Monitor:
    """One battery monitor's failsafe series while armed, and their peaks."""

    prefix: str
    times: Array
    low_share: Array | None  # the spell under BATT_LOW_VOLT, over BATT_LOW_TIMER
    low_what: str
    headroom_pct: Array | None  # VoltR (else Volt) above BATT_LOW_VOLT, in %
    headroom_field: str
    charge_share: Array | None  # charge used over BATT_CAPACITY less BATT_LOW_MAH
    budget_mah: float
    field: str


def _monitors(flight: FlightData) -> list[Monitor]:
    bat = flight.table("BAT")
    if bat is None or bat.num_rows == 0 or "Inst" not in bat.column_names:
        return []
    out: list[Monitor] = []
    for inst in np.unique(_ints(bat, "Inst")):
        if not 0 <= inst < len(MONITORS):
            continue
        prefix = MONITORS[int(inst)]
        if _param(flight, f"{prefix}_MONITOR") == 0:
            continue
        rows = _ints(bat, "Inst") == inst
        times = _ints(bat, "TimeUS")[rows]
        armed = (times >= flight.arm_us) & (times <= flight.disarm_us)
        times = times[armed]
        limit = _param(flight, f"{prefix}_LOW_VOLT")
        timer = _param(flight, f"{prefix}_LOW_TIMER", 10.0)
        field = "VoltR" if _param(flight, f"{prefix}_FS_VOLTSRC") == 1 else "Volt"
        low = headroom = charge = None
        low_what, head_field = "", ""
        if limit > 0 and field in bat.column_names:
            volts = _floats(bat, field)[rows][armed]
            if timer > 0:
                spell = run_lengths(times, (volts > 0) & (volts < limit))
                low = spell / 1e6 / timer
                low_what = f"BAT.{field} under {prefix}_LOW_VOLT {limit:g} V"
            head_field = "VoltR" if "VoltR" in bat.column_names else field
            rested = _floats(bat, head_field)[rows][armed]
            headroom = np.where(rested > 0, 100.0 * (rested - limit) / limit, np.nan)
        capacity, reserve = (
            _param(flight, f"{prefix}_CAPACITY"),
            _param(flight, f"{prefix}_LOW_MAH"),
        )
        budget = capacity - reserve
        if capacity > 0 and reserve > 0 and budget > 0 and "CurrTot" in bat.column_names:
            charge = _floats(bat, "CurrTot")[rows][armed] / budget
        if len(times):
            out.append(Monitor(prefix, times, low, low_what, headroom, head_field, charge,
                               budget, field))  # fmt: skip
    return out


def _battery(monitors: Sequence[Monitor], reg: Registry, rules: Mapping[str, Any]) -> list[Series]:
    spec: Mapping[str, Any] = rules["precursors"]["FAILSAFE_BATT"]
    out: list[Series] = []
    for m in monitors:
        if m.low_share is not None:
            metric = reg.metrics[spec["low_voltage"]["metric"]]
            out.append(Series(
                "low_voltage", m.low_what, m.times, off_target(metric, m.low_share),
                ("BAT",), (m.field,),
            ))  # fmt: skip
        if m.headroom_pct is not None:
            below = float(spec["headroom"]["below_pct"])
            out.append(Series(
                "headroom", f"BAT.{m.headroom_field} within {below:g}% of {m.prefix}_LOW_VOLT",
                m.times, np.nan_to_num(m.headroom_pct, nan=np.inf) <= below,
                ("BAT",), (m.headroom_field,),
            ))  # fmt: skip
        if m.charge_share is not None:
            metric = reg.metrics[spec["charge"]["metric"]]
            out.append(Series(
                "charge", f"the charge used of {m.budget_mah:g} mAh to {m.prefix}_LOW_MAH",
                m.times, off_target(metric, m.charge_share), ("BAT",), ("CurrTot",),
            ))  # fmt: skip
    return out


def _near_misses(
    flight: FlightData, monitors: Sequence[Monitor], reg: Registry, fired: bool, out: Result
) -> None:
    """The closest the battery failsafe came, over every monitor: a near miss when no
    battery failsafe fired. One that would have (a share of 1 or more) is the failsafe
    analysis's to judge as missed."""
    for name in ("failsafe.battery_low_run", "failsafe.battery_charge"):
        best: tuple[float, Monitor, int] | None = None
        for m in monitors:
            values = m.low_share if name == "failsafe.battery_low_run" else m.charge_share
            if values is None or not len(values):
                continue
            k = int(np.argmax(values))
            if best is None or float(values[k]) > best[0]:
                best = (float(values[k]), m, k)
        if best is None:
            continue
        share, m, k = best
        at_s = round((int(m.times[k]) - flight.arm_us) / 1e6, 1)
        if name == "failsafe.battery_low_run":
            timer = _param(flight, f"{m.prefix}_LOW_TIMER", 10.0)
            spell = round(share * timer, 2)
            at_s = round(at_s - spell, 1)
            what = f"{m.low_what} for {spell} s"
            left = f"{round(timer - spell, 2):g} s of {m.prefix}_LOW_TIMER {timer:g}"
            facts: dict[str, Any] = {
                "spell_s": spell, "timer_s": timer, "share": round(share, 3), "at_s": at_s,
            }  # fmt: skip
            fields: tuple[str, ...] = (m.field,)
        else:
            used = round(share * m.budget_mah)
            what = f"{used} mAh used of {m.budget_mah:g}"
            left = f"{round(m.budget_mah - used):g} mAh"
            facts = {
                "used_mah": used, "budget_mah": m.budget_mah, "share": round(share, 3),
                "at_s": at_s,
            }  # fmt: skip
            fields = ("CurrTot",)
        ev = Evidence(
            id=f"precursor:{name.split('.')[1]}",
            kind="fact",
            summary=f"{m.prefix}: {what}, at {at_s} s after arming",
            messages=("BAT",),
            fields=fields,
            segment=flight.armed,
            values=facts,
        )
        out.evidence.append(ev)
        metric = MetricValue(name, "flight", round(share, 3), reg.metrics[name].zone(share), ev.id)
        out.metrics.append(metric)
        if metric.zone == "caution" and not fired:
            out.findings.append(Finding(
                "failsafe.battery_near_miss", "caution", "high", (ev,),
                {"left": left, "at_s": at_s, "what": what},
            ))  # fmt: skip


# --- the radio --------------------------------------------------------------------------


def _radio(flight: FlightData) -> list[Series]:
    if int(_param(flight, "RC_OPTIONS")) & (IGNORE_RECEIVER | IGNORE_RECEIVER_FAILSAFE):
        return []
    rci2 = flight.table("RCI2")
    if rci2 is None or rci2.num_rows == 0 or "Flags" not in rci2.column_names:
        return []
    flagged = (_ints(rci2, "Flags") & PROTOCOL_FAILSAFE) > 0
    return [Series(
        "receiver_flag", "the receiver's own failsafe flag", _ints(rci2, "TimeUS"), flagged,
        ("RCI2",), ("Flags",),
    )]  # fmt: skip


# --- lead time --------------------------------------------------------------------------


def _lead(
    flight: FlightData, o: Occurrence, series: Sequence[Series], name: str,
    lookback_us: int, out: Result,
) -> None:  # fmt: skip
    """For one failsafe, the first time each of its precursors warned within the
    look-back window (or since arming), as seconds before it fired."""
    if not series:
        return
    since = max(o.start_us - lookback_us, flight.arm_us)
    leads: dict[str, float] = {}
    words: dict[str, str] = {}
    for s in series:
        hit = s.warned & (s.times >= since) & (s.times <= o.start_us)
        if np.any(hit):
            lead = round((o.start_us - int(s.times[hit][0])) / 1e6, 1)
            if s.id not in leads or lead > leads[s.id]:
                leads[s.id], words[s.id] = lead, s.what
    watched = sorted({s.id for s in series})
    at_s = round((o.start_us - flight.arm_us) / 1e6, 1)
    summary = f"{name} failsafe at {at_s} s: " + (
        ", ".join(f"{words[i]} {leads[i]} s before" for i in sorted(leads, key=lambda i: -leads[i]))
        if leads
        else "no precursor warned"
    )
    ev = Evidence(
        id=f"precursor:lead:{o.kind.removeprefix('FAILSAFE_').lower()}:{at_s}",
        kind="fact",
        summary=summary,
        messages=tuple(sorted({m for s in series for m in s.messages})),
        fields=tuple(sorted({f for s in series for f in s.fields})),
        segment=flight.armed,
        values={"at_s": at_s, "watched": len(watched), "warned": len(leads)}
        | {f"{i}_lead_s": leads.get(i) for i in watched},
    )
    out.evidence.append(ev)
    if leads:
        first = max(leads, key=lambda i: leads[i])
        out.findings.append(Finding("failsafe.lead_time", "info", "high", (ev,), {
            "failsafe": name, "at_s": at_s, "first": words[first], "lead_s": leads[first],
            "warned": len(leads), "watched": len(watched),
        }))  # fmt: skip
    else:
        out.findings.append(Finding("failsafe.unwarned", "info", "high", (ev,), {
            "failsafe": name, "at_s": at_s, "watched": len(watched),
        }))  # fmt: skip


def _precursors(flight: FlightData, reg: Registry) -> Result:
    if not flight.logged:
        return Result(skipped="a parameter file has no flight to judge")
    if flight.vehicle != "copter":
        return Result(skipped="the failsafes are modelled for Copter")
    rules: Mapping[str, Any] = reg.rules["precursors"]
    failsafes: Mapping[str, Any] = reg.rules["failsafes"]["failsafes"]
    names = reg.names(flight.firmware)
    monitors = _monitors(flight)
    series = {
        "FAILSAFE_GCS": _gcs(flight, reg, rules),
        "FAILSAFE_BATT": _battery(monitors, reg, rules),
        "FAILSAFE_RADIO": _radio(flight),
    }
    fired = [
        o for o in occurrences(flight, names, tuple(series))
        if flight.arm_us <= o.start_us <= flight.disarm_us
    ]  # fmt: skip
    out = Result()
    battery_fired = any(o.kind == "FAILSAFE_BATT" for o in fired)
    _near_misses(flight, monitors, reg, battery_fired, out)
    lookback = round(float(rules["lookback_s"]) * 1e6)
    for o in fired:
        _lead(flight, o, series[o.kind], failsafes[o.kind]["name"]["en"], lookback, out)
    return out


PRECURSORS = Analyzer(
    name="precursors",
    version=1,
    run=_precursors,
    metrics=("failsafe.battery_low_run", "failsafe.battery_charge"),
    findings=("failsafe.battery_near_miss", "failsafe.lead_time", "failsafe.unwarned"),
)
