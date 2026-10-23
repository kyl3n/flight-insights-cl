"""Raw gyro samples as spectra need them (spec: The filter analyzer in detail).

GYR holds every gyro sample at the sensor's rate, stamped by the sensor's own clock
(SampleUS). Spectra need evenly spaced samples, so the samples are cut into runs
wherever the clock has a hole (a lost sample, or raw logging switched off), and
each run's rate is its sample count over its span, as ArduPilot's Filter Review
tool reads it. Nothing is decimated or interpolated.

With INS_RAW_LOG_OPT bit 3 ("pre and post filter") each gyro is logged twice: its
pre-filter samples as its own instance, and its post-filter samples as that
instance plus the number of gyros.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.dataflash import si

Floats = np.ndarray[tuple[int, ...], np.dtype[np.float64]]
AXES = ("GyrX", "GyrY", "GyrZ")
HOLE = 1.5  # a step this many times the clock's longest usual step is a hole
POST_FILTER_ONLY, PRE_AND_POST = 2, 3  # INS_RAW_LOG_OPT bits


@dataclass(frozen=True)
class Run:
    """Contiguous samples of one gyro: (n, 3) in deg/s, X, Y and Z, with each sample's
    log time."""

    start_us: int
    end_us: int
    rate_hz: float
    samples: Floats
    times_us: np.ndarray[tuple[int], np.dtype[np.int64]]


@dataclass(frozen=True)
class GyroLayout:
    """Which GYR instances hold which data."""

    pre: tuple[int, ...]  # pre-filter instances
    post: dict[int, int]  # pre-filter instance -> its post-filter instance


def layout(raw_log_opt: int, gyro_count: int | None, instances: list[int]) -> GyroLayout:
    """Read INS_RAW_LOG_OPT the way AP_InertialSensor_Backend::log_gyro_raw writes."""
    if raw_log_opt >> PRE_AND_POST & 1 and gyro_count:
        pre = tuple(i for i in instances if i < gyro_count)
        return GyroLayout(pre, {i: i + gyro_count for i in pre if i + gyro_count in instances})
    if raw_log_opt >> POST_FILTER_ONLY & 1:
        return GyroLayout((), {})
    return GyroLayout(tuple(instances), {})


def runs(
    gyr: pa.Table, instance: int, start_us: int, end_us: int, min_samples: int = 2
) -> list[Run]:
    """One instance's samples between two log times (TimeUS), cut at clock holes."""
    inst = np.asarray(gyr.column("I").to_numpy(), np.int64)
    t = np.asarray(gyr.column("TimeUS").to_numpy(), np.int64)
    sel = np.flatnonzero((inst == instance) & (t >= start_us) & (t <= end_us))
    if len(sel) < min_samples:
        return []
    clock = np.asarray(gyr.column("SampleUS").to_numpy(), np.int64)[sel]
    data: Any = np.column_stack([si.values(gyr, a)[sel] for a in AXES]) * (180.0 / np.pi)
    step = np.diff(clock)
    usual = float(np.percentile(step, 90)) if len(step) else 0.0
    cut = np.flatnonzero((step <= 0) | (step > HOLE * usual)) + 1 if usual > 0 else np.empty(0, int)
    out: list[Run] = []
    for a, b in zip(np.concatenate([[0], cut]), np.concatenate([cut, [len(sel)]]), strict=True):
        n = int(b - a)
        span = int(clock[b - 1] - clock[a])
        if n < min_samples or span <= 0:
            continue
        out.append(
            Run(
                int(t[sel[a]]),
                int(t[sel[b - 1]]),
                (n - 1) * 1e6 / span,
                data[a:b].copy(),
                t[sel[a:b]].copy(),
            )
        )
    return out
