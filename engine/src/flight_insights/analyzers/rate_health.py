"""The rate loop's health in ordinary flight (spec: Copter analyzers, "Rate-loop tune"):
what the rate PIDs and the measured rates show on any flight that logs them, not only
on the tune check.

- **I term at its limit**: the share of flying time each axis's rate I term spends at
  95% of `ATC_RAT_*_IMAX` or more. An I term pinned at its limit is asking for more steady
  correction than it may give: the centre of gravity off to one side, a tilted or
  twisted motor, or on yaw a motor weaker than its neighbours.
- **Slew limiter**: with `ATC_RAT_*_SMAX` set, the share of flying time the limiter cut
  P and D (`PIDx.Dmod` under 0.9). ArduPilot's limiter is there to cap oscillation from
  too much gain; acting in ordinary flight, it says the gains are near their limit or
  noise is reaching them.
- **D-term noise**: in steady hover, the share of the D term's power above the control
  band (40 Hz, where the filter analysis counts noise). D amplifies what the gyro
  filters let through; most of it above the control band heats motors and does nothing
  for control.
- **Oscillation in hover**: peaks from 1 to 40 Hz in the measured rates during steady
  hover, standing well above the spectrum around them and not at a motor harmonic.

Hover here is the fingerprint's steady-hover windows with the pilot's stick inputs (the
tune check's events) taken out, so a doublet is not read as an oscillation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import (
    Analyzer,
    Evidence,
    Finding,
    MetricValue,
    Result,
    Segment,
)
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.tune import height, logged_hz, needed_hz, rates
from flight_insights.dataflash import si
from flight_insights.segment.excitation import AXES, PID_MESSAGES, RATE_FIELDS, spans
from flight_insights.spectral.gyro import Run
from flight_insights.spectral.peaks import harmonic_of, peaks
from flight_insights.spectral.welch import Spectrum, welch

Array = np.ndarray[Any, Any]
AXIS_PARAM = {"roll": "RLL", "pitch": "PIT", "yaw": "YAW"}


def _rules(reg: Registry) -> Mapping[str, Any]:
    rules: Mapping[str, Any] = reg.rules["tune"]["health"]
    return rules


def _flying(flight: FlightData, reg: Registry, t: Array) -> Array:
    """Armed, and above the tune check's minimum height when the log says how high."""
    ok = (t >= flight.arm_us) & (t <= flight.disarm_us)
    above = height(flight)
    if above is not None:
        ok &= np.interp(t, *above) >= float(reg.rules["tune"]["excitation"]["min_height_m"])
    return ok


def _quiet(flight: FlightData, reg: Registry) -> list[tuple[int, int]]:
    """Steady hover with the pilot's stick inputs, on any axis, taken out."""
    hover = [(s.start_us, s.end_us) for s in flight.hover()]
    rate = flight.table("RATE")
    if not hover or rate is None or rate.num_rows == 0:
        return hover
    t = np.asarray(rate.column("TimeUS").to_numpy(), np.int64)
    excitation: Mapping[str, Any] = reg.rules["tune"]["excitation"]
    busy: list[tuple[int, int]] = []
    for axis in AXES:
        both = rates(rate, axis)
        if both is not None:
            busy += spans(t, both[0], excitation, axis)
    out: list[tuple[int, int]] = []
    for a, b in hover:
        cuts = sorted((max(a, s), min(b, e)) for s, e in busy if s < b and e > a)
        start = a
        for s, e in cuts:
            if s > start:
                out.append((start, s))
            start = max(start, e)
        if start < b:
            out.append((start, b))
    return out


def _runs(t: Array, x: Array, quiet: Sequence[tuple[int, int]], hz: float) -> list[Run]:
    """The samples inside the quiet stretches, split wherever the log has a gap."""
    gap_us = 5e6 / hz
    out: list[Run] = []
    for a, b in quiet:
        inside = np.flatnonzero((t >= a) & (t <= b))
        if len(inside) < 2:
            continue
        cut = np.flatnonzero(np.diff(t[inside]) > gap_us) + 1
        for piece in np.split(inside, cut):
            if len(piece) < 2:
                continue
            ts = t[piece]
            out.append(
                Run(int(ts[0]), int(ts[-1]), hz, x[piece].reshape(len(piece), -1), ts)
            )  # fmt: skip
    return out


