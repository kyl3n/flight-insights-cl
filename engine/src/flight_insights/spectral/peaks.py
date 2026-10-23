"""Spectral peaks and the harmonics of a fundamental.

A peak is a local maximum that stands a given number of dB above the local noise
floor, the running median of the spectrum in dB over a band around it. Peaks closer
than two resolution steps are one peak.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from flight_insights.spectral.gyro import Floats
from flight_insights.spectral.welch import Spectrum

FLOOR_HZ = 20.0  # width of the band the floor is the median of
PROMINENCE_DB = 6.0


@dataclass(frozen=True)
class Peak:
    freq_hz: float
    level_db: float  # 10 log10 of the density, (deg/s)^2/Hz
    prominence_db: float  # above the local floor


def db(x: Floats) -> Floats:
    return np.asarray(10.0 * np.log10(np.maximum(x, 1e-30)), np.float64)


def _floor(level: Floats, half: int) -> Floats:
    padded = np.pad(level, half, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * half + 1)
    return np.asarray(np.median(windows, axis=1), np.float64)


def peaks(
    spectrum: Spectrum,
    low_hz: float,
    high_hz: float,
    prominence_db: float = PROMINENCE_DB,
    axes: tuple[int, ...] = (0, 1, 2),
) -> list[Peak]:
    """Peaks of the summed axes' spectrum in [low, high), strongest first."""
    level = db(spectrum.psd[list(axes)].sum(axis=0))
    half = max(1, int(FLOOR_HZ / 2 / spectrum.resolution_hz))
    above = level - _floor(level, half)
    f = spectrum.freqs
    inside = (f >= low_hz) & (f < high_hz)
    local = np.zeros(len(f), bool)
    local[1:-1] = (level[1:-1] >= level[:-2]) & (level[1:-1] > level[2:])
    found = [int(k) for k in np.flatnonzero(local & inside & (above >= prominence_db))]
    found.sort(key=lambda k: -level[k])
    kept: list[int] = []
    for k in found:
        if all(abs(k - j) > 2 for j in kept):
            kept.append(k)
    return [Peak(float(f[k]), float(level[k]), float(above[k])) for k in kept]


def harmonic_of(peak_hz: float, fundamental_hz: float, tolerance: float = 0.05) -> int | None:
    """Which harmonic of `fundamental_hz` a peak is (1 is the fundamental), or None."""
    if fundamental_hz <= 0:
        return None
    k = round(peak_hz / fundamental_hz)
    if k >= 1 and abs(peak_hz - k * fundamental_hz) <= tolerance * k * fundamental_hz:
        return k
    return None
