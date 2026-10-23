"""ArduPilot's gyro filters as transfer functions (libraries/Filter, 4.7.0).

Both filters are biquads, H(z) = (b0 + b1 z^-1 + b2 z^-2) / (1 + a1 z^-1 + a2 z^-2),
with the coefficients ArduPilot computes:

- LowPassFilter2p (DigitalBiquadFilter::compute_params): a second-order Butterworth
  from the bilinear transform with prewarping, its cutoff held at or below 40% of
  the sample rate; a zero cutoff passes the signal through.
- NotchFilter (calculate_A_and_Q, init_with_A_and_Q): the notch's depth A from its
  attenuation and its quality Q from centre frequency and bandwidth.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

Floats = np.ndarray[tuple[int, ...], np.dtype[np.float64]]
Complex = np.ndarray[tuple[int, ...], np.dtype[np.complex128]]


@dataclass(frozen=True)
class Biquad:
    b0: float
    b1: float
    b2: float
    a1: float
    a2: float

    def response(self, freqs: Floats, sample_hz: float) -> Complex:
        """H(e^jw) at each frequency."""
        z1 = np.exp(-2j * np.pi * np.asarray(freqs, np.float64) / sample_hz).astype(np.complex128)
        z2 = z1 * z1
        h = (self.b0 + self.b1 * z1 + self.b2 * z2) / (1.0 + self.a1 * z1 + self.a2 * z2)
        return np.asarray(h, np.complex128)


def lowpass(sample_hz: float, cutoff_hz: float) -> Biquad | None:
    """LowPassFilter2p at `cutoff_hz`, or None for a pass-through (cutoff 0)."""
    cutoff = min(cutoff_hz, sample_hz * 0.4)
    if cutoff <= 0 or sample_hz <= 0:
        return None
    ohm = math.tan(math.pi / (sample_hz / cutoff))
    c = 1.0 + 2.0 * math.cos(math.pi / 4.0) * ohm + ohm * ohm
    b0 = ohm * ohm / c
    a1 = 2.0 * (ohm * ohm - 1.0) / c
    a2 = (1.0 - 2.0 * math.cos(math.pi / 4.0) * ohm + ohm * ohm) / c
    return Biquad(b0, 2.0 * b0, b0, a1, a2)


def notch_a_q(center_hz: float, bandwidth_hz: float, attenuation_db: float) -> tuple[float, float]:
    """NotchFilter::calculate_A_and_Q."""
    a = 10.0 ** (-attenuation_db / 40.0)
    if center_hz > 0.5 * bandwidth_hz:
        octaves = math.log2(center_hz / (center_hz - bandwidth_hz / 2.0)) * 2.0
        q = math.sqrt(2.0**octaves) / (2.0**octaves - 1.0)
    else:
        q = 0.0
    return a, q


def notch(sample_hz: float, center_hz: float, a: float, q: float) -> Biquad | None:
    """NotchFilter::init_with_A_and_Q, or None where the firmware leaves it off."""
    if not (center_hz > 0 and center_hz < 0.5 * sample_hz and q > 0):
        return None
    omega = 2.0 * math.pi * center_hz / sample_hz
    alpha = math.sin(omega) / (2.0 * q)
    a0_inv = 1.0 / (1.0 + alpha)
    b0 = (1.0 + alpha * a * a) * a0_inv
    b1 = -2.0 * math.cos(omega) * a0_inv
    b2 = (1.0 - alpha * a * a) * a0_inv
    return Biquad(b0, b1, b2, b1, (1.0 - alpha) * a0_inv)