def _share(mask: Array, within: Array) -> float | None:
    n = int(within.sum())
    return None if n == 0 else 100.0 * float((mask & within).sum()) / n


def _i_limit(flight: FlightData, reg: Registry, out: Result) -> list[tuple[str, float]]:
    rules = _rules(reg)
    found: list[tuple[str, float]] = []
    for axis in AXES:
        imax = float(flight.params.get(f"ATC_RAT_{AXIS_PARAM[axis]}_IMAX", 0.0))
        pid = flight.table(PID_MESSAGES[axis], ["TimeUS", "I"])
        if imax <= 0 or pid is None or pid.num_rows < 3:
            continue
        t = np.asarray(pid.column("TimeUS").to_numpy(), np.int64)
        flying = _flying(flight, reg, t)
        hz = logged_hz(flight, PID_MESSAGES[axis])
        if hz == 0 or flying.sum() / hz < float(rules["min_flying_s"]):
            continue
        i = np.abs(si.values(pid, "I"))
        share = _share(i >= float(rules["i_limit_share"]) * imax, flying)
        if share is None:
            continue
        held = float(np.median(i[flying])) / imax
        ev = Evidence(
            id=f"rate_i:{axis}",
            kind="metric",
            summary=(
                f"{axis} rate I term at 95% of IMAX ({imax:g}) or more for {share:.1f}% of "
                f"flying time; its median is {100 * held:.0f}% of IMAX"
            ),
            messages=(PID_MESSAGES[axis], "CTUN"),
            fields=("I",),
            segment=flight.armed,
            values={"share_pct": round(share, 2), "imax": imax, "median_share": round(held, 3),
                    "seconds": round(float(flying.sum()) / hz, 1)},
        )  # fmt: skip
        out.evidence.append(ev)
        out.metrics.append(_metric(reg, f"tune.i_at_limit_{axis}", "flight", share, ev))
        if share >= float(rules["i_limit_finding_pct"]):
            found.append((axis, share))
    if found:
        axis, share = max(found, key=lambda f: f[1])
        out.findings.append(
            Finding(
                code="tune.i_limit",
                severity="caution",
                confidence="medium",
                evidence=tuple(
                    e for e in out.evidence if e.id in {f"rate_i:{a}" for a, _ in found}
                ),
                params={"axes": [a for a, _ in found], "axis": axis,
                        "percent": round(share, 1)},
            )
        )  # fmt: skip
    return found


def _slew(flight: FlightData, reg: Registry, out: Result) -> None:
    rules = _rules(reg)
    found: list[tuple[str, float, float]] = []
    for axis in AXES:
        smax = float(flight.params.get(f"ATC_RAT_{AXIS_PARAM[axis]}_SMAX", 0.0))
        pid = flight.table(PID_MESSAGES[axis], ["TimeUS", "Dmod", "SRate"])
        if smax <= 0 or pid is None or pid.num_rows < 3:
            continue  # SMAX 0 switches the limiter off: Dmod stays at 1
        t = np.asarray(pid.column("TimeUS").to_numpy(), np.int64)
        flying = _flying(flight, reg, t)
        hz = logged_hz(flight, PID_MESSAGES[axis])
        if hz == 0 or flying.sum() / hz < float(rules["min_flying_s"]):
            continue
        dmod = si.values(pid, "Dmod")
        share = _share(dmod < float(rules["dmod_limited"]), flying)
        if share is None:
            continue
        slew = si.values(pid, "SRate")[flying]
        ev = Evidence(
            id=f"rate_slew:{axis}",
            kind="metric",
            summary=(
                f"{axis} slew limiter (SMAX {smax:g}) cut P and D under 90% for {share:.1f}% "
                f"of flying time; lowest {float(dmod[flying].min()):.2f}"
            ),
            messages=(PID_MESSAGES[axis], "CTUN"),
            fields=("Dmod", "SRate"),
            segment=flight.armed,
            values={"share_pct": round(share, 2), "smax": smax,
                    "lowest": round(float(dmod[flying].min()), 3),
                    "slew_p99": round(float(np.quantile(slew, 0.99)), 2)},
        )  # fmt: skip
        out.evidence.append(ev)
        out.metrics.append(_metric(reg, f"tune.slew_limited_{axis}", "flight", share, ev))
        if share >= float(rules["slew_finding_pct"]):
            found.append((axis, share, smax))
    if found:
        axis, share, smax = max(found, key=lambda f: f[1])
        out.findings.append(
            Finding(
                code="tune.slew_limited",
                severity="caution",
                confidence="medium",
                evidence=tuple(e for e in out.evidence
                               if e.id in {f"rate_slew:{a}" for a, _, _ in found}),
                params={"axes": [a for a, _, _ in found], "axis": axis,
                        "percent": round(share, 1), "smax": smax},
            )
        )  # fmt: skip


