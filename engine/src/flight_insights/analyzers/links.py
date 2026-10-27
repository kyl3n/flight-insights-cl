"""Telemetry and RC links, from what the autopilot and the ground station recorded (spec:
Links, ground station and whole-system faults: Telemetry link, RC link).

- **Downlink** (tlog sessions): packets from the vehicle the station never received,
  from MAVLink sequence numbers in recorded order, and the longest time the station
  heard nothing from it, while armed.
- **Uplink** (MAV): packets the autopilot knows it missed on each link that carried the
  GCS (rxdp against rxp, cumulative 16-bit counts), and the longest time it did not hear
  the GCS (mgs) against FS_GCS_TIMEOUT.
- **Radio status** (RAD, logged as each RADIO_STATUS arrives, or RADIO_STATUS in the
  tlog): the share of time the radio's buffer was under half free, which slows every
  stream; fade margin and the RSSI gap between the ends only for SiK firmware radios,
  the only ones whose units are known (rules/links.json).
- **RC** (RSSI, RCIN, RCI2): receiver RSSI and link quality against the airframe's own
  flights, brief receiver failsafes, and FS_THR_VALUE against the lowest throttle the
  receiver sent with valid input.

The radios' own logs, and link quality against distance from the station, come later.
Positions are never read.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.failsafe_onset import last_seen
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import (
    SEVERITY_OF_ZONE,
    Analyzer,
    Evidence,
    Finding,
    MetricValue,
    Result,
    Segment,
)
from flight_insights.analyzers.registry import Registry
from flight_insights.tlog.decode import SeqCounter

Array = np.ndarray[Any, Any]
VALID, IN_FAILSAFE, PROTOCOL_FAILSAFE = 1 << 0, 1 << 1, 1 << 2  # RCI2.Flags
IGNORE_RECEIVER = 1 << 0  # RC_OPTIONS
COUNTER = 1 << 16  # MAV's packet counts are 16-bit
MIN_PACKETS = 50  # fewer, and a loss share says little


def _ints(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.int64)


def _floats(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.float64)


def _param(flight: FlightData, name: str, fallback: float = 0.0) -> float:
    return float(flight.params.get(name, flight.defaults.get(name, fallback)))


def _armed(flight: FlightData, times: Array) -> Array:
    return (times >= flight.arm_us) & (times <= flight.disarm_us)


def _metric(reg: Registry, out: Result, metric: str, value: float, ev: Evidence) -> MetricValue:
    m = MetricValue(metric, "flight", value, reg.metrics[metric].zone(value), ev.id)
    out.metrics.append(m)
    return m


def _flag(out: Result, m: MetricValue, code: str, ev: Evidence, params: dict[str, Any]) -> None:
    """A finding when the metric left its target zone."""
    if m.zone in ("caution", "out"):
        out.findings.append(Finding(code, SEVERITY_OF_ZONE[m.zone], "high", (ev,), params))


# --- the downlink, as the ground station heard it ---------------------------------------


def _downlink(flight: FlightData, reg: Registry, rules: Mapping[str, Any], out: Result) -> bool:
    """The worst station's view: the share of the vehicle's packets it never received,
    and the longest time it heard nothing from the vehicle, while armed."""
    views: list[dict[str, Any]] = []
    for n, (session, packets) in enumerate(flight.tlog_packets(), 1):
        mapped = np.asarray(packets.column("TimeUS").to_numpy(zero_copy_only=False), np.float64)
        mine = (
            (_ints(packets, "src_sysid") == session["sysid"])
            & (_ints(packets, "src_compid") == session["compid"])
            & np.isfinite(mapped) & (mapped >= flight.arm_us) & (mapped <= flight.disarm_us)
        )  # fmt: skip
        if np.count_nonzero(mine) < 2:
            continue
        counter = SeqCounter()
        for seq in _ints(packets, "src_seq")[mine]:
            counter.add(int(seq))
        times = np.sort(mapped[mine])
        gaps = np.diff(times) / 1e6
        k = int(np.argmax(gaps))
        received = int(np.count_nonzero(mine))
        views.append({
            "station": n,
            "received": received,
            "lost": counter.lost,
            "out_of_order": counter.out_of_order,
            "loss_pct": round(100.0 * counter.lost / (received + counter.lost), 2),
            "longest_s": round(float(gaps[k]), 2),
            "longest_at_s": round((float(times[k]) - flight.arm_us) / 1e6, 1),
            "outages": int(np.count_nonzero(gaps > float(rules["outage_s"]))),
        })  # fmt: skip
    if not views:
        return False
    worst = max(views, key=lambda v: (v["loss_pct"], v["longest_s"]))
    ev = Evidence(
        id="link:downlink",
        kind="fact",
        summary=(
            f"tlog: {worst['lost']} of {worst['received'] + worst['lost']} vehicle packets"
            f" lost ({worst['loss_pct']}%), longest silence {worst['longest_s']} s"
            + (f", over {len(views)} stations" if len(views) > 1 else "")
        ),
        segment=flight.armed,  # read from the packets' headers: sender and sequence
        values={
            "loss_pct": worst["loss_pct"], "lost": worst["lost"],
            "received": worst["received"], "out_of_order": worst["out_of_order"],
            "longest_s": worst["longest_s"], "longest_at_s": worst["longest_at_s"],
            "outages": worst["outages"], "stations": len(views),
        },
    )  # fmt: skip
    out.evidence.append(ev)
    loss = _metric(reg, out, "link.downlink_loss", worst["loss_pct"], ev)
    if worst["received"] + worst["lost"] >= MIN_PACKETS:
        params = {"loss": worst["loss_pct"], "lost": worst["lost"], "received": worst["received"]}
        _flag(out, loss, "link.downlink_loss", ev, params)
    gap = _metric(reg, out, "link.downlink_outage", worst["longest_s"], ev)
    _flag(out, gap, "link.downlink_outage", ev, {
        "seconds": worst["longest_s"], "at_s": worst["longest_at_s"],
        "outages": worst["outages"], "over_s": rules["outage_s"],
    })  # fmt: skip
    return True


# --- the uplink, as the autopilot heard the ground station -------------------------------


def _delta(counts: Array) -> int:
    """How far a 16-bit cumulative count advanced, across its wraps."""
    return int(np.sum(np.diff(counts) % COUNTER)) if len(counts) > 1 else 0


def _uplink(flight: FlightData, reg: Registry, out: Result) -> bool:
    mav = flight.table("MAV")
    if mav is None or mav.num_rows == 0 or "rxdp" not in mav.column_names:
        return False
    times = _ints(mav, "TimeUS")
    order = np.argsort(times, kind="stable")
    times, chan = times[order], _ints(mav, "chan")[order]
    rxp, rxdp = _ints(mav, "rxp")[order], _ints(mav, "rxdp")[order]
    has_mgs = "mgs" in mav.column_names
    mgs = _ints(mav, "mgs")[order] if has_mgs else np.zeros(len(times), np.int64)
    armed = _armed(flight, times)
    links: list[dict[str, Any]] = []
    for c in np.unique(chan[armed]):
        rows = armed & (chan == c)
        got, missed = _delta(rxp[rows]), _delta(rxdp[rows])
        heard_gcs = bool(np.any(np.diff(mgs[rows]) > 0)) if has_mgs else got > 0
        if not heard_gcs or got + missed < MIN_PACKETS:
            continue
        links.append({"chan": int(c), "received": got, "missed": missed,
                      "loss_pct": round(100.0 * missed / (got + missed), 2)})  # fmt: skip
    found = False
    if links:
        worst = max(links, key=lambda v: v["loss_pct"])
        ev = Evidence(
            id="link:uplink",
            kind="fact",
            summary=(
                f"MAV link {worst['chan']}: {worst['missed']} of"
                f" {worst['received'] + worst['missed']} packets from the ground missed"
                f" ({worst['loss_pct']}%)"
            ),
            messages=("MAV",),
            fields=("chan", "rxp", "rxdp", "mgs") if has_mgs else ("chan", "rxp", "rxdp"),
            segment=flight.armed,
            values={
                "link": worst["chan"],
                "received": worst["received"],
                "missed": worst["missed"],
                "loss_pct": worst["loss_pct"],
                "links": len(links),
            },
        )
        out.evidence.append(ev)
        m = _metric(reg, out, "link.uplink_loss", worst["loss_pct"], ev)
        _flag(out, m, "link.uplink_loss", ev, {"loss": worst["loss_pct"], "link": worst["chan"]})
        found = True
    if has_mgs:
        found |= _gcs_silence(flight, reg, times, chan, mgs, out)
    return found


def _gcs_silence(
    flight: FlightData, reg: Registry, times: Array, chan: Array, mgs: Array, out: Result
) -> bool:
    """The longest the GCS went unheard while armed, against the wait of its failsafe. At
    the wait or past it the failsafe analyzer judges the loss; short of it, a near miss."""
    seen = last_seen(times, chan, mgs)
    armed = _armed(flight, times) & (seen > 0)
    if not np.any(armed):
        return False
    silence = (times - seen)[armed] / 1e6
    k = int(np.argmax(silence))
    timeout = _param(flight, "FS_GCS_TIMEOUT", 5.0)
    longest = round(float(silence[k]), 2)
    at_s = round((int(times[armed][k]) - flight.arm_us) / 1e6 - longest, 1)
    ev = Evidence(
        id="link:gcs_silence",
        kind="fact",
        summary=(
            f"MAV.mgs: the ground station unheard for up to {longest} s"
            f" (FS_GCS_TIMEOUT {timeout:g})"
        ),
        messages=("MAV",),
        fields=("mgs",),
        segment=flight.armed,
        values={"longest_s": longest, "from_s": at_s, "timeout_s": timeout},
    )
    out.evidence.append(ev)
    share = round(longest / timeout, 3) if timeout > 0 else float("nan")
    m = _metric(reg, out, "link.gcs_silence_share", share, ev)
    if m.zone == "caution":  # out is a loss: the failsafe analyzer's to judge
        out.findings.append(Finding("link.gcs_silence", "caution", "high", (ev,), {
            "seconds": longest, "at_s": at_s, "timeout": f"{timeout:g}",
        }))  # fmt: skip
    return True


# --- the radios' own status -------------------------------------------------------------


def radio_table(flight: FlightData) -> tuple[pa.Table, str] | None:
    """RADIO_STATUS as the aircraft's radio sent it (RAD), else as the ground radio sent
    it to the station (tlog); with which end reported, and whether it is a SiK radio."""
    rad = flight.table("RAD")
    if rad is not None and rad.num_rows:
        return rad, "aircraft"
    for _, table in flight.tlog("RADIO_STATUS"):
        return table, "ground"
    return None


def _is_sik(flight: FlightData, rules: Mapping[str, Any]) -> bool:
    sik: Mapping[str, Any] = rules["sik"]
    if flight.answers.get("telemetry_radio") in sik["answers"]:
        return True
    sysid, compid = sik["sender"]
    for _, table in flight.tlog("RADIO_STATUS", []):
        if np.any((_ints(table, "src_sysid") == sysid) & (_ints(table, "src_compid") == compid)):
            return True
    return False


def _radio_status(flight: FlightData, reg: Registry, rules: Mapping[str, Any], out: Result) -> bool:
    found = radio_table(flight)
    if found is None:
        return False
    table, end = found
    onboard = end == "aircraft"
    names = (
        ("RSSI", "RemRSSI", "TxBuf", "Noise", "RemNoise") if onboard
        else ("rssi", "remrssi", "txbuf", "noise", "remnoise")
    )  # fmt: skip
    if any(n not in table.column_names for n in names):
        return False
    times = _ints(table, "TimeUS")
    armed = _armed(flight, times)
    if not np.any(armed):
        return False
    rssi, rem, txbuf, noise, remnoise = (_floats(table, n)[armed] for n in names)
    message = "RAD" if onboard else "RADIO_STATUS"
    slows = float(rules["txbuf"]["slows_streams"])
    low_share = round(100.0 * float(np.mean(txbuf < slows)), 1)
    ev = Evidence(
        id="link:radio",
        kind="fact",
        summary=(
            f"{message} from the {end}'s radio: TxBuf under {slows:g}%"
            f" for {low_share}% of the flight"
        ),
        messages=(message,),
        fields=(names[2],),
        segment=flight.armed,
        values={
            "reported_by": end,
            "txbuf_low_pct": low_share,
            "txbuf_min": float(np.min(txbuf)),
            "samples": int(np.count_nonzero(armed)),
        },
    )
    out.evidence.append(ev)
    m = _metric(reg, out, "link.txbuf_low", low_share, ev)
    _flag(out, m, "link.txbuf_low", ev, {"share": low_share, "below": f"{slows:g}"})
    if _is_sik(flight, rules):
        _sik_margins(flight, reg, rules, end, (rssi, rem, noise, remnoise), names, message, out)
    return True


def _sik_margins(
    flight: FlightData, reg: Registry, rules: Mapping[str, Any], end: str,
    values: tuple[Array, Array, Array, Array], names: tuple[str, ...], message: str,
    out: Result,
) -> None:  # fmt: skip
    """Fade margin at each end (its RSSI over its noise, 1.9 units a dB) and the gap
    between the two ends' RSSI. A remote RSSI of 0 means that end heard nothing yet."""
    sik: Mapping[str, Any] = rules["sik"]
    per_db = float(sik["db_per_unit"])
    rssi, rem, noise, remnoise = values
    here, there = ("aircraft", "ground") if end == "aircraft" else ("ground", "aircraft")
    heard = rem > 0
    margins = {here: (rssi - noise) / per_db, there: ((rem - remnoise) / per_db)[heard]}
    p5 = {side: round(float(np.percentile(v, 5)), 1) for side, v in margins.items() if len(v)}
    if not p5:
        return
    weak = min(p5, key=lambda s: p5[s])
    doubling = float(sik["range_doubling_db"])
    ev = Evidence(
        id="link:fade_margin",
        kind="fact",
        summary=(
            "SiK fade margin (5th percentile): " + ", ".join(f"{s} {v} dB" for s, v in p5.items())
        ),
        messages=(message,),
        fields=names[:2] + names[3:],
        segment=flight.armed,
        values={f"{side}_db": v for side, v in p5.items()}
        | {"unheard_pct": round(100.0 * float(np.mean(~heard)), 1)},
    )
    out.evidence.append(ev)
    m = _metric(reg, out, "link.fade_margin", p5[weak], ev)
    _flag(out, m, "link.fade_margin", ev, {
        "margin": p5[weak], "side": weak,
        "range_factor": round(2 ** (p5[weak] / doubling), 1),
    })  # fmt: skip
    both = heard & (rssi > 0)
    if np.count_nonzero(both) < 10:
        return
    gap = (rssi[both] - rem[both]) / per_db
    median = round(float(np.median(np.abs(gap))), 1)
    weaker = here if float(np.median(gap)) < 0 else there
    ev2 = Evidence(
        id="link:asymmetry",
        kind="fact",
        summary=f"SiK RSSI: the {weaker} end receives {median} dB less (median)",
        messages=(message,),
        fields=names[:2],
        segment=flight.armed,
        values={"gap_db": median, "weaker": weaker},
    )
    out.evidence.append(ev2)
    m2 = _metric(reg, out, "link.rssi_asymmetry", median, ev2)
    _flag(out, m2, "link.rssi_asymmetry", ev2, {"gap": median, "weaker": weaker})


