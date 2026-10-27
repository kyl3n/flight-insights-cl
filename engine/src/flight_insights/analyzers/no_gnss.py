"""Flying without GNSS: every outage in a flight, measured (spec: Flying without GNSS,
"Performance from the logs"; rules in `knowledge/rules/no_gnss.json`).

An outage is a stretch of at least `min_outage_s` with no receiver holding a 3D fix
(gaps under `join_s` closed). It is a drill when the GPS-disable switch (aux function 65)
turned GNSS off at its start. For each, from the primary estimator core's own solution
status (XKF4.SS):
- detection: how long after the loss the estimator stopped using GNSS;
- hold: how long it kept an absolute position, or that it kept one all along;
- whether it kept a relative position (optical flow or vision) throughout;
- how long after GNSS returned it used it again;
- the source set in use (aux function 90), the failsafes that triggered, and the first mode
  change within `response_s`, with its reason;
- drift: when GNSS returns, how far the estimate is from it, compared as local tracks
  from one origin, and per minute of outage for outages of `drift.min_outage_s` or more;
  only when GNSS comes back trustworthy (its fix held, its accuracy good, no drift
  faster than an aircraft could move).

The flight's range is measured too, whatever its outages: the farthest it flew from
where it armed, from the estimator's track (local_track), and how long it was armed, so
the recovery card can judge whether a way home on dead reckoning fits FS_DR_TIMEOUT.

Drift and range are position-derived. They stay in evidence that cites the position
fields, so the location policy withholds them from the AI path; the rest is times, flags
and words.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.gnss import Receiver, receivers, spans, switched_off
from flight_insights.analyzers.model import (
    Analyzer,
    Evidence,
    Finding,
    MetricValue,
    Result,
    Segment,
)
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si

Array = np.ndarray[Any, Any]


def _ints(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.int64)


@dataclass(frozen=True)
class Outage:
    start_us: int
    end_us: int  # GNSS back (the first fixed sample), or disarming
    drill: bool  # GNSS switched off with the GPS-disable switch


def source_set_at(flight: FlightData, function: int, at_us: int) -> int | None:
    """The source set the switch selected last before `at_us` (1, 2 or 3), if it was used."""
    auxf = flight.table("AUXF", ["TimeUS", "function", "pos"])
    if auxf is None or auxf.num_rows == 0 or "function" not in auxf.column_names:
        return None
    t, fn, pos = _ints(auxf, "TimeUS"), _ints(auxf, "function"), _ints(auxf, "pos")
    rows = np.flatnonzero((fn == function) & (t <= at_us))
    return int(pos[rows[np.argmax(t[rows])]]) + 1 if len(rows) else None


def outages(flight: FlightData, rules: Mapping[str, Any], found: Mapping[int, Receiver]
            ) -> list[Outage]:  # fmt: skip
    """Stretches with no receiver holding a fix, after the first one had, while armed."""
    fixed_rx = [r for r in found.values() if r.first_fix_us is not None]
    if not fixed_rx:
        return []
    first = min(fixed_rx, key=lambda r: r.instance)
    t = first.t
    min_fix = 3
    any_fix = first.status >= min_fix
    for r in fixed_rx:
        if r is first:
            continue
        k = np.clip(np.searchsorted(r.t, t, side="right") - 1, 0, len(r.t) - 1)
        any_fix = any_fix | ((r.status[k] >= min_fix) & (r.t[k] <= t))
    lost = (t >= min(r.first_fix_us or 0 for r in fixed_rx)) & ~any_fix
    joined: list[tuple[int, int]] = []
    for a, b in spans(t, lost):
        if joined and t[a] - joined[-1][1] <= float(rules["join_s"]) * 1e6:
            joined[-1] = (joined[-1][0], int(t[b]))
        else:
            joined.append((int(t[a]), int(t[b])))
    off = switched_off(flight, int(rules["aux"]["gps_disable"]))
    out: list[Outage] = []
    for a, b in joined:
        a, b = max(a, flight.arm_us), min(b, flight.disarm_us)
        if b - a < float(rules["min_outage_s"]) * 1e6:
            continue
        drill = any(s - 1_000_000 <= a <= e for s, e in off)
        out.append(Outage(a, b, drill))
    return out


# --- measuring an outage -----------------------------------------------------------------


@dataclass(frozen=True)
class Measured:
    using_at_start: bool  # the estimator was using GNSS when it went
    detection_s: float | None  # until it stopped using GNSS; None if it never did
    duration_s: float
    hold_s: float  # how long it kept an absolute position (the whole outage if it kept it)
    held: bool  # kept an absolute position all along
    kept_relative: bool  # kept a relative position all along (flow or vision)
    resumed_s: float | None  # after GNSS returned, until it was used again
    source_set: int | None
    failsafes: tuple[str, ...]
    mode: str | None
    reason: str | None
    drift_m: float | None  # the estimate against GNSS when it returned
    rate_m_min: float | None

    @property
    def kept_s(self) -> float:
        """How long it kept a position it could fly on: absolute, or relative from optical
        flow or vision; the whole outage when it kept one throughout."""
        return self.duration_s if self.held or self.kept_relative else self.hold_s

    def words(self) -> str:
        out: list[str] = []
        if not self.using_at_start:
            on_set = f" (source set {self.source_set})" if self.source_set else ""
            out.append(f"the estimator was not using GNSS when it went{on_set}")
        elif self.detection_s is not None:
            out.append(f"the estimator stopped using GNSS after {self.detection_s:g} s")
        if self.held:
            out.append("it kept its position throughout")
        elif self.kept_relative:
            out.append(f"it kept a relative position throughout (absolute for {self.hold_s:g} s)")
        else:
            out.append(f"it gave up its position after {self.hold_s:g} s")
        if self.failsafes:
            out.append(f"{_plain(', '.join(self.failsafes))} triggered")
        if self.mode is not None:
            out.append(f"the mode changed to {self.mode} by {_plain(self.reason or 'UNKNOWN')}")
        if self.resumed_s is not None:
            out.append(f"GNSS was in use again {self.resumed_s:g} s after it returned")
        return "; ".join(out)


def _plain(name: str) -> str:
    words = [f"{w.removeprefix('FAILSAFE_')} failsafe" if w.startswith("FAILSAFE_") else w
             for w in name.split(", ")]  # fmt: skip
    out = ", ".join(words).replace("EKFCHECK", "EKF check").replace("_", " ")
    return " ".join(w if w in ("GCS", "RC", "EKF", "EKFINAV", "GPS", "DEADRECKON") else w.lower()
                    for w in out.split(" "))  # fmt: skip


def solution(flight: FlightData) -> tuple[Array, Array] | None:
    """The primary core's solution status over time (XKF4.SS where C is PI)."""
    x4 = flight.table("XKF4", ["TimeUS", "C", "PI", "SS"])
    if x4 is None or x4.num_rows == 0 or "SS" not in x4.column_names:
        return None
    core = _ints(x4, "C")
    pi = _ints(x4, "PI") if "PI" in x4.column_names else np.zeros(len(core), np.int64)
    rows = core == pi
    return _ints(x4, "TimeUS")[rows], _ints(x4, "SS")[rows]