def _d_noise(
    flight: FlightData, reg: Registry, quiet: Sequence[tuple[int, int]], out: Result
) -> None:
    rules = _rules(reg)
    needed = needed_hz(flight, reg)
    band = float(rules["noise_band_low_hz"])
    found: list[tuple[str, float]] = []
    for axis in AXES:
        if float(flight.params.get(f"ATC_RAT_{AXIS_PARAM[axis]}_D", 0.0)) <= 0:
            continue  # no D term to be noisy
        hz = logged_hz(flight, PID_MESSAGES[axis])
        pid = flight.table(PID_MESSAGES[axis], ["TimeUS", "D"])
        if hz < needed or pid is None or hz / 2 <= band:
            continue
        t = np.asarray(pid.column("TimeUS").to_numpy(), np.int64)
        spec = welch(_runs(t, si.values(pid, "D"), quiet, hz), float(rules["resolution_hz"]))
        if spec is None or spec.windows < int(rules["min_windows"]):
            continue
        total = float(spec.band_power(float(rules["low_hz"]), hz / 2)[0])
        if total <= 0:
            continue
        share = 100.0 * float(spec.band_power(band, hz / 2)[0]) / total
        ev = Evidence(
            id=f"rate_d:{axis}",
            kind="metric",
            summary=(
                f"{axis} rate D term in steady hover: {share:.0f}% of its power above {band:g} Hz "
                f"over {spec.seconds:.0f} s"
            ),
            messages=(PID_MESSAGES[axis],),
            fields=("D",),
            segment=Segment("hover", min(a for a, _ in quiet), max(b for _, b in quiet)),
            values={"share_pct": round(share, 1), "band_hz": band,
                    "seconds": round(spec.seconds, 1), "windows": spec.windows},
        )  # fmt: skip
        out.evidence.append(ev)
        out.metrics.append(_metric(reg, f"tune.d_noise_{axis}", "hover", share, ev))
        if share >= float(rules["d_noise_finding_pct"]):
            found.append((axis, share))
    if found:
        axis, share = max(found, key=lambda f: f[1])
        out.findings.append(
            Finding(
                code="tune.d_noise",
                severity="caution",
                confidence="medium",
                evidence=tuple(
                    e for e in out.evidence if e.id in {f"rate_d:{a}" for a, _ in found}
                ),
                params={"axes": [a for a, _ in found], "axis": axis,
                        "percent": round(share), "band_hz": band},
            )
        )  # fmt: skip


