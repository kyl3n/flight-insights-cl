"""The measured wind's arithmetic, on flights made to a known answer: the drag model, the
wind and the lean in still air come back exactly from steady windows; each window's own
wind follows its drag; the windows themselves come right from the estimator's attitude
and velocity; and a flight that cannot tell the drag from the wind measures none."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from flight_insights.analyzers.wind import (
    G,
    Windows,
    blowing_from,
    body_rows,
    fit,
    measure,
    trim_deg,
    window_winds,
    windows,
)
from flight_insights.knowledge import load_pack

RULES: dict[str, Any] = dict(load_pack().json("rules/wind.json"))
Array = np.ndarray[Any, Any]


def drag_of(air: Array, k1: float, k2: float, bias: Array) -> Array:
    return -k1 * air - k2 * air * np.abs(air) + bias


def made(
    speeds: list[float], wind: Array, k1: float, k2: float, bias: Array, headings: int = 8,
    noise: float = 0.0, seed: int = 1,
) -> Windows:  # fmt: skip
    """Level windows (no tilt in the rotation, as the fit sees it) at these ground speeds,
    each on `headings` courses with the nose turned off the course."""
    rng = np.random.default_rng(seed)
    rows: list[tuple[Array, Array, Array]] = []
    for speed in speeds:
        for k in range(headings):
            course = 2 * math.pi * k / headings
            yaw = course + math.radians(30 * (k % 3))
            vg = speed * np.array([math.cos(course), math.sin(course)])
            turn = np.array([[math.cos(yaw), math.sin(yaw)], [-math.sin(yaw), math.cos(yaw)]])
            ground = turn @ vg
            air = ground - turn @ wind
            drag = drag_of(air, k1, k2, bias) + rng.normal(0, noise, 2)
            rows.append((drag, ground, turn))
    n = len(rows)
    return Windows(
        np.arange(n, dtype=np.int64) * 2_000_000, np.arange(1, n + 1, dtype=np.int64) * 2_000_000,
        np.array([r[0] for r in rows]), np.array([r[1] for r in rows]),
        np.array([r[2] for r in rows]), np.full(n, 20.0),
    )  # fmt: skip


WIND = np.array([2.0, -4.0])  # the air moving north-west of south: from 116.6 degrees
BIAS = np.array([0.08, -0.05])


def test_the_drag_the_wind_and_the_lean_come_back_exactly() -> None:
    w = made([0.0, 3.0, 6.0, 9.0], WIND, 0.25, 0.035, BIAS)
    f = fit(w, RULES)
    assert f is not None and f.quadratic
    assert (f.k1, f.k2) == pytest.approx((0.25, 0.035), abs=1e-9)
    assert f.wind == pytest.approx(WIND, abs=1e-9) and f.bias == pytest.approx(BIAS, abs=1e-9)
    assert window_winds(w, f) == pytest.approx(np.tile(WIND, (len(w), 1)), abs=1e-9)
    assert blowing_from(f.wind) == pytest.approx(math.degrees(math.atan2(4.0, -2.0)) % 360)
    assert trim_deg(f.bias) == pytest.approx(math.degrees(math.atan2(math.hypot(*BIAS), G)))


def test_rotor_drag_alone_keeps_the_linear_model() -> None:
    """With no square term in the air, the information criterion keeps the simpler model."""
    w = made([0.0, 3.0, 6.0, 9.0], WIND, 0.3, 0.0, BIAS, noise=0.01)
    f = fit(w, RULES)
    assert f is not None and not f.quadratic
    assert f.k1 == pytest.approx(0.3, rel=0.02) and f.wind == pytest.approx(WIND, abs=0.1)


def test_the_flights_wind_with_its_interval() -> None:
    w = made([0.0, 3.0, 6.0, 9.0], WIND, 0.25, 0.035, BIAS, headings=12, noise=0.02)
    m = measure(w, RULES)
    assert m is not None
    assert m.speed == pytest.approx(math.hypot(*WIND), abs=0.15)
    low, high = m.intervals["speed"]
    assert low <= math.hypot(*WIND) <= high and high - low < 0.5
    assert m.peak >= m.speed
    assert m.intervals["k1"][0] <= 0.25 <= m.intervals["k1"][1]


def test_a_hover_cannot_tell_the_drag_from_the_wind() -> None:
    w = made([0.0, 0.3], WIND, 0.25, 0.035, BIAS, headings=24)
    assert measure(w, RULES) is None  # its ground speeds do not span enough


def test_too_few_windows_measure_nothing() -> None:
    w = made([0.0, 4.0, 8.0], WIND, 0.25, 0.035, BIAS, headings=4)
    assert len(w) < int(RULES["min_windows"]) and measure(w, RULES) is None


def test_a_wild_flight_with_no_consistent_drag_measures_nothing() -> None:
    """Drag that does not follow the airspeed (here pure noise) leaves the interval wide."""
    w = made([0.0, 3.0, 6.0, 9.0], WIND, 0.25, 0.035, BIAS, headings=8)
    rng = np.random.default_rng(3)
    noisy = Windows(w.start_us, w.end_us, rng.normal(0, 3.0, w.drag.shape), w.ground, w.turn,
                    w.height)  # fmt: skip
    assert measure(noisy, RULES) is None


# --- the windows from the estimator's attitude and velocity --------------------------------


def attitude_for(drag: Array, yaw: float) -> tuple[float, float]:
    """Roll and pitch at which a steady aircraft feels this body-frame drag (m/s/s): the
    specific force in the body frame is the attitude applied to minus gravity."""
    pitch = math.asin(drag[0] / G)
    roll = math.asin(-drag[1] / (G * math.cos(pitch)))
    return roll, pitch


def ekf_series(wind: Array, k1: float, k2: float, bias: Array, speeds: tuple[float, ...]
               ) -> dict[str, Array]:  # fmt: skip
    """10 Hz primary-core rows: on the ground, a climb to 20 m, then 4 s legs at these
    speeds on eight courses, each holding the attitude the drag model asks for."""
    t: list[int] = []
    names = ("Roll", "Pitch", "Yaw", "VN", "VE", "VD", "PD")
    cols: dict[str, list[float]] = {n: [] for n in names}
    now = 0

    def row(roll: float, pitch: float, yaw: float, vn: float, ve: float, vd: float, pd: float
            ) -> None:  # fmt: skip
        nonlocal now
        t.append(now)
        now += 100_000
        values = (math.degrees(roll), math.degrees(pitch), math.degrees(yaw) % 360, vn, ve, vd, pd)
        for name, value in zip(names, values, strict=True):
            cols[name].append(value)

    for _ in range(20):
        row(0, 0, 0, 0, 0, 0, 0.0)
    for k in range(40):  # climbing at 5 m/s, not steady
        row(0, 0, 0, 0, 0, -5.0, -0.5 * k)
    for speed in speeds:
        for k in range(8):
            course = 2 * math.pi * k / 8
            yaw = course + math.radians(30 * (k % 3))
            vg = speed * np.array([math.cos(course), math.sin(course)])
            # the attitude depends on the airspeed in the body frame, which depends on the
            # attitude: a few rounds settle it
            roll = pitch = 0.0
            before = (math.inf, math.inf)
            for _ in range(60):
                bx, by = body_rows(np.array(roll), np.array(pitch), np.array(yaw))
                air3 = np.array([*(vg - wind), 0.0])
                air = np.array([bx @ air3, by @ air3])
                before = (roll, pitch)
                roll, pitch = attitude_for(drag_of(air, k1, k2, bias), yaw)
            assert max(abs(roll - before[0]), abs(pitch - before[1])) < 1e-12  # settled
            for _ in range(40):  # 4 s
                row(roll, pitch, yaw, float(vg[0]), float(vg[1]), 0.0, -20.0)
    return {"t": np.array(t, np.int64), **{n: np.array(v) for n, v in cols.items()}}


def test_windows_from_the_estimator_give_back_the_drag() -> None:
    """Tilted as the drag asks (up to about 25 degrees here), the estimator's attitude and
    velocity give back the model exactly."""
    wind = np.array([1.5, -2.0])
    e = ekf_series(wind, 0.25, 0.035, BIAS, (0.0, 2.0, 4.0, 6.0))
    w = windows(e, arm_us=int(e["t"][20]), disarm_us=int(e["t"][-1]) + 100_000, rules=RULES)
    assert len(w) == 4 * 8 * 2  # each 4 s leg is two windows; the climb is left out
    assert np.all(w.height == pytest.approx(20.0))
    f = fit(w, RULES)
    assert f is not None
    assert (f.k1, f.k2) == pytest.approx((0.25, 0.035), rel=1e-6)
    assert f.wind == pytest.approx(wind, abs=1e-6) and f.bias == pytest.approx(BIAS, abs=1e-6)
