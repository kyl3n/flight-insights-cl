"""The rate loop's responses: how each axis's measured rate answers its target, across
frequency and to a step (spec: Copter analyzers, "Rate-loop tune").

Reads the rate PIDs' own target and measurement (PIDR, PIDP, PIDY: Tar, the target
after ATC_RAT_*_FLTT, and Act) where they measure the rate loop alone, by the tune
check's rules: flown in a stick mode, above 2 m, with no gap in the log, and away from
the PID's saturation. The attitude loop around the rate loop carries gusts into the
target, so each response is read through the rate of the attitude target (ANG), which
the stick sets and gusts do not (spectral/response.py). Per axis:

- the frequency response: the -3 dB bandwidth and the phase there, the peak gain, and
  the sensitivity peak Ms, over the band the instrument reaches;
- the step response: rise time, overshoot, settling and its final gain.

Each comes with a seeded 95% interval, so flights of the same profile can be compared,
before and after a tuning change and light and loaded. A sensitivity peak of 2 or more
is a finding: the loop amplifies disturbances near that frequency and is close to
oscillating. The peak is the loop's sensitivity only when its controller acts on the
error alone, so it is left out on an axis with feed-forward (FF or D_FF) set.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

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
from flight_insights.analyzers.tune import height, logged_hz, modes, needed_hz
from flight_insights.dataflash import si
from flight_insights.segment.excitation import AXES, PID_MESSAGES
from flight_insights.spectral.response import (
    Correlations,
    CrossSpectra,
    Margins,
    Pick,
    StepFigures,
    Stretch,
    bootstrap,
    correlations,
    cross_spectra,
    figures,
    margins,
    step,
)
from flight_insights.spectral.welch import segment_length

Array = np.ndarray[Any, Any]
ATTITUDE_TARGETS = {"roll": "DesRoll", "pitch": "DesPitch", "yaw": "DesYaw"}
TO_DEG = {"deg": 1.0, "degheading": 1.0, "rad": 180.0 / math.pi}
HEADINGS = ("degheading",)
STEP_FIGURES = {"rise_s": 1000.0, "overshoot_pct": 1.0, "settling_s": 1000.0}  # to ms and %
MARGINS = ("bandwidth_hz", "sensitivity_peak")


@dataclass(frozen=True)
class _Loop:
    """One axis's samples at the PID's times, and which of them measure the loop alone."""

    t: Array
    instrument: Array  # deg/s
    target: Array  # in the PID's own unit, whatever it is: only ratios are taken
    actual: Array
    ok: Array


def _attitude_rate(flight: FlightData, axis: str) -> tuple[Array, Array] | None:
    """The attitude target's rate of change in deg/s, at ANG's times."""
    name = ATTITUDE_TARGETS[axis]
    ang = flight.table("ANG", ["TimeUS", name])
    if ang is None or ang.num_rows < 3 or si.unit(ang, name) not in TO_DEG:
        return None
    t, keep = np.unique(np.asarray(ang.column("TimeUS").to_numpy(), np.int64), return_index=True)
    if len(t) < 3:
        return None
    deg = si.values(ang, name)[keep] * TO_DEG[si.unit(ang, name)]
    if si.unit(ang, name) in HEADINGS:
        deg = np.degrees(np.unwrap(np.radians(deg)))
    return t, np.gradient(deg, t / 1e6)


def _loop(
    flight: FlightData, reg: Registry, axis: str, gap_us: float, rules: Mapping[str, Any]
) -> _Loop | None:
    pid = flight.table(PID_MESSAGES[axis], ["TimeUS", "Tar", "Act", "Flags"])
    if pid is None or pid.num_rows < 3 or si.unit(pid, "Tar") != si.unit(pid, "Act"):
        return None
    attitude = _attitude_rate(flight, axis)
    if attitude is None:
        return None
    ta, rate = attitude
    t = np.asarray(pid.column("TimeUS").to_numpy(), np.int64)
    excitation: Mapping[str, Any] = reg.rules["tune"]["excitation"]
    ok = flight.during(t)
    stick = np.zeros(len(t), dtype=bool)
    for a, b, name in modes(flight, reg):
        if name in excitation["stick_modes"]:
            stick |= (t >= a) & (t < b)
    ok &= stick
    above = height(flight)
    if above is not None:
        ok &= np.interp(t, *above) >= float(excitation["min_height_m"])
    flags = np.asarray(pid.column("Flags").to_numpy(), np.int64)
    hits = t[(flags >> int(excitation["limit_flag"]) & 1).astype(bool)]
    if len(hits):
        ok &= _apart(t, hits) > float(rules["saturation_pad_s"]) * 1e6
    ok &= _apart(t, ta) <= gap_us  # the attitude target logged alongside
    return _Loop(
        t, np.interp(t, ta, rate), si.values(pid, "Tar"), si.values(pid, "Act"), ok
    )  # fmt: skip


def _apart(t: Array, marks: Array) -> Array:
    """How far each time is from the nearest mark, in microseconds."""
    k = np.searchsorted(marks, t)
    before = np.abs(t - marks[np.clip(k - 1, 0, len(marks) - 1)])
    after = np.abs(marks[np.clip(k, 0, len(marks) - 1)] - t)
    return np.minimum(before, after)


def _stretches(loop: _Loop, gap_us: float, shortest: int) -> list[Stretch]:
    """Runs of usable samples with no gap, at least `shortest` long."""
    cut = ~loop.ok | np.concatenate([[True], np.diff(loop.t) > gap_us])
    starts = np.flatnonzero(cut)
    out: list[Stretch] = []
    for a, b in zip(starts, np.concatenate([starts[1:], [len(loop.t)]]), strict=True):
        a = a if loop.ok[a] else a + 1
        if b - a >= shortest:
            out.append(
                Stretch(loop.instrument[a:b].copy(), loop.target[a:b].copy(),
                        loop.actual[a:b].copy())
            )  # fmt: skip
    return out


def _band(rules: Mapping[str, Any]) -> tuple[float, float, float, float, float]:
    return (
        float(rules["max_error"]), float(rules["low_hz"]), float(rules["high_hz"]),
        float(rules["peak_drop"]), float(rules["peak_reach"]),
    )  # fmt: skip


def _frequency(
    spectra: CrossSpectra, rules: Mapping[str, Any], seed: int
) -> dict[str, tuple[float, float] | None]:
    """The frequency response's margins' intervals, over resampled windows."""
    band = _band(rules)

    def stats(pick: Pick) -> dict[str, float | None]:
        m = margins(spectra.response(pick), *band)
        return {a: None if m is None else getattr(m, a) for a in MARGINS}

    return bootstrap(
        spectra.windows, stats, MARGINS, seed, int(rules["resamples"]), float(rules["level"])
    )


def _step(
    corr: Correlations, hz: float, rules: Mapping[str, Any], seed: int
) -> tuple[StepFigures, dict[str, tuple[float, float] | None]] | None:
    """The step response's figures, with intervals over resampled blocks."""
    loading = float(rules["loading"])
    whole = step(corr, loading=loading)
    if whole is None:
        return None

    def stats(pick: Pick) -> dict[str, float | None]:
        s = step(corr, pick, loading)
        fig = None if s is None else figures(s, hz)
        return {a: None if fig is None else getattr(fig, a) for a in STEP_FIGURES}

    spread = bootstrap(
        corr.blocks, stats, tuple(STEP_FIGURES), seed, int(rules["resamples"]),
        float(rules["level"]),
    )  # fmt: skip
    return figures(whole, hz), spread


