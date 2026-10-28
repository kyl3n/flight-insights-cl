"""The ESC thermal model against a known answer: telemetry made from
`dT/dt = (T_air + k I^2 - T) / tau` under a changing current, read in whole degrees with a
little noise, gives tau, k and the air's temperature back; held at a current it settles at
`T_air + k I^2`; and a flight whose current never changes gives no model."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import flight, table

from flight_insights.analyzers import Registry
from flight_insights.knowledge import load_pack
from flight_insights.performance.thermal import fit, from_flight

RULES: dict[str, Any] = dict(Registry.load(load_pack()).rules["performance"])
TAU, K, AIR = 90.0, 0.012, 24.0
HZ = 10


def made(currents: Sequence[tuple[float, float]], seed: int = 3) -> tuple[Any, Any, Any]:
    """(seconds, amps) pieces from the air's temperature; temperature in whole degrees."""
    rng = np.random.default_rng(seed)
    t: list[float] = []
    amps: list[float] = []
    temp: list[float] = []
    now, hot, dt = 0.0, AIR, 1.0 / HZ
    for seconds, a in currents:
        for _ in range(round(seconds * HZ)):
            t.append(now)
            amps.append(a + rng.normal(0, 0.3))
            temp.append(hot)
            hot += (AIR + K * a * a - hot) / TAU * dt
            now += dt
    shown = np.round(np.array(temp) + rng.normal(0, 0.2, len(temp)))
    return np.array(t) * 1e6, shown, np.array(amps)


PROFILE = [(60, 5.0), (120, 35.0), (60, 15.0), (120, 45.0), (90, 10.0)]


def test_tau_k_and_the_air_come_back() -> None:
    t, temp, amps = made(PROFILE)
    m = fit(t.astype(np.int64), temp, amps, RULES)
    assert m is not None
    assert m.tau_s == pytest.approx(TAU, rel=0.03)
    assert m.k == pytest.approx(K, rel=0.03)
    assert m.air_c == pytest.approx(AIR, abs=0.5)
    assert m.current_max_a == pytest.approx(45.0, abs=1.5)


def test_held_at_a_current_it_settles_and_rises_as_the_closed_form() -> None:
    t, temp, amps = made(PROFILE)
    m = fit(t.astype(np.int64), temp, amps, RULES)
    assert m is not None
    assert m.steady(50.0) == pytest.approx(m.air_c + m.k * 2500.0)
    start = 40.0
    half = m.after(50.0, m.tau_s, start)
    assert half == pytest.approx(m.steady(50.0) + (start - m.steady(50.0)) / math.e)
    assert m.after(50.0, 0.0, start) == pytest.approx(start)


def test_a_steady_current_cannot_tell_the_heat_from_the_air() -> None:
    t, temp, amps = made([(400, 20.0)])
    assert fit(t.astype(np.int64), temp, amps, RULES) is None


def test_each_esc_of_a_flight_and_none_from_the_simulator() -> None:
    t, temp, amps = made(PROFILE)
    times = (t + 1_000_000).astype(np.int64).tolist()
    n = len(times)
    esc = table(times * 2, {
        "Instance": (pa.uint8(), [0] * n + [1] * n),
        "Temp": (pa.float32(), temp.tolist() + (temp + 5).tolist()),
        "Curr": (pa.float32(), amps.tolist() * 2),
    }, {"Temp": {b"unit": b"degC", b"multiplier": b"1.0"},
        "Curr": {b"unit": b"A", b"multiplier": b"1.0"}})  # fmt: skip
    f = flight({"ESC": esc}, times[0], times[-1])
    found = from_flight(f, RULES)
    assert [m.instance for m in found] == [0, 1]
    assert found[1].air_c - found[0].air_c == pytest.approx(5.0, abs=0.5)
    sim = flight({"ESC": esc}, times[0], times[-1],
                 fingerprint={"vehicle": "copter", "board": {"hal": "SITL"}})  # fmt: skip
    assert from_flight(sim, RULES) == []
