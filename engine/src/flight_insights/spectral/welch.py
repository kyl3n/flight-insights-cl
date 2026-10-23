"""Power spectral density by Welch's method, in numpy alone.

Hann windows with 50% overlap, each with its mean removed, averaged; one-sided
density in (deg/s)^2/Hz, so integrating over a band gives the band's power. Windows
are taken only inside contiguous runs, so a lost sample never shifts a window.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from flight_insights.spectral.gyro import Floats, Run

RATE_TOLERANCE = 0.01  # runs this far from the median rate are another sensor setting


@dataclass(frozen=True)
class Spectrum:
    freqs: Floats  # Hz
    psd: Floats  # (3, len(freqs)): X, Y, Z in (deg/s)^2/Hz
    rate_hz: float
    windows: int
    seconds: float  # of samples the windows cover, overlap not counted twice

    @property
    def resolution_hz(self) -> float:
        return float(self.freqs[1] - self.freqs[0])

    def band_power(self, low_hz: float, high_hz: float) -> Floats:
        """Power per axis in [low, high), (deg/s)^2."""
        sel = (self.freqs >= low_hz) & (self.freqs < high_hz)
        return np.asarray(self.psd[:, sel].sum(axis=1) * self.resolution_hz, np.float64)


def segment_length(rate_hz: float, resolution_hz: float) -> int:
    """The power of two nearest rate / resolution."""
    return int(2 ** round(np.log2(rate_hz / resolution_hz)))


@dataclass(frozen=True)
class Windows:
    """Each window's own spectrum, and the log times it spans."""

    freqs: Floats
    psd: Floats  # (windows, 3, len(freqs))
    start_us: np.ndarray[tuple[int], np.dtype[np.int64]]
    end_us: np.ndarray[tuple[int], np.dtype[np.int64]]
    rate_hz: float
    seconds: float

    def mean(self) -> Spectrum:
        return Spectrum(
            self.freqs, self.psd.mean(axis=0), self.rate_hz, len(self.psd), self.seconds
        )


def windows(runs: Sequence[Run], resolution_hz: float = 1.0) -> Windows | None:
    """Every whole window in `runs`, each with its spectrum, or None if none fits."""
    if not runs:
        return None
    rate = float(np.median([r.rate_hz for r in runs]))
    usable = [r for r in runs if abs(r.rate_hz - rate) <= RATE_TOLERANCE * rate]
    n = segment_length(rate, resolution_hz)
    step = n // 2
    window = np.hanning(n)
    scale = 1.0 / (rate * float((window**2).sum()))
    spectra: list[Floats] = []
    starts_us: list[int] = []
    ends_us: list[int] = []
    seconds = 0.0
    for r in usable:
        x = r.samples
        if len(x) < n:
            continue
        starts = range(0, len(x) - n + 1, step)
        for s in starts:
            seg = x[s : s + n]
            seg = (seg - seg.mean(axis=0)) * window[:, None]
            spectra.append(np.abs(np.fft.rfft(seg, axis=0).T) ** 2 * scale)
            starts_us.append(int(r.times_us[s]))
            ends_us.append(int(r.times_us[s + n - 1]))
        seconds += (starts[-1] + n) / r.rate_hz
    if not spectra:
        return None
    psd = np.stack(spectra)
    psd[:, :, 1:-1] *= 2  # one-sided: fold negative frequencies (not DC or Nyquist)
    return Windows(
        np.fft.rfftfreq(n, 1.0 / rate),
        psd,
        np.asarray(starts_us, np.int64),
        np.asarray(ends_us, np.int64),
        rate,
        seconds,
    )


def welch(runs: Sequence[Run], resolution_hz: float = 1.0) -> Spectrum | None:
    """The averaged spectrum of every whole window in `runs`, or None if none fits."""
    w = windows(runs, resolution_hz)
    return w.mean() if w is not None else None