def _feed_forward(flight: FlightData, axis: str, rules: Mapping[str, Any]) -> list[float]:
    return [float(flight.params.get(name, 0.0)) for name in rules["feed_forward"][axis]]


def _summary(axis: str, seconds: float, freq: Margins | None, fig: StepFigures | None) -> str:
    parts = [f"{axis} rate loop over {seconds:.0f} s of stick flying:"]
    if freq is not None:
        if freq.bandwidth_hz is not None:
            parts.append(f"bandwidth {freq.bandwidth_hz:.1f} Hz,")
        peak = f"{freq.sensitivity_peak:.2f} at {freq.sensitivity_peak_hz:.0f} Hz"
        parts.append(
            f"peak of |1 - H| {peak},"
            if freq.sensitivity_peak_inside
            else f"|1 - H| still rising at the top ({peak}),"
        )
        parts.append(f"measured to {freq.measured_to_hz:.0f} Hz;")
    if fig is not None:
        if fig.rise_s is not None:
            parts.append(f"step rise {fig.rise_s * 1000:.0f} ms,")
        parts.append(f"overshoot {fig.overshoot_pct:.0f}%, settling {fig.settling_s * 1000:.0f} ms")
    return " ".join(parts).rstrip(",;")


@dataclass(frozen=True)
class Responses:
    """One axis's stick flying that measures the rate loop alone, read as cross-spectra
    and correlations, with the frequency response's margins."""

    hz: float
    seconds: float  # of stick flying measured
    used: Array  # the times of the samples measured
    spectra: CrossSpectra
    corr: Correlations
    margins: Margins

    def step_response(self, rules: Mapping[str, Any]) -> Array | None:
        """The step response, where the inputs measured the loop past its bandwidth: a
        step's rise and overshoot rest on the response up to it and beyond."""
        if self.margins.bandwidth_hz is None:
            return None
        return step(self.corr, loading=float(rules["loading"]))