# --- the RC link ------------------------------------------------------------------------


def _rc(flight: FlightData, reg: Registry, rules: Mapping[str, Any], out: Result) -> bool:
    if int(_param(flight, "RC_OPTIONS")) & IGNORE_RECEIVER:
        return False  # the receiver is ignored: the ground station flies it
    found = _rc_signal(flight, reg, out)
    found |= _rc_failsafes(flight, reg, out)
    found |= _fs_thr_margin(flight, reg, rules, out)
    return found


def _rc_signal(flight: FlightData, reg: Registry, out: Result) -> bool:
    """Receiver RSSI (0 to 1) and link quality (percent) at their 5th percentile while
    armed, against the airframe's own flights."""
    rssi = flight.table("RSSI")
    if rssi is None or rssi.num_rows == 0:
        return False
    armed = _armed(flight, _ints(rssi, "TimeUS"))
    found = False
    for field, metric, scale in (("RXRSSI", "link.rc_rssi", 100.0), ("RXLQ", "link.rc_lq", 1.0)):
        if field not in rssi.column_names:
            continue
        v = _floats(rssi, field)[armed] * scale
        v = v[v > 0]  # nothing reported, or a receiver that does not report it
        if not len(v):
            continue
        p5 = round(float(np.percentile(v, 5)), 1)
        ev = Evidence(
            id=f"link:{metric.split('.')[1]}",
            kind="metric",
            summary=(
                f"RSSI.{field} while armed: 5th percentile {p5}%,"
                f" lowest {round(float(v.min()), 1)}%"
            ),
            messages=("RSSI",),
            fields=(field,),
            segment=flight.armed,
            values={"p5_pct": p5, "min_pct": round(float(v.min()), 1)},
        )
        out.evidence.append(ev)
        _metric(reg, out, metric, p5, ev)
        found = True
    return found


