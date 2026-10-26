"""How fast each motor follows its command (spec: Copter analyzers, heavy-lift,
"Actuator response"; rules in `knowledge/rules/motor_lag.json`).

Large propellers and low-KV motors spin up and down slowly, and the rate loop can ask
for no faster than its motors answer. The command each motor was given is rebuilt from
what the mixer was asked for, logged every loop in RATE (its roll, pitch, yaw and
throttle inputs), and the frame's own mixer factors (knowledge/frames/, checked in
SITL by knowledge/tools/check_motor_frames_sitl.py); the motor's answer is its ESC's
RPM. RCOU, the outputs themselves, is logged at only 10 Hz, too slow to time.

- **Sampling.** Both go onto a common grid at the rate the ESC is logged: the command
  averaged over each grid step, centred on it, so the averaging adds no delay; the RPM
  between its logged values. Stretches where any motor's command is near its limits
  (stopped, or saturated) are left out, as is any stretch shorter than a window.
- **The response** from command to RPM, by Welch cross-spectra (spectral/response.py),
  and a first-order lag with dead time fitted to it (spectral/lag.py): the time
  constant, the dead time, and the response time, their sum, when a step reaches 63%.
- **The interval** from a seeded bootstrap over the windows.

What the log calls the RPM's time is when it was logged, which can be up to one
logging step (10 ms at 100 Hz) after the ESC measured it; the response time includes
that, and the ESC's own telemetry path. The metric has no zones until real heavy-lift
flights have calibrated it; a motor much slower than its siblings is the finding.
ArduPilot's simulator models no motor dynamics, so there the response is a few
milliseconds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import (
    Analyzer,
    Evidence,
    Finding,
    MetricValue,
    Result,
    Segment,
)
from flight_insights.analyzers.motor_out import motor_frame
from flight_insights.analyzers.motors_health import motors
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si
from flight_insights.spectral.lag import Lag, LagFit
from flight_insights.spectral.response import Response, Stretch, cross_spectra

Array = np.ndarray[Any, Any]
INPUTS = ("AOut", "ROut", "POut", "YOut")  # the mixer's throttle, roll, pitch, yaw
FACTORS = ("throttle", "roll", "pitch", "yaw")


def _times(t: pa.Table) -> Array:
    return np.asarray(t.column("TimeUS").to_numpy(), np.int64)


def loop_spans(t: Array, gap_us: float, shortest_us: float) -> list[tuple[int, int]]:
    """Index ranges [a, b) of RATE logged every loop (no gap over `gap_us`), each at
    least `shortest_us` long."""
    if len(t) < 2:
        return []
    close = np.diff(t) <= gap_us
    edges = np.diff(np.concatenate([[0], close.astype(np.int8), [0]]))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return [(int(a), int(b) + 1) for a, b in zip(starts, ends, strict=True)
            if t[b] - t[a] >= shortest_us]  # fmt: skip


def stretches(
    t: Array, command: Array, et: Array, rpm: Array, spans: Sequence[tuple[int, int]],
    hz: float, n: int, valid: Array,
) -> list[Stretch]:  # fmt: skip
    """Command and RPM on a grid at `hz` inside each span, in runs of at least `n`
    samples where `valid` (indexed like `t`) holds throughout each step. The grid starts
    on an ESC row, so where the ESC is logged at `hz` its RPM is read as logged: reading
    between rows would average two of them and lose gain at the top of the band."""
    step = 1e6 / hz
    # the command's running integral, exact for a command linear between its samples,
    # so each step's average is centred on it however the samples fall
    total = np.concatenate([[0.0], np.cumsum(np.diff(t) * (command[1:] + command[:-1]) / 2)])
    bad = np.concatenate([[0], np.cumsum(~valid)])
    out: list[Stretch] = []
    for a, b in spans:
        k = int(np.searchsorted(et, float(t[a]) + step / 2))
        if k >= len(et):
            continue
        first, last = float(et[k]), min(t[b - 1], et[-1]) - step / 2
        if last - first < n * step:
            continue
        grid = np.arange(first, last, step)
        lo = np.searchsorted(t, grid - step / 2)
        hi = np.searchsorted(t, grid + step / 2, side="right")
        ok = (hi > lo) & (bad[hi] - bad[lo] == 0) & (grid - step / 2 >= t[a])
        cmd = (np.interp(grid + step / 2, t, total) - np.interp(grid - step / 2, t, total)) / step
        speed = np.interp(grid, et, rpm)
        edges = np.diff(np.concatenate([[0], ok.astype(np.int8), [0]]))
        for s, e in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1), strict=True):
            if e - s >= n:
                out.append(Stretch(cmd[s:e], cmd[s:e], speed[s:e]))
    return out


def unboxed(r: Response, hz: float) -> Response:
    """The response with the command's averaging over one grid step taken back out: a
    centred average over 1/hz has the gain sinc(f / hz) and no phase."""
    return Response(r.freqs, r.h * np.sinc(r.freqs / hz), r.error)


def resampled(
    fit: LagFit, ss: Sequence[Stretch], hz: float, rules: Mapping[str, Any]
) -> tuple[Lag | None, Array]:
    """The fit, and the response time refitted on each seeded resample of the windows
    (NaN where a resample had too few good bins)."""
    count = int(rules["bootstrap"])
    cs = cross_spectra(ss, hz, int(rules["window"]))
    if cs is None:
        return None, np.full(count, np.nan)
    max_error, min_bins = float(rules["max_error"]), int(rules["min_bins"])
    lag = fit.fit(unboxed(cs.response(), hz), max_error, min_bins)
    rng = np.random.default_rng(int(rules["seed"]))
    found = np.full(count, np.nan)
    for b in range(count if lag is not None else 0):
        pick = rng.integers(0, cs.windows, cs.windows)
        again = fit.fit(unboxed(cs.response(pick), hz), max_error, min_bins)
        if again is not None:
            found[b] = again.response_s
    return lag, found


def _bounds(samples: Array) -> tuple[float, float] | None:
    """The 95% interval, in ms, when most resamples gave an answer."""
    kept = samples[np.isfinite(samples)]
    if len(kept) < len(samples) // 2 or not len(kept):
        return None
    low, high = np.percentile(kept, [2.5, 97.5])
    return round(1000.0 * float(low), 1), round(1000.0 * float(high), 1)


def _motor_lag(flight: FlightData, reg: Registry) -> Result:
    if not flight.logged:
        return Result(skipped="a parameter file has no flight to time")
    if flight.vehicle != "copter":
        return Result(skipped="the motors' response is read for Copter")
    rules: Mapping[str, Any] = reg.rules["motor_lag"]
    found = motor_frame(flight, reg)
    if isinstance(found, str):
        return Result(skipped=found)
    frame, name = found
    rate, esc = flight.table("RATE"), flight.table("ESC")
    if rate is None or rate.num_rows < 2 or not set(INPUTS) <= set(rate.column_names):
        return Result(skipped="the log has no RATE with the mixer's inputs")
    if esc is None or "RPM" not in esc.column_names:
        return Result(skipped="the log has no ESC RPM (bidirectional DShot or ESC telemetry)")
    t = _times(rate)
    spans = loop_spans(t, float(rules["loop_gap_us"]), 1e6 * float(rules["min_span_s"]))
    if not spans:
        hz = 1e6 / float(np.median(np.diff(t)))
        return Result(skipped=f"RATE is logged at {hz:.0f} Hz: timing the motors needs it every"
                      " loop (LOG_BITMASK bit 0, as on the tune-check card)")  # fmt: skip
    inputs = np.column_stack([si.values(rate, c) for c in INPUTS])
    table: list[Mapping[str, Any]] = frame["motors"]
    commands = inputs @ np.array([[float(m[f]) for f in FACTORS] for m in table]).T
    lo, hi = (float(v) for v in rules["command_range"])
    valid = ((commands > lo) & (commands < hi)).all(axis=1)
    hz, n = float(rules["grid_hz"]), int(rules["window"])
    channel = {m.number: m.channel for m in motors(flight.params)}
    et_all, inst = _times(esc), np.asarray(esc.column("Instance").to_numpy(), np.int64)
    rpm_all = si.values(esc, "RPM")
    fit = LagFit(np.fft.rfftfreq(n, 1.0 / hz), (float(rules["band_hz"][0]),
                 float(rules["band_hz"][1])), float(rules["time_constant_max_s"]),
                 float(rules["time_constant_step_s"]), float(rules["dead_time_max_s"]),
                 float(rules["dead_time_step_s"]))  # fmt: skip
    rows: list[tuple[int, Lag, Array]] = []
    for i, m in enumerate(table):
        number = int(m["motor"])
        if number not in channel:
            continue
        mine = inst == channel[number] - 1
        et, rpm = et_all[mine], rpm_all[mine]
        inside = sum(int(((et >= t[a]) & (et <= t[b - 1])).sum()) for a, b in spans)
        seconds = sum(float(t[b - 1] - t[a]) for a, b in spans) / 1e6
        if len(et) < 2 or inside / seconds < float(rules["min_esc_hz"]) or not (rpm > 0).any():
            continue
        ss = stretches(t, commands[:, i], et, rpm, spans, hz, n, valid)
        lag, samples = resampled(fit, ss, hz, rules)
        if lag is not None:
            rows.append((number, lag, samples))
    if not rows:
        return Result(skipped="no motor's ESC RPM followed a command clearly enough to time "
                      "(ESC RPM logged too slowly, or too little stick input)")  # fmt: skip

    def ms(seconds: float) -> float:
        return round(1000.0 * seconds, 1)

    numbers = [r[0] for r in rows]
    response = [ms(r[1].response_s) for r in rows]
    bounds = [_bounds(r[2]) for r in rows]
    median = float(np.median(response))
    # the median's interval: each resample's median over the motors (the same seed
    # draws the same windows for every motor)
    together = _bounds(np.median(np.stack([r[2] for r in rows]), axis=0))
    seg = Segment("flight", int(t[spans[0][0]]), int(t[spans[-1][1] - 1]))
    values: dict[str, Any] = {
        "frame": name, "motors": numbers, "response_ms": response,
        "time_constant_ms": [ms(r[1].time_constant_s) for r in rows],
        "dead_time_ms": [ms(r[1].dead_time_s) for r in rows],
        "low_ms": [None if b is None else b[0] for b in bounds],
        "high_ms": [None if b is None else b[1] for b in bounds],
        "bins": [r[1].bins for r in rows], "grid_hz": hz,
    }  # fmt: skip
    slowest = int(np.argmax(response))
    ev = Evidence(
        id="motors:response", kind="metric",
        summary=f"{name}: motors reach 63% of a command change in {min(response):.0f} to "
        f"{max(response):.0f} ms (median {median:.0f} ms), from command to ESC RPM",
        messages=("RATE", "ESC"), fields=(*INPUTS, "RPM"), segment=seg, values=values,
    )  # fmt: skip
    out = Result(evidence=[ev])
    spread = {} if together is None else {"low": together[0], "high": together[1],
                                           "motors": float(len(rows))}  # fmt: skip
    out.metrics.append(MetricValue("motors.response_ms", "flight", round(median, 1),
                                   reg.metrics["motors.response_ms"].zone(median), ev.id,
                                   spread))  # fmt: skip
    if len(rows) >= 3:
        others = float(np.median([v for k, v in enumerate(response) if k != slowest]))
        low = bounds[slowest]
        excess = response[slowest] - others
        if (excess > max(float(rules["slow_ms"]), float(rules["slow_share"]) * others)
                and (low is None or low[0] > others)):  # fmt: skip
            out.findings.append(Finding(
                "motors.slow_motor", "caution", "medium", (ev,),
                {"motor": numbers[slowest], "response_ms": response[slowest],
                 "others_ms": round(others, 1)},
            ))  # fmt: skip
    return out


MOTOR_LAG = Analyzer(
    name="motor_lag",
    version=1,
    run=_motor_lag,
    metrics=("motors.response_ms",),
    findings=("motors.slow_motor",),
)
