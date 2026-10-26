"""Small statistics the analyzers share.

Every random draw is seeded, so the same log gives the same interval on every laptop
and every run (spec: Storage, "Analysis run": results are a pure function of the log
and the versions).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

LEVEL = 0.95
RESAMPLES = 2000


def bootstrap_mean(
    values: Sequence[float], seed: int, level: float = LEVEL, resamples: int = RESAMPLES
) -> tuple[float, float]:
    """The percentile bootstrap interval of the mean: resample the values with
    replacement, take each resample's mean, and keep the central `level` of them."""
    x = np.asarray(values, dtype=np.float64)
    if len(x) == 0:
        raise ValueError("no values to resample")
    rng = np.random.default_rng(seed)
    means = x[rng.integers(0, len(x), size=(resamples, len(x)))].mean(axis=1)
    tail = (1.0 - level) / 2.0
    low, high = np.quantile(means, [tail, 1.0 - tail])
    return float(low), float(high)