def _rci2(flight: FlightData) -> tuple[Array, Array, Array] | None:
    rci2 = flight.table("RCI2")
    if rci2 is None or rci2.num_rows == 0 or "Flags" not in rci2.column_names:
        return None
    omask = (
        _ints(rci2, "OMask") if "OMask" in rci2.column_names else np.zeros(rci2.num_rows, np.int64)
    )
    return _ints(rci2, "TimeUS"), _ints(rci2, "Flags"), omask


def _rc_failsafes(flight: FlightData, reg: Registry, out: Result) -> bool:
    """Each time the receiver itself flagged failsafe while armed (RCI2.Flags bit 2):
    the transmitter lost, however briefly."""
    rci2 = _rci2(flight)
    if rci2 is None:
        return False
    times, flags, _ = rci2
    armed = _armed(flight, times)
    flagged = armed & ((flags & PROTOCOL_FAILSAFE) > 0)
    edges = np.flatnonzero(np.diff(np.concatenate(([0], flagged.astype(np.int8), [0]))))
    runs = list(zip(edges[::2], edges[1::2] - 1, strict=True))
    longest = max(((int(times[b]) - int(times[a])) / 1e6 for a, b in runs), default=0.0)
    ev = Evidence(
        id="link:rc_failsafes",
        kind="fact",
        summary=f"RCI2: the receiver flagged failsafe {len(runs)} times while armed",
        messages=("RCI2",),
        fields=("Flags",),
        segment=flight.armed,
        values={
            "count": len(runs),
            "longest_s": round(longest, 2),
            "first_s": None
            if not runs
            else round((int(times[runs[0][0]]) - flight.arm_us) / 1e6, 1),
        },
    )
    out.evidence.append(ev)
    m = _metric(reg, out, "link.rc_receiver_failsafes", float(len(runs)), ev)
    _flag(
        out,
        m,
        "link.rc_receiver_failsafes",
        ev,
        {"count": len(runs), "longest_s": round(longest, 2)},
    )
    return True