def _response(flight: FlightData, o: Outage, after_us: float, names: Mapping[str, Any]
              ) -> tuple[tuple[str, ...], str | None, str | None]:  # fmt: skip
    failsafes: list[str] = []
    err = flight.table("ERR", ["TimeUS", "Subsys", "ECode"])
    if err is not None and err.num_rows:
        subsystems: Mapping[str, str] = names.get("error_subsystems", {})
        t, sub, code = _ints(err, "TimeUS"), _ints(err, "Subsys"), _ints(err, "ECode")
        for k in np.argsort(t, kind="stable"):
            name = subsystems.get(str(int(sub[k])), "")
            failed = code[k] != 0 and name.startswith(("FAILSAFE_", "EKFCHECK"))
            if o.start_us <= t[k] <= after_us and failed and name not in failsafes:
                failsafes.append(name)
    mode = reason = None
    table = flight.table("MODE", ["TimeUS", "ModeNum", "Rsn"])
    if table is not None and table.num_rows and "Rsn" in table.column_names:
        t, num, rsn = _ints(table, "TimeUS"), _ints(table, "ModeNum"), _ints(table, "Rsn")
        for k in np.argsort(t, kind="stable"):
            if o.start_us <= t[k] <= after_us:
                modes: Mapping[str, str] = names.get("modes", {}).get("copter", {})
                mode = modes.get(str(int(num[k])), f"MODE_{int(num[k])}")
                reason = names.get("mode_reasons", {}).get(str(int(rsn[k])), "UNKNOWN")
                break
    return tuple(failsafes), mode, reason