def _oscillation(
    flight: FlightData, reg: Registry, quiet: Sequence[tuple[int, int]], out: Result
) -> None:
    rules = _rules(reg)
    osc: Mapping[str, Any] = rules["oscillation"]
    rate = flight.table("RATE")
    hz = logged_hz(flight, "RATE")
    if rate is None or hz < needed_hz(flight, reg):
        return
    t = np.asarray(rate.column("TimeUS").to_numpy(), np.int64)
    low, high = float(osc["low_hz"]), float(osc["high_hz"])
    motor_hz = _motor_hz(flight)
    found: list[tuple[str, float, float, float]] = []
    for axis in AXES:
        both = rates(rate, axis)
        if both is None:
            continue
        spec = welch(_runs(t, both[1], quiet, hz), float(rules["resolution_hz"]))
        if spec is None or spec.windows < int(rules["min_windows"]):
            continue
        unsteady = float(np.sqrt(spec.band_power(low, high)[0]))
        candidates: list[tuple[float, float, float]] = []
        for p in peaks(spec, low, high, float(osc["prominence_db"]), axes=(0,)):
            tol = float(osc["motor_tolerance"])
            if motor_hz and harmonic_of(p.freq_hz, motor_hz, tol) is not None:
                continue
            half = float(osc["peak_half_width_hz"])
            amplitude = _rms(spec, p.freq_hz - half, p.freq_hz + half)
            if amplitude >= float(osc["min_amplitude_dps"]):
                candidates.append((p.freq_hz, amplitude, p.prominence_db))
        top = max(candidates, key=lambda c: c[1]) if candidates else None
        ev = Evidence(
            id=f"rate_osc:{axis}",
            kind="metric",
            summary=(
                f"{axis} rate in steady hover: {unsteady:.1f} deg/s RMS from {low:g} to {high:g} Hz"
                + (f"; a peak at {top[0]:.1f} Hz, {top[1]:.1f} deg/s, {top[2]:.0f} dB above "
                   "the spectrum around it" if top else "; no oscillation peak")
                + f", over {spec.seconds:.0f} s"
            ),
            messages=("RATE",),
            fields=(RATE_FIELDS[axis][1],),
            segment=Segment("hover", min(a for a, _ in quiet), max(b for _, b in quiet)),
            values={"rms_dps": round(unsteady, 2), "seconds": round(spec.seconds, 1),
                    "peak_hz": None if top is None else round(top[0], 2),
                    "peak_dps": None if top is None else round(top[1], 2),
                    "peak_db": None if top is None else round(top[2], 1),
                    "motor_hz": None if motor_hz is None else round(motor_hz, 1)},
        )  # fmt: skip
        out.evidence.append(ev)
        out.metrics.append(_metric(reg, f"tune.hover_unsteady_{axis}", "hover", unsteady, ev))
        if top is not None:
            found.append((axis, *top))
    if found:
        axis, at_hz, amplitude, _ = max(found, key=lambda f: f[2])
        out.findings.append(
            Finding(
                code="tune.oscillation",
                severity="caution",
                confidence="medium",
                evidence=tuple(e for e in out.evidence
                               if e.id in {f"rate_osc:{a}" for a, *_ in found}),
                params={"axes": [a for a, *_ in found], "axis": axis,
                        "hz": round(at_hz, 1), "dps": round(amplitude, 1)},
            )
        )  # fmt: skip


def _motor_hz(flight: FlightData) -> float | None:
    """The motors' frequency in hover, from the fingerprint (ESC RPM), when it has one."""
    entry = flight.fingerprint.get("motor_hz_at_hover")
    value = cast(Mapping[str, Any], entry).get("value") if isinstance(entry, dict) else None
    return float(value) if isinstance(value, int | float) and value > 0 else None


def _rms(spec: Spectrum, low_hz: float, high_hz: float) -> float:
    return float(np.sqrt(spec.band_power(max(0.0, low_hz), high_hz)[0]))


def _metric(reg: Registry, name: str, segment: str, value: float, ev: Evidence) -> MetricValue:
    entry = reg.metrics[name]
    return MetricValue(entry.id, segment, value, entry.zone(value), ev.id)


def _rate_health(flight: FlightData, reg: Registry) -> Result:
    if not flight.logged:
        return Result(skipped="a parameter file has no flight to measure")
    if flight.vehicle != "copter":  # Plane's and QuadPlane's rate loops: Phase 3
        return Result(skipped="the rate-loop health checks are for Copter")
    if not any(logged_hz(flight, m) for m in PID_MESSAGES.values()):
        return Result(skipped="the log has no rate PID messages (PIDR, PIDP, PIDY)")
    out = Result()
    _i_limit(flight, reg, out)
    _slew(flight, reg, out)
    quiet = _quiet(flight, reg)
    if quiet:
        _d_noise(flight, reg, quiet, out)
        _oscillation(flight, reg, quiet, out)
    if not out.evidence:
        return Result(skipped="too little flying, or no steady hover, to judge the rate loop")
    return out


RATE_HEALTH = Analyzer(
    name="rate_health",
    version=1,
    run=_rate_health,
    metrics=tuple(
        f"tune.{m}_{axis}"
        for axis in AXES
        for m in ("i_at_limit", "slew_limited", "d_noise", "hover_unsteady")
    ),
    findings=("tune.i_limit", "tune.slew_limited", "tune.d_noise", "tune.oscillation"),
)
