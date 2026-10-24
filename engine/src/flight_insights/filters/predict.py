"""Predict the post-filter gyro spectrum from the pre-filter one (spec: The filter
analyzer in detail, step 4).

The gyro filter chain is the enabled harmonic notch sets in order, then the low-pass
(AP_InertialSensor_Backend::apply_gyro_filters). For each spectrum window the chain's
power gain |H(f)|^2 is averaged over the notch positions in force during the window,
and multiplies that window's pre-filter spectrum: the method of ArduPilot's Filter
Review tool, window by window. Compared with the post-filter samples the firmware
logged over the same stretches, this checks the model; applied to candidate settings,
it predicts what they would do.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from flight_insights.filters.biquad import Biquad, Complex, Floats, lowpass
from flight_insights.filters.harmonic import LOOP_RATE, MULTI_SOURCE, HarmonicNotch, NotchParams
from flight_insights.filters.tracking import Sources, sources, update_period_us
from flight_insights.spectral.welch import Spectrum, Windows

SETS = ("INS_HNTCH_", "INS_HNTC2_")
DECIMALS = 2  # source frequencies equal to 0.01 Hz share one response


@dataclass(frozen=True)
class NotchSet:
    notch: HarmonicNotch
    sources: Sources


@dataclass(frozen=True)
class FilterChain:
    sample_hz: float
    lowpass: Biquad | None
    sets: tuple[NotchSet, ...]
    missing: tuple[str, ...] = ()  # enabled sets whose sources the log lacks

    def response(self, freqs: Floats, now: Sequence[Sequence[float]]) -> Complex:
        """H(f) with each set's notches at the given source frequencies."""
        h: Complex = np.ones(len(freqs), np.complex128)
        for s, src in zip(self.sets, now, strict=True):
            for n in s.notch.notches(list(src)):
                h = h * n.filter.response(freqs, self.sample_hz)
        if self.lowpass is not None:
            h = h * self.lowpass.response(freqs, self.sample_hz)
        return h

    @property
    def inputs(self) -> str:
        """What the notches followed: "none" without a notch, "exact" when every set's
        inputs are known exactly, else "rebuilt"."""
        if self.missing:
            return "rebuilt"  # a set whose source the log lacks is not modelled at all
        if not self.sets:
            return "none"
        return "exact" if all(s.sources.exact for s in self.sets) else "rebuilt"

    def power_gain(self, freqs: Floats, start_us: int, end_us: int) -> Floats:
        """|H(f)|^2 averaged over the notch positions in force between two times."""
        g: Floats = np.ones(len(freqs))
        for s in self.sets:
            rows = np.round(s.sources.sampled(start_us, end_us), DECIMALS)
            unique, counts = np.unique(rows, axis=0, return_counts=True)
            acc = np.zeros(len(freqs))
            for row, count in zip(unique, counts, strict=True):
                h: Complex = np.ones(len(freqs), np.complex128)
                for n in s.notch.notches(list(row)):
                    h = h * n.filter.response(freqs, self.sample_hz)
                acc += count * np.abs(h) ** 2
            g = g * acc / counts.sum()
        if self.lowpass is not None:
            g = g * np.abs(self.lowpass.response(freqs, self.sample_hz)) ** 2
        return g

    def typical_sources(self, start_us: int, end_us: int) -> list[list[float]]:
        """Each set's median source frequencies between two times."""
        return [
            [float(v) for v in np.median(s.sources.between(start_us, end_us), axis=0)]
            for s in self.sets
        ]


def follows_as_flown(flown: NotchParams, p: NotchParams) -> bool:
    """Whether a set, as changed, still follows what it followed in flight: the same
    mode, reference, frequency, minimum ratio, sources and update rate. Only then are
    the inputs the firmware logged in flight the candidate's inputs too."""
    same = ("mode", "ref", "freq", "fm_rat")
    bits = MULTI_SOURCE | LOOP_RATE
    return all(getattr(flown, k) == getattr(p, k) for k in same) and (
        flown.opts & bits == p.opts & bits
    )