def _fs_thr_margin(
    flight: FlightData, reg: Registry, rules: Mapping[str, Any], out: Result
) -> bool:
    """FS_THR_VALUE against the lowest throttle the receiver sent with valid input, not in
    failsafe and not overridden over MAVLink, anywhere in the log: the stick's lowest."""
    if _param(flight, "FS_THR_ENABLE") == 0:
        return False
    rci2 = _rci2(flight)
    channel = int(_param(flight, "RCMAP_THROTTLE", 3))
    rcin = flight.table("RCIN")
    column = f"C{channel}"
    if rci2 is None or rcin is None or rcin.num_rows == 0 or column not in rcin.column_names:
        return False
    times, flags, omask = rci2
    rc_times, pwm = _ints(rcin, "TimeUS"), _ints(rcin, column)
    k = np.clip(np.searchsorted(times, rc_times, side="right") - 1, 0, None)
    good = (
        ((flags[k] & VALID) > 0) & ((flags[k] & (IN_FAILSAFE | PROTOCOL_FAILSAFE)) == 0)
        & (((omask[k] >> (channel - 1)) & 1) == 0) & (pwm > 0)
    )  # fmt: skip
    if not np.any(good):
        return False
    lowest = int(pwm[good].min())
    value = _param(flight, "FS_THR_VALUE", 975)
    margin = lowest - value
    ev = Evidence(
        id="link:fs_thr",
        kind="fact",
        summary=f"RCIN.{column}: lowest {lowest} with valid input, FS_THR_VALUE {value:g}",
        messages=("RCIN", "RCI2"),
        fields=(column, "Flags", "OMask"),
        segment=Segment("log", int(rc_times[good][0]), int(rc_times[good][-1])),
        values={"lowest_pwm": lowest, "fs_thr_value": value, "margin_pwm": margin},
    )
    out.evidence.append(ev)
    m = _metric(reg, out, "link.rc_fs_thr_margin", float(margin), ev)
    _flag(out, m, "link.rc_fs_thr_margin", ev, {
        "lowest": lowest, "value": f"{value:g}", "margin": f"{margin:g}",
        "needed": rules["rc_fs_thr_margin"],
    })  # fmt: skip
    return True