def _drift(flight: FlightData, o: Outage, first: Receiver, rules: Mapping[str, Any]
           ) -> float | None:  # fmt: skip
    """GNSS's first fixed position after the outage against the estimate from before it
    returned, carried forward from its last two samples: the estimator takes GNSS back
    within a sample or two and its position jumps, so the error is read before that.
    Only when GNSS comes back trustworthy: its fix held for `stable_s`, its accuracy
    within `hacc_m`, and no drift faster than `max_mps` implied."""
    d = rules["drift"]
    tracks = flight.local_tracks()
    pos, gps = tracks.get("POS"), tracks.get(f"GPS{first.instance}")
    if pos is None or gps is None:
        return None
    t = gps.times_us
    k = np.clip(np.searchsorted(first.t, t), 0, len(first.t) - 1)
    back = np.flatnonzero((t >= o.end_us) & (first.t[k] == t) & (first.status[k] >= 3))
    pt = pos.times_us
    before = np.flatnonzero(pt < o.end_us)
    if not len(back) or len(before) < 2 or t[back[0]] - pt[before[-1]] > 1_000_000:
        return None
    since = (first.t >= t[back[0]]) & (first.t <= t[back[0]] + float(d["stable_s"]) * 1e6)
    if np.any(first.status[since] < 3) or first.t[-1] < t[back[0]] + float(d["stable_s"]) * 1e6:
        return None
    hacc = _accuracy(flight, first.instance, int(t[back[0]]))
    if hacc is not None and hacc > float(d["hacc_m"]):
        return None
    a, b = before[-2], before[-1]
    e, n = gps.east_north_m()
    pe, pn = pos.east_north_m()
    ahead = (t[back[0]] - pt[b]) / max(int(pt[b] - pt[a]), 1)
    est_e = pe[b] + (pe[b] - pe[a]) * ahead
    est_n = pn[b] + (pn[b] - pn[a]) * ahead
    drift = float(np.hypot(e[back[0]] - est_e, n[back[0]] - est_n))
    seconds = (o.end_us - o.start_us) / 1e6
    return drift if drift <= float(d["max_mps"]) * seconds else None


def _accuracy(flight: FlightData, instance: int, at_us: int) -> float | None:
    """The receiver's reported horizontal accuracy at its sample nearest `at_us`."""
    gpa = flight.table("GPA", ["TimeUS", "I", "HAcc"])
    if gpa is None or gpa.num_rows == 0 or "HAcc" not in gpa.column_names:
        return None
    rows = _ints(gpa, "I") == instance if "I" in gpa.column_names else np.ones(gpa.num_rows, bool)
    t = _ints(gpa, "TimeUS")[rows]
    if not len(t):
        return None
    k = int(np.argmin(np.abs(t - at_us)))
    return float(si.values(gpa, "HAcc")[rows][k])