def loop_hz(flight: FlightData, reg: Registry) -> float | str:
    """How often the rate PIDs were logged, when the responses can be measured from
    them; otherwise why not."""
    if not flight.logged:
        return "a parameter file has no flight to measure"
    if flight.vehicle != "copter":  # Plane's and QuadPlane's rate loops: Phase 3
        return "the rate-loop responses are for Copter"
    needed = needed_hz(flight, reg)
    hz = min(logged_hz(flight, m) for m in PID_MESSAGES.values())
    if hz == 0:
        return "the log has no rate PID messages (PIDR, PIDP, PIDY)"
    if hz < needed:
        return f"the rate PIDs are logged at {hz:.0f} Hz; the responses need {needed:.0f}"
    ang = logged_hz(flight, "ANG")
    if ang < needed:
        return (
            f"the attitude targets (ANG) are logged at {ang:.0f} Hz; "
            f"the responses need {needed:.0f}"
        )
    return hz


def responses(flight: FlightData, reg: Registry, axis: str, hz: float) -> Responses | None:
    """An axis's responses, or None with too little stick flying to measure them."""
    rules: Mapping[str, Any] = reg.rules["tune"]["response"]
    gap_us = float(reg.rules["tune"]["excitation"]["gap_periods"]) * 1e6 / hz
    loop = _loop(flight, reg, axis, gap_us, rules)
    if loop is None:
        return None
    n = segment_length(hz, float(rules["resolution_hz"]))
    block, taps = int(float(rules["block_s"]) * hz), int(float(rules["step_s"]) * hz)
    least = float(rules["min_instrument_rms_dps"])
    moving = [
        s for s in _stretches(loop, gap_us, min(n, block)) if float(np.std(s.instrument)) >= least
    ]
    spectra, corr = cross_spectra(moving, hz, n), correlations(moving, block, taps)
    if spectra is None or corr is None:
        return None
    if spectra.windows < int(rules["min_windows"]) or corr.blocks < int(rules["min_blocks"]):
        return None
    m = margins(spectra.response(), *_band(rules))
    if m is None:
        return None
    seconds = sum(len(s.target) for s in moving) / hz
    return Responses(hz, seconds, loop.t[loop.ok], spectra, corr, m)