def _links(flight: FlightData, reg: Registry) -> Result:
    if not flight.logged:
        return Result(skipped="a parameter file has no flight to judge")
    rules: Mapping[str, Any] = reg.rules["links"]
    out = Result()
    seen = [
        _downlink(flight, reg, rules, out),
        _uplink(flight, reg, out),
        _radio_status(flight, reg, rules, out),
        _rc(flight, reg, rules, out),
    ]
    if not any(seen):
        out.skipped = "no tlog, MAV, RAD, RSSI or RC input to judge the links by"
    return out


LINKS = Analyzer(
    name="links",
    version=1,
    run=_links,
    metrics=(
        "link.downlink_loss", "link.downlink_outage", "link.uplink_loss",
        "link.gcs_silence_share", "link.txbuf_low", "link.fade_margin",
        "link.rssi_asymmetry", "link.rc_rssi", "link.rc_lq",
        "link.rc_receiver_failsafes", "link.rc_fs_thr_margin",
    ),
    findings=(
        "link.downlink_loss", "link.downlink_outage", "link.uplink_loss", "link.gcs_silence",
        "link.txbuf_low", "link.fade_margin", "link.rssi_asymmetry",
        "link.rc_receiver_failsafes", "link.rc_fs_thr_margin",
    ),
)  # fmt: skip