def chain(
    params: Mapping[str, float],
    sample_hz: float,
    board_family: str | None,
    tables: dict[str, pa.Table],
    start_us: int,
    end_us: int,
    overrides: Mapping[str, Mapping[str, float]] | None = None,
) -> FilterChain:
    """The gyro filter chain the parameters set up for one gyro at `sample_hz`.
    `overrides` changes a set's parameters, by prefix, for candidate settings."""
    sets: list[NotchSet] = []
    missing: list[str] = []
    loop_rate = float(params.get("SCHED_LOOP_RATE", 400))
    for index, prefix in enumerate(SETS):
        flown = p = NotchParams.read(params, prefix)
        if overrides and prefix in overrides:
            p = p.with_changes(**overrides[prefix])
        if not p.enable or not p.num_harmonics:
            continue
        notch = HarmonicNotch(p, sample_hz, board_family)
        period = update_period_us(p.opts, loop_rate)
        src = sources(notch, tables, start_us, end_us, index, period, follows_as_flown(flown, p))
        if src is None:
            missing.append(prefix)
            continue
        sets.append(NotchSet(notch, src))
    cutoff = float(params.get("INS_GYRO_FILTER", 0))
    if overrides and "INS_GYRO_FILTER" in overrides:
        cutoff = float(overrides["INS_GYRO_FILTER"]["value"])
    return FilterChain(sample_hz, lowpass(sample_hz, cutoff), tuple(sets), tuple(missing))


@dataclass(frozen=True)
class Prediction:
    freqs: Floats
    pre: Floats  # (3, F) (deg/s)^2/Hz
    predicted: Floats
    logged: Floats | None  # the firmware's post-filter samples, when logged
    windows: int
    seconds: float

    def band_db(self, which: str, low_hz: float, high_hz: float) -> float:
        """Total power of all three axes in [low, high), dB re (deg/s)^2."""
        psd = {"pre": self.pre, "predicted": self.predicted, "logged": self.logged}[which]
        assert psd is not None
        sel = (self.freqs >= low_hz) & (self.freqs < high_hz)
        df = float(self.freqs[1] - self.freqs[0])
        return float(10 * np.log10(max(float(psd[:, sel].sum()) * df, 1e-30)))

    def band_error_db(self, low_hz: float, high_hz: float) -> float | None:
        """Predicted minus logged power in a band, dB."""
        if self.logged is None:
            return None
        return self.band_db("predicted", low_hz, high_hz) - self.band_db("logged", low_hz, high_hz)

    def spectrum_error_db(
        self, low_hz: float, high_hz: float, band_hz: float = 10.0
    ) -> float | None:
        """RMS over consecutive bands of `band_hz` of the predicted minus logged band
        power, dB: how well the predicted spectrum's shape matches."""
        if self.logged is None:
            return None
        edges = np.arange(low_hz, high_hz, band_hz)
        errors = [self.band_error_db(a, a + band_hz) for a in edges]
        values = np.array([e for e in errors if e is not None])
        return float(np.sqrt(np.mean(values**2))) if len(values) else None


def predict(pre: Windows, filters: FilterChain, logged: Spectrum | None) -> Prediction:
    """The post-filter spectrum `filters` would give, window by window."""
    predicted = np.zeros_like(pre.psd[0])
    for k in range(len(pre.psd)):
        gain = filters.power_gain(pre.freqs, int(pre.start_us[k]), int(pre.end_us[k]))
        predicted += pre.psd[k] * gain[None, :]
    predicted /= len(pre.psd)
    logged_psd = None
    if logged is not None and len(logged.freqs) == len(pre.freqs):
        logged_psd = logged.psd
    return Prediction(
        pre.freqs, pre.psd.mean(axis=0), predicted, logged_psd, len(pre.psd), pre.seconds
    )


def phase_lag_deg(
    filters: FilterChain, freqs: Sequence[float], now: Sequence[Sequence[float]]
) -> list[float]:
    """Phase lag the chain adds at each frequency, degrees (positive is late)."""
    h = filters.response(np.asarray(freqs, np.float64), now)
    return [float(-np.degrees(np.angle(v))) for v in h]