def measure(
    flight: FlightData, o: Outage, rules: Mapping[str, Any], names: Mapping[str, Any],
    status: tuple[Array, Array] | None, first: Receiver,
) -> Measured:  # fmt: skip
    bits: Mapping[str, int] = rules["status_bits"]
    duration = (o.end_us - o.start_us) / 1e6
    using, held, relative, hold = True, True, False, duration
    detection: float | None = None
    resumed: float | None = None
    if status is not None and len(status[0]):
        st, ss = status

        def on(name: str, rows: Array) -> Array:
            return (ss[rows] >> int(bits[name])) & 1 == 1

        before = np.flatnonzero(st <= o.start_us)
        if len(before):
            using = bool(on("using_gps", before[-1:])[0])
        window = np.flatnonzero((st >= o.start_us) & (st <= o.end_us))
        if len(window):
            stopped = window[~on("using_gps", window)]
            if using and len(stopped):
                detection = round(float(st[stopped[0]] - o.start_us) / 1e6, 1)
            lost = window[~on("horiz_pos_abs", window)]
            held = not len(lost)
            if len(lost):
                hold = float(st[lost[0]] - o.start_us) / 1e6
            relative = bool(np.all(on("horiz_pos_rel", window)))
        after = np.flatnonzero((st > o.end_us) & (st <= o.end_us + float(rules["resume_s"]) * 1e6))
        again = after[on("using_gps", after)] if len(after) else after
        if len(again) and (detection is not None or not using):  # it had stopped using GNSS
            resumed = round(float(st[again[0]] - o.end_us) / 1e6, 1)
    failsafes, mode, reason = _response(
        flight, o, o.end_us + float(rules["response_s"]) * 1e6, names
    )
    # drift means something only while the estimator still claimed a position when GNSS
    # came back: once it gives one up, the position it logs is a stale value
    drift = _drift(flight, o, first, rules) if held or relative else None
    long = duration >= float(rules["drift"]["min_outage_s"])
    rate = drift / (duration / 60) if drift is not None and long else None
    return Measured(
        using_at_start=using, detection_s=detection, duration_s=round(duration, 1),
        hold_s=round(hold, 1), held=held,
        kept_relative=relative, resumed_s=resumed,
        source_set=source_set_at(flight, int(rules["aux"]["source_set"]), o.start_us),
        failsafes=failsafes, mode=mode, reason=reason,
        drift_m=None if drift is None else round(drift, 1),
        rate_m_min=None if rate is None else round(rate, 1),
    )  # fmt: skip


# --- the analyzer ----------------------------------------------------------------------------


def _evidence(flight: FlightData, n: int, o: Outage, m: Measured, rules: Mapping[str, Any]
              ) -> list[Evidence]:  # fmt: skip
    def at(us: int) -> float:
        return round((us - flight.arm_us) / 1e6, 1)

    seg = Segment("no_gnss", o.start_us, o.end_us)
    messages, fields = {"GPS", "XKF4"}, {"Status", "C", "PI", "SS"}
    if o.drill or m.source_set:
        messages, fields = messages | {"AUXF"}, fields | {"function", "pos"}
    if m.failsafes:
        messages, fields = messages | {"ERR"}, fields | {"Subsys", "ECode"}
    if m.mode:
        messages, fields = messages | {"MODE"}, fields | {"ModeNum", "Rsn"}
    what = "GNSS-off drill" if o.drill else "GNSS outage"
    out = [Evidence(
        id=f"no_gnss:outage:{n}", kind="fact",
        summary=f"{what} at {at(o.start_us)} s for {round((o.end_us - o.start_us) / 1e6, 1)} s: "
        f"{m.words()}",
        messages=tuple(sorted(messages)), fields=tuple(sorted(fields)), segment=seg,
        values={"start_s": at(o.start_us), "end_s": at(o.end_us),
                "duration_s": round((o.end_us - o.start_us) / 1e6, 1), "drill": o.drill,
                "using_gnss_at_start": m.using_at_start, "detection_s": m.detection_s,
                "absolute_s": m.hold_s, "kept_s": m.kept_s, "held": m.held,
                "kept_relative": m.kept_relative,
                "resumed_s": m.resumed_s, "source_set": m.source_set,
                "failsafes": list(m.failsafes), "mode": m.mode, "mode_reason": m.reason},
    )]  # fmt: skip
    if m.drift_m is not None:
        values: dict[str, Any] = {"drift_m": m.drift_m}
        words = f"when GNSS returned, the estimate was {m.drift_m:g} m from it"
        if m.rate_m_min:
            tolerance = float(rules["landing_tolerance_m"])
            values |= {"drift_m_per_min": m.rate_m_min,
                       "minutes_to_tolerance": round(tolerance / m.rate_m_min, 1)}  # fmt: skip
            words += (f" ({m.rate_m_min:g} m per minute: {tolerance:g} m off after "
                      f"{tolerance / m.rate_m_min:.1f} min)")  # fmt: skip
        out.append(Evidence(id=f"no_gnss:outage:{n}:drift", kind="fact", summary=words,
                            messages=("GPS", "POS"), fields=("Lat", "Lng"), segment=seg,
                            values=values))  # fmt: skip
    return out


