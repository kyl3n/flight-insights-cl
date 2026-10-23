"""Check the filter model against a flight (spec: The filter analyzer in detail, step 4;
Roadmap, Phase 1 gate "filter prediction on target").

The primary gyro's logged pre-filter samples go through the flight's own filter
settings, sample by sample, and the result is compared with the post-filter samples
the firmware logged over the same stretches (INS_RAW_LOG_OPT bit 3). Agreement means
the app's predictions for other settings can be trusted on this aircraft.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.filters.predict import FilterChain, Prediction, chain, phase_lag_deg
from flight_insights.filters.simulate import simulate
from flight_insights.spectral.gyro import Run, layout, runs
from flight_insights.spectral.welch import welch

LEAD_US = 2_000_000  # filters settle on this much data before a span is measured
CONTROL_HZ = (10.0, 20.0, 40.0)  # phase lag is reported at these frequencies
BAND_HZ = 10.0
CONTROL_BAND = (0.0, 40.0)
TOP = 0.45  # the comparison stops at this share of the sample rate


@dataclass(frozen=True)
class Check:
    instance: int
    rate_hz: float
    prediction: Prediction  # predicted: simulated; logged: the firmware's, if logged
    filters: FilterChain
    tracking: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    phase_lag_deg: dict[str, float] = field(default_factory=dict[str, float])

    @property
    def top_hz(self) -> float:
        return TOP * self.rate_hz

    def spectrum_error_db(self) -> float | None:
        return self.prediction.spectrum_error_db(BAND_HZ, self.top_hz, BAND_HZ)

    def control_band_error_db(self) -> float | None:
        return self.prediction.band_error_db(*CONTROL_BAND)

    def summary(self) -> dict[str, Any]:
        """The numbers worth keeping: no samples, no positions."""
        p = self.prediction
        bands = [(a, a + 50.0) for a in np.arange(0.0, self.top_hz, 50.0)]
        return {
            "instance": self.instance,
            "rate_hz": round(self.rate_hz, 1),
            "seconds": round(p.seconds, 1),
            "windows": p.windows,
            "spectrum_error_db": _round(self.spectrum_error_db()),
            "control_band_error_db": _round(self.control_band_error_db()),
            "bands": [
                {
                    "from_hz": float(a),
                    "to_hz": float(min(b, self.top_hz)),
                    "pre_db": round(p.band_db("pre", a, min(b, self.top_hz)), 2),
                    "predicted_db": round(p.band_db("predicted", a, min(b, self.top_hz)), 2),
                    "logged_db": (
                        round(p.band_db("logged", a, min(b, self.top_hz)), 2)
                        if p.logged is not None
                        else None
                    ),
                }
                for a, b in bands
            ],
            "tracking": self.tracking,
            "phase_lag_deg": self.phase_lag_deg,
            "missing_sources": list(self.filters.missing),
        }


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 3)


def _clip(run: Run, start_us: int, end_us: int) -> Run | None:
    sel = (run.times_us >= start_us) & (run.times_us <= end_us)
    if sel.sum() < 2:
        return None
    t = run.times_us[sel]
    return Run(int(t[0]), int(t[-1]), run.rate_hz, run.samples[sel], t)


def _logged_centres(
    tables: Mapping[str, pa.Table], set_index: int, start_us: int, end_us: int
) -> float | None:
    for name, column in (("FCN", "CF1"), ("FCNS", "CF")):
        t = tables.get(name)
        if t is None or column not in t.column_names or "I" not in t.column_names:
            continue
        times = np.asarray(t.column("TimeUS").to_numpy(), np.int64)
        inst = np.asarray(t.column("I").to_numpy(), np.int64)
        sel = (times >= start_us) & (times <= end_us) & (inst == set_index)
        values = np.asarray(t.column(column).to_numpy(zero_copy_only=False), np.float64)[sel]
        values = values[np.isfinite(values)]
        if len(values):
            return float(np.median(values))
    return None


def check(
    params: Mapping[str, float],
    tables: Mapping[str, pa.Table],
    spans: Sequence[tuple[int, int]],
    imu_count: int | None,
    board_family: str | None,
) -> Check | None:
    """The filter model run on the flight's own settings over `spans` (steady hover),
    or None when the log has no pre-filter gyro data there."""
    gyr = tables.get("GYR")
    if gyr is None or gyr.num_rows == 0 or not spans:
        return None
    instances = sorted({int(i) for i in np.unique(gyr.column("I").to_numpy())})
    gyros = layout(int(params.get("INS_RAW_LOG_OPT", 0)), imu_count, instances)
    if not gyros.pre:
        return None
    primary = gyros.pre[0]
    leads = [r for a, b in spans for r in runs(gyr, primary, a - LEAD_US, b)]
    if not leads:
        return None
    rate = float(np.median([r.rate_hz for r in leads]))
    first, last = min(a for a, _ in spans), max(b for _, b in spans)
    filters = chain(params, rate, board_family, dict(tables), first - LEAD_US, last)
    loop_rate = float(params.get("SCHED_LOOP_RATE", 400))
    simulated: list[Run] = []
    pre: list[Run] = []
    for a, b in spans:
        for r in runs(gyr, primary, a - LEAD_US, b):
            out = _clip(simulate(filters, r, loop_rate), a, b)
            inside = _clip(r, a, b)
            if out is not None and inside is not None:
                simulated.append(out)
                pre.append(inside)
    predicted, before = welch(simulated), welch(pre)
    if predicted is None or before is None:
        return None
    logged = None
    if primary in gyros.post:
        logged = welch([r for a, b in spans for r in runs(gyr, gyros.post[primary], a, b)])
    prediction = Prediction(
        predicted.freqs,
        before.psd,
        predicted.psd,
        logged.psd if logged is not None and len(logged.freqs) == len(predicted.freqs) else None,
        predicted.windows,
        predicted.seconds,
    )
    now = filters.typical_sources(first, last)
    tracking = [
        {
            "set": s.notch.p.prefix,
            "mode": s.notch.p.mode,
            "source": s.sources.how,
            "source_hz": [round(v, 2) for v in now[k]],
            "model_centre_hz": (
                round(first_notch[0].center_hz, 2)
                if (first_notch := s.notch.notches(now[k]))
                else None
            ),
            "logged_centre_hz": _logged_centres(tables, k, first, last),
        }
        for k, s in enumerate(filters.sets)
    ]
    lag = phase_lag_deg(filters, CONTROL_HZ, now)
    return Check(
        primary,
        rate,
        prediction,
        filters,
        tracking,
        {f"{f:g}": round(v, 1) for f, v in zip(CONTROL_HZ, lag, strict=True)},
    )
