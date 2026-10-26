"""A rate loop inside an attitude loop, simulated for the rate-loop analyzer's tests.

The closed rate loop is a second-order system with dead time, driven by a rate target
the attitude loop makes from a shaped attitude target (the pilot's stick doublets) and
the measured attitude, as ArduPilot's attitude controller does: target = the attitude
target's rate + P x (attitude target - attitude). Gusts add a disturbance to the
measured rate, which the attitude loop then feeds back into the target. Every number the
analyzer should find is known in closed form or from the same integration."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Any

import numpy as np

HZ = 400.0
SUB = 8  # integration steps per sample
ANGLE_P = 4.5  # ATC_ANG_RLL_P's default
Array = np.ndarray[Any, Any]


@dataclass(frozen=True)
class Loop:
    zeta: float
    fn_hz: float
    delay_s: float

    def response(self, f: Array) -> Array:
        """The closed rate loop's frequency response."""
        w, wn = 2 * np.pi * f, 2 * np.pi * self.fn_hz
        return wn**2 / (wn**2 - w**2 + 2j * self.zeta * wn * w) * np.exp(-1j * w * self.delay_s)

    def bandwidth_hz(self) -> float:
        f = np.linspace(0.05, 60, 120_000)
        return float(f[np.argmax(np.abs(self.response(f)) < 1 / np.sqrt(2))])

    def step(self, seconds: float = 1.5) -> Array:
        """Its step response, from the same integration as the flights."""
        return _integrate(self, np.ones(int(seconds * HZ)))


def _integrate(loop: Loop, target: Array) -> Array:
    wn, dt, d = 2 * np.pi * loop.fn_hz, 1 / HZ / SUB, round(loop.delay_s * HZ)
    out = np.zeros(len(target))
    p = v = 0.0
    for i in range(len(target)):
        u = target[i - d] if i >= d else 0.0
        for _ in range(SUB):
            v += (wn * wn * (u - p) - 2 * loop.zeta * wn * v) * dt
            p += v * dt
        out[i] = p
    return out


@cache
def fly(loop: Loop, gust_dps: float = 0.0, seconds: float = 100.0, seed: int = 0) -> tuple[
    Array, Array, Array
]:  # fmt: skip
    """(attitude target in deg, rate target, measured rate in deg/s) at HZ, from t = 0: a
    doublet every 5 s from 3 s on, 1 s each way, at 15 or 25 degrees of lean, shaped by
    two 0.1 s lags as input shaping smooths the stick."""
    rng = np.random.default_rng(seed)
    n = int(seconds * HZ)
    t = np.arange(n) / HZ
    stick = np.zeros(n)
    for k, t0 in enumerate(np.arange(3.0, seconds - 5, 5.0)):
        lean = 25.0 if k % 3 == 0 else 15.0
        stick[(t >= t0) & (t < t0 + 1)] = lean
        stick[(t >= t0 + 1) & (t < t0 + 2)] = -lean
    a = (1 / HZ) / (0.1 + 1 / HZ)
    shaped, attitude_target = np.zeros(n), np.zeros(n)
    for i in range(1, n):
        shaped[i] = shaped[i - 1] + a * (stick[i] - shaped[i - 1])
        attitude_target[i] = attitude_target[i - 1] + a * (shaped[i] - attitude_target[i - 1])
    feed = np.gradient(attitude_target) * HZ
    noise = rng.normal(0, 1, n)
    b = (1 / HZ) / (1 / (2 * np.pi * 4.0) + 1 / HZ)  # gusts low-passed at 4 Hz
    gust = np.zeros(n)
    for i in range(1, n):
        gust[i] = gust[i - 1] + b * (noise[i] - gust[i - 1])
    gust *= gust_dps / max(float(gust.std()), 1e-12)
    gust += rng.normal(0, 0.5, n)  # gyro noise
    wn, dt, d = 2 * np.pi * loop.fn_hz, 1 / HZ / SUB, round(loop.delay_s * HZ)
    target, rate = np.zeros(n), np.zeros(n)
    attitude = p = v = 0.0
    for i in range(n):
        target[i] = feed[i] + ANGLE_P * (attitude_target[i] - attitude)
        u = target[i - d] if i >= d else 0.0
        for _ in range(SUB):
            v += (wn * wn * (u - p) - 2 * loop.zeta * wn * v) * dt
            p += v * dt
        rate[i] = p + gust[i]
        attitude += rate[i] / HZ
    return attitude_target, target, rate