def farthest_m(flight: FlightData) -> float | None:
    """The farthest the aircraft flew from where it armed, horizontally, in metres; None
    without a track over the armed time."""
    track = flight.local_track()
    if track is None:
        return None
    t = track.times_us
    armed = (t >= flight.arm_us) & (t <= flight.disarm_us)
    if not np.any(armed):
        return None
    e, n = track.east_north_m()
    first = int(np.flatnonzero(armed)[0])
    return float(np.max(np.hypot(e[armed] - e[first], n[armed] - n[first])))


def _no_gnss(flight: FlightData, reg: Registry) -> Result:
    if not flight.logged:
        return Result(skipped="a parameter file has no flight to judge")
    rules: Mapping[str, Any] = reg.rules["no_gnss"]
    found = receivers(flight, reg.rules["gnss"])
    fixed = [r for r in found.values() if r.first_fix_us is not None]
    if not fixed:
        return Result(skipped="the log holds no GNSS receiver with a fix")
    first = min(fixed, key=lambda r: r.instance)
    names = reg.names(flight.firmware)
    status = solution(flight)
    out = _measure(flight, reg, rules, found, first, names, status)
    if (far := farthest_m(flight)) is not None:  # last, after the outages
        out.evidence.append(Evidence(
            id="no_gnss:range", kind="fact",
            summary=f"it flew up to {far:.0f} m from where it armed",
            messages=("POS",), fields=("Lat", "Lng"), segment=flight.armed,
            values={"farthest_m": round(far),
                    "armed_s": round((flight.disarm_us - flight.arm_us) / 1e6)},
        ))  # fmt: skip
    return out


def _measure(
    flight: FlightData, reg: Registry, rules: Mapping[str, Any], found: Mapping[int, Receiver],
    first: Receiver, names: Any, status: tuple[Array, Array] | None,
) -> Result:  # fmt: skip
    """Every outage in the flight, its findings and the flight's metrics."""
    out = Result()
    measured: list[tuple[Outage, Measured]] = []
    for n, o in enumerate(outages(flight, rules, found), 1):
        m = measure(flight, o, rules, names, status, first)
        measured.append((o, m))
        evidence = _evidence(flight, n, o, m, rules)
        out.evidence += evidence
        params = {
            "what": "GNSS-off drill" if o.drill else "GNSS lost",
            "start_s": round((o.start_us - flight.arm_us) / 1e6, 1),
            "duration_s": round((o.end_us - o.start_us) / 1e6, 1),
            "hold_s": m.hold_s, "response": m.words(),
        }  # fmt: skip
        if m.held or m.kept_relative:
            out.findings.append(
                Finding("no_gnss.position_held", "info", "high", tuple(evidence), params)
            )
        else:
            out.findings.append(
                Finding("no_gnss.position_lost", "caution", "high", tuple(evidence), params)
            )
    if not measured:
        return out
    ev = Evidence(
        id="no_gnss:flight", kind="fact",
        summary=f"{len(measured)} GNSS outages while armed",
        messages=("GPS", "XKF4"), fields=("C", "PI", "SS", "Status"), segment=flight.armed,
        values={"outages": len(measured), "drills": sum(o.drill for o, _ in measured)},
    )  # fmt: skip
    out.evidence.append(ev)

    def metric(name: str, value: float, cited: Evidence) -> None:
        out.metrics.append(MetricValue(name, "flight", value, reg.metrics[name].zone(value),
                                       cited.id))  # fmt: skip

    metric("no_gnss.hold_s", min(m.kept_s for _, m in measured), ev)
    if detections := [m.detection_s for _, m in measured if m.detection_s is not None]:
        metric("no_gnss.detection_s", max(detections), ev)
    if rates := [m.rate_m_min for _, m in measured if m.rate_m_min is not None]:
        dev = Evidence(id="no_gnss:flight:drift", kind="fact",
                       summary=f"without GNSS the estimate drifted up to {max(rates):g} m a minute",
                       messages=("GPS", "POS"), fields=("Lat", "Lng"), segment=flight.armed,
                       values={"drift_m_per_min": max(rates)})  # fmt: skip
        out.evidence.append(dev)
        metric("no_gnss.drift_rate", max(rates), dev)
    return out


NO_GNSS = Analyzer(
    name="no_gnss",
    version=2,
    run=_no_gnss,
    metrics=("no_gnss.hold_s", "no_gnss.detection_s", "no_gnss.drift_rate"),
    findings=("no_gnss.position_held", "no_gnss.position_lost"),
)