def _axis(
    flight: FlightData, reg: Registry, axis: str, hz: float, out: Result
) -> tuple[str, float, float] | None:
    """Measures one axis into `out`; returns (axis, Ms, where) when its margin is low."""
    rules: Mapping[str, Any] = reg.rules["tune"]["response"]
    r = responses(flight, reg, axis, hz)
    if r is None:
        return None
    seed = int(rules["seed"][axis])
    m, freq_spread = r.margins, _frequency(r.spectra, rules, seed)
    # A step's rise and overshoot rest on the response up to the bandwidth and past it:
    # taken only when the inputs measured that far
    stepped = None if m.bandwidth_hz is None else _step(r.corr, hz, rules, seed + 100)
    fig, step_spread = (None, {}) if stepped is None else stepped
    feed_forward = _feed_forward(flight, axis, rules)
    seconds, spectra, corr = r.seconds, r.spectra, r.corr
    values: dict[str, Any] = {
        "seconds": round(seconds, 1),
        "windows": spectra.windows,
        "blocks": corr.blocks,
        "feed_forward": [round(v, 4) for v in feed_forward],
        "bandwidth_hz": None if m.bandwidth_hz is None else round(m.bandwidth_hz, 2),
        "phase_at_bandwidth_deg": None
        if m.phase_at_bandwidth_deg is None
        else round(m.phase_at_bandwidth_deg, 1),
        "peak_db": round(m.peak_db, 2),
        "error_peak": round(m.sensitivity_peak, 3),
        "error_peak_hz": round(m.sensitivity_peak_hz, 1),
        "error_peak_inside": m.sensitivity_peak_inside,
        "measured_to_hz": round(m.measured_to_hz, 1),
        "step_measured": fig is not None,
    }
    if fig is not None:
        values["step_gain"] = round(fig.gain, 3)
    used = r.used
    ev = Evidence(
        id=f"rate:{axis}",
        kind="metric",
        summary=_summary(axis, seconds, m, fig),
        messages=(PID_MESSAGES[axis], "ANG", "MODE", "CTUN"),
        fields=("Tar", "Act", ATTITUDE_TARGETS[axis]),
        segment=Segment("inputs", int(used[0]), int(used[-1])),
        values=values,
    )
    out.evidence.append(ev)

    def metric(name: str, value: float | None, spread: tuple[float, float] | None) -> None:
        if value is None:
            return
        entry = reg.metrics[f"tune.{name}_{axis}"]
        out.metrics.append(
            MetricValue(
                entry.id, "inputs", value, entry.zone(value), ev.id,
                spread={} if spread is None else {"low": spread[0], "high": spread[1]},
            )
        )  # fmt: skip

    if fig is not None:
        for name, attr in (("rise_time", "rise_s"), ("overshoot", "overshoot_pct"),
                           ("settling", "settling_s")):  # fmt: skip
            value, spread, k = getattr(fig, attr), step_spread.get(attr), STEP_FIGURES[attr]
            metric(
                name,
                None if value is None else value * k,
                None if spread is None else (spread[0] * k, spread[1] * k),
            )
    metric("bandwidth", m.bandwidth_hz, freq_spread.get("bandwidth_hz"))
    if any(feed_forward) or not m.sensitivity_peak_inside:
        return None  # not the loop's sensitivity, or its peak lies above the measured band
    metric("sensitivity_peak", m.sensitivity_peak, freq_spread.get("sensitivity_peak"))
    zone = reg.metrics[f"tune.sensitivity_peak_{axis}"].zone(m.sensitivity_peak)
    if zone in ("caution", "out"):
        return axis, m.sensitivity_peak, m.sensitivity_peak_hz
    return None


def _rate_tune(flight: FlightData, reg: Registry) -> Result:
    hz = loop_hz(flight, reg)
    if isinstance(hz, str):
        return Result(skipped=hz)
    out = Result()
    low = [found for axis in AXES if (found := _axis(flight, reg, axis, hz, out))]
    if not out.evidence:
        return Result(skipped="too little stick flying to measure the rate loop's responses")
    _low_margin(low, out, reg)
    return out


def _low_margin(found: Sequence[tuple[str, float, float]], out: Result, reg: Registry) -> None:
    if not found:
        return
    axis, ms, at_hz = max(found, key=lambda f: f[1])
    zone = reg.metrics[f"tune.sensitivity_peak_{axis}"].zone(ms)
    cited = tuple(e for e in out.evidence if e.id in {f"rate:{a}" for a, _, _ in found})
    out.findings.append(
        Finding(
            code="tune.low_margin",
            severity=SEVERITY_OF_ZONE[zone or "caution"],
            confidence="medium",
            evidence=cited,
            params={"axes": [a for a, _, _ in found], "ms": round(ms, 2), "hz": round(at_hz, 1)},
        )
    )


RATE_TUNE = Analyzer(
    name="rate_tune",
    version=1,
    run=_rate_tune,
    metrics=tuple(
        f"tune.{m}_{axis}"
        for axis in AXES
        for m in ("bandwidth", "sensitivity_peak", "rise_time", "overshoot", "settling")
    ),
    findings=("tune.low_margin",),
    spans=True,
)
