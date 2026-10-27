"""A first-order lag with dead time fitted to a measured frequency response (spec:
Copter analyzers, heavy-lift, "Actuator response"):

    H(f) = K e^(-i w D) / (1 + i w T),  w = 2 pi f

T is the time constant and D the dead time; a step's response reaches 63% of its final
value after T + D, the response time. At low frequency the two add the same phase, so
they trade against each other and T + D is what the data fixes best; the gain falling
off with frequency is what tells T from D.

The fit is weighted least squares over the bins of a band whose estimated random error
(spectral/response.py) is small enough, each bin weighted by one over its error
squared, the gain K complex and solved for each (T, D) in closed form, over a grid of T
and D. The grid's model is computed once for a band, so resampling the windows for an
interval costs only the sums.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.spectral.response import Response

Floats = np.ndarray[tuple[int, ...], np.dtype[np.float64]]
Complex = np.ndarray[Any, np.dtype[np.complex128]]
ERROR_FLOOR = 0.02  # no bin weighs more than one 2% off


@dataclass(frozen=True)
class Lag:
    time_constant_s: float
    dead_time_s: float
    gain: float  # |K|, in the response's units
    bins: int  # how many bins the fit used

    @property
    def response_s(self) -> float:
        """When a step reaches 63% of its final value."""
        return self.time_constant_s + self.dead_time_s


class LagFit:
    """The model over a grid of T and D for the bins of one band, kept for refitting."""

    def __init__(
        self, freqs: Floats, band: tuple[float, float], t_max: float, t_step: float,
        d_max: float, d_step: float,
    ) -> None:  # fmt: skip
        self.pick = np.flatnonzero((freqs >= band[0]) & (freqs <= band[1]))
        w = 2.0 * np.pi * freqs[self.pick]
        self.t = np.arange(0.0, t_max + t_step / 2, t_step)
        self.d = np.arange(0.0, d_max + d_step / 2, d_step)
        model = (
            np.exp(-1j * np.outer(self.d, w))[None, :, :]
            / (1.0 + 1j * np.outer(self.t, w))[:, None, :]
        )  # (T, D, bins)
        self.model: Complex = model.reshape(len(self.t) * len(self.d), len(w))
        self.power: Floats = np.abs(self.model) ** 2

    def fit(self, r: Response, max_error: float, min_bins: int) -> Lag | None:
        """The best T and D for a response, or None with fewer than `min_bins` good bins."""
        error = r.error[self.pick]
        good = np.isfinite(error) & (error <= max_error)
        if int(good.sum()) < min_bins:
            return None
        weight = np.where(good, 1.0 / np.maximum(error, ERROR_FLOOR) ** 2, 0.0)
        h = np.where(good, r.h[self.pick], 0.0)
        cross = np.conj(self.model) @ (weight * h)  # sum w conj(m) h, for every (T, D)
        norm = self.power @ weight  # sum w |m|^2
        # the weighted residual with the best K, less the constant sum w |h|^2
        best = int(np.argmax(np.abs(cross) ** 2 / norm))
        k = cross[best] / norm[best]
        ti, di = divmod(best, len(self.d))
        return Lag(float(self.t[ti]), float(self.d[di]), float(abs(k)), int(good.sum()))
