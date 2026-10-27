"""The wind the aircraft measured itself (spec: Weather and wind, "The aircraft"; rules in
`knowledge/rules/wind.json`).

A multirotor holding a course leans into the air going past it. Its rotors are pushed
back in proportion to the airspeed (rotor momentum drag) and its frame in proportion to
the airspeed's square (bluff-body drag); thrust has no forward or sideways part, so in
steady flight the forward and sideways specific force in the body frame is the drag.
The estimator gives it without trusting the accelerometers' level: its attitude applied
to its velocity's change, less gravity.

- **Windows.** The flight is cut into 2 s windows of the primary estimator core (XKF1),
  each kept when it is steady: well above take-off, hardly accelerating or climbing, and
  not leaning hard. In each, the drag is the mean body-frame specific force, and the
  ground velocity is taken into the body frame with the window's own attitude.
- **The model, per body axis:** drag = -k1 v - k2 v|v| + b, where v is the airspeed
  (ground velocity less the wind) and b the lean the aircraft needs in still air (its
  centre of gravity, the accelerometers' level).
- **One flight that changes speed and heading measures them all at once:** the wind (one
  vector for the flight), k1, k2 and b, by least squares: the linear model first, which
  is exact algebra, then Gauss-Newton with the quadratic term, kept only when it lowers
  the Bayesian information criterion. A flight that only hovers, or holds one speed and
  heading, cannot tell its drag from the wind, and measures none.
- **Each window's own wind:** its airspeed from its drag, inverted per axis, taken from
  its ground velocity. The flight's wind is their mean speed, its peak their 95th
  percentile, each with an interval from a seeded bootstrap over the windows.
- **The estimator's own wind:** with drag fusion on, EKF3 estimates the wind too
  (XKF2 VWN, VWE); it is given beside, over the same windows.

Checked in ArduPilot's simulator, whose frame model has both kinds of drag: a flight of
legs at 3, 6 and 9 m/s on eight headings, in a set wind, measures it within a few
percent and its direction within a degree or two (tests/test_wind_fixture.py).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import (
    Analyzer,
    Evidence,
    MetricValue,
    Result,
    Segment,
)
from flight_insights.analyzers.no_gnss_readiness import drag_fusion
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si

Array = np.ndarray[Any, Any]
G = 9.80665
EKF_FIELDS = ("Roll", "Pitch", "Yaw", "VN", "VE", "VD", "PD")


@dataclass(frozen=True)
class Windows:
    """The steady windows, one row each."""

    start_us: Array
    end_us: Array
    drag: Array  # (n, 2) body x, y specific force, m/s/s
    ground: Array  # (n, 2) the ground velocity in the body frame, m/s
    turn: Array  # (n, 2, 2) earth north, east to body x, y
    height: Array  # (n,) above take-off, m

    def take(self, rows: Array) -> Windows:
        return Windows(self.start_us[rows], self.end_us[rows], self.drag[rows],
                       self.ground[rows], self.turn[rows], self.height[rows])  # fmt: skip

    def __len__(self) -> int:
        return len(self.start_us)


@dataclass(frozen=True)
class Fit:
    k1: float  # 1/s
    k2: float  # 1/m
    wind: Array  # (2,) north, east, m/s: where the air goes
    bias: Array  # (2,) body x, y, m/s/s
    quadratic: bool
    rms: float  # of the drag residuals, m/s/s


def _ints(values: Any) -> Array:
    return np.asarray(values, dtype=np.int64)


def _primary(flight: FlightData, t: Array, core: Array) -> Array:
    """Which rows are the primary core's: XKF4.PI as last reported, else core 0."""
    x4 = flight.table("XKF4", ["TimeUS", "C", "PI"])
    if x4 is None or not x4.num_rows or "PI" not in x4.column_names:
        return core == 0
    t4, c4, pi = (_ints(x4.column(n).to_numpy()) for n in ("TimeUS", "C", "PI"))
    rows = c4 == pi
    if not rows.any():
        return core == 0
    pt, primary = t4[rows], pi[rows]
    k = np.clip(np.searchsorted(pt, t, side="right") - 1, 0, len(pt) - 1)
    return core == primary[k]


def ekf(flight: FlightData) -> dict[str, Array] | None:
    """The primary core's rows of XKF1, in time order."""
    x1 = flight.table("XKF1", ["TimeUS", "C", *EKF_FIELDS])
    if x1 is None or not x1.num_rows or not set(EKF_FIELDS) <= set(x1.column_names):
        return None
    t = _ints(x1.column("TimeUS").to_numpy())
    keep = _primary(flight, t, _ints(x1.column("C").to_numpy()))
    out = {"t": t[keep]}
    for name in EKF_FIELDS:
        out[name] = si.values(x1, name)[keep]
    order = np.argsort(out["t"], kind="stable")
    return {k: v[order] for k, v in out.items()}


def body_rows(roll: Array, pitch: Array, yaw: Array) -> tuple[Array, Array]:
    """The body x and y axes in north, east, down, for each attitude (radians): a vector
    in the body frame is these rows dotted with it in the earth frame."""
    cr, sr, cp, sp, cy, sy = (np.cos(roll), np.sin(roll), np.cos(pitch), np.sin(pitch),
                              np.cos(yaw), np.sin(yaw))  # fmt: skip
    x = np.stack([cp * cy, cp * sy, -sp], axis=-1)
    y = np.stack([sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, sr * cp], axis=-1)
    return x, y


def _empty() -> Windows:
    return Windows(*(np.zeros((0, *shape)) for shape in ((), (), (2,), (2,), (2, 2), ())))


def windows(e: Mapping[str, Array], arm_us: int, disarm_us: int, rules: Mapping[str, Any]
            ) -> Windows:  # fmt: skip
    t = e["t"]
    flown = (t >= arm_us) & (t <= disarm_us)
    if not flown.any():
        return _empty()
    ground_pd = float(e["PD"][np.argmax(flown)])
    step = int(float(rules["window_s"]) * 1e6)
    edges = np.arange(arm_us, disarm_us - step + 1, step)
    roll, pitch, yaw = (np.radians(e[n]) for n in ("Roll", "Pitch", "Yaw"))
    bx, by = body_rows(roll, pitch, yaw)
    rows: list[tuple[int, int, Array, Array, Array, float]] = []
    lo = np.searchsorted(t, edges, side="left")
    hi = np.searchsorted(t, edges + step, side="left")
    for start, a, b in zip(edges, lo, hi, strict=True):
        if b - a < int(rules["min_samples"]):
            continue
        s = slice(a, b)
        tt = (t[s] - t[s].mean()) / 1e6
        if np.ptp(tt) <= 0:
            continue
        acc = np.array([np.polyfit(tt, e[n][s], 1)[0] for n in ("VN", "VE", "VD")])
        vel = np.stack([e["VN"][s], e["VE"][s], e["VD"][s]], axis=-1)
        height = -(float(np.mean(e["PD"][s])) - ground_pd)
        tilt = np.degrees(np.arccos(np.clip(np.cos(roll[s]) * np.cos(pitch[s]), -1, 1)))
        if (height < float(rules["min_height_m"])
                or math.hypot(acc[0], acc[1]) > float(rules["max_accel"])
                or abs(float(np.mean(vel[:, 2]))) > float(rules["max_climb"])
                or float(np.max(tilt)) > float(rules["max_tilt_deg"])):  # fmt: skip
            continue
        force = acc - np.array([0.0, 0.0, G])
        drag = np.array([np.mean(bx[s] @ force), np.mean(by[s] @ force)])
        ground = np.array([np.mean(np.sum(bx[s] * vel, axis=1)),
                           np.mean(np.sum(by[s] * vel, axis=1))])  # fmt: skip
        turn = np.array([np.mean(bx[s][:, :2], axis=0), np.mean(by[s][:, :2], axis=0)])
        rows.append((int(start), int(start + step), drag, ground, turn, height))
    if not rows:
        return _empty()
    return Windows(
        np.array([r[0] for r in rows], np.int64), np.array([r[1] for r in rows], np.int64),
        np.array([r[2] for r in rows]), np.array([r[3] for r in rows]),
        np.array([r[4] for r in rows]), np.array([r[5] for r in rows]),
    )  # fmt: skip


def _airspeed(w: Windows, wind: Array) -> Array:
    return w.ground - np.einsum("nij,j->ni", w.turn, wind)


def _residual(w: Windows, k1: float, k2: float, wind: Array, bias: Array) -> Array:
    v = _airspeed(w, wind)
    return (w.drag - (-k1 * v - k2 * v * np.abs(v) + bias)).ravel()


def _linear(w: Windows) -> tuple[float, Array, Array] | None:
    """k1, the wind and the bias, with no quadratic term: drag = -k1 (ground - turn wind)
    + bias is linear in k1, k1 wind and the bias."""
    n = len(w)
    a = np.zeros((2 * n, 5))
    a[:, 0] = -w.ground.ravel()
    a[0::2, 1:3] = w.turn[:, 0, :]
    a[1::2, 1:3] = w.turn[:, 1, :]
    a[0::2, 3] = 1.0
    a[1::2, 4] = 1.0
    sol, *_ = np.linalg.lstsq(a, w.drag.ravel(), rcond=None)
    k1 = float(sol[0])
    if not k1 > 0:
        return None
    return k1, sol[1:3] / k1, sol[3:5]


def _gauss_newton(w: Windows, k1: float, k2: float, wind: Array, bias: Array, steps: int
                  ) -> tuple[float, float, Array, Array]:  # fmt: skip
    theta = np.array([k1, k2, *wind, *bias])
    for _ in range(steps):
        k1, k2, wind, bias = theta[0], theta[1], theta[2:4], theta[4:6]
        v = _airspeed(w, wind)
        r = (w.drag - (-k1 * v - k2 * v * np.abs(v) + bias)).ravel()
        slope = k1 + 2 * k2 * np.abs(v)  # d(-pred)/d(airspeed), per axis
        j = np.zeros((len(r), 6))
        j[:, 0] = v.ravel()
        j[:, 1] = (v * np.abs(v)).ravel()
        # airspeed = ground - turn wind: the residual moves with the wind by -slope turn
        j[:, 2:4] = -(slope[..., None] * w.turn).reshape(-1, 2)
        j[0::2, 4] = -1.0
        j[1::2, 5] = -1.0
        step, *_ = np.linalg.lstsq(j, -r, rcond=None)
        theta = theta + step
        if float(np.max(np.abs(step))) < 1e-10:
            break
    return float(theta[0]), float(theta[1]), theta[2:4], theta[4:6]


def _rms(w: Windows, k1: float, k2: float, wind: Array, bias: Array) -> float:
    return math.sqrt(float(np.mean(_residual(w, k1, k2, wind, bias) ** 2)))


def _quadratic(w: Windows, k1: float, k2: float, wind: Array, bias: Array, steps: int
               ) -> Fit | None:  # fmt: skip
    k1, k2, wind, bias = _gauss_newton(w, k1, k2, wind, bias, steps)
    if not (k1 >= 0 and k2 > 0) or not np.all(np.isfinite([k1, k2, *wind, *bias])):
        return None
    return Fit(k1, k2, wind, bias, True, _rms(w, k1, k2, wind, bias))


def fit(w: Windows, rules: Mapping[str, Any], start: Fit | None = None) -> Fit | None:
    """The flight's drag, wind and bias; None when its windows cannot tell them apart.
    With `start` (a resample), the same model refitted from the full sample's answer."""
    steps = int(rules["iterations"])
    if start is not None and start.quadratic:
        return _quadratic(w, start.k1, start.k2, start.wind, start.bias, steps)
    lin = _linear(w)
    if lin is None:
        return None
    k1, wind, bias = lin
    linear = Fit(k1, 0.0, wind, bias, False, _rms(w, k1, 0.0, wind, bias))
    if start is not None:
        return linear
    candidates = [linear]
    quad = _quadratic(w, k1, float(rules["k2_start"]), wind, bias, steps)
    if quad is not None:
        candidates.append(quad)
    n = 2 * len(w)

    def bic(f: Fit) -> float:
        return n * math.log(max(f.rms**2, 1e-18)) + (6 if f.quadratic else 5) * math.log(n)

    return min(candidates, key=bic)


def window_winds(w: Windows, f: Fit) -> Array:
    """(n, 2) north, east: each window's wind, from its airspeed inverted from its drag."""
    push = -(w.drag - f.bias)  # = k1 v + k2 v|v|, per axis
    if f.k2 > 0:
        v = np.sign(push) * (-f.k1 + np.sqrt(f.k1**2 + 4 * f.k2 * np.abs(push))) / (2 * f.k2)
    else:
        v = push / f.k1
    # ground - airspeed = turn wind, per window
    return np.linalg.solve(w.turn, (w.ground - v)[..., None])[..., 0]


@dataclass(frozen=True)
class Measured:
    fit: Fit
    winds: Array  # (n, 2)
    speed: float
    peak: float
    intervals: Mapping[str, tuple[float, float]]


def _stats(winds: Array, rules: Mapping[str, Any]) -> tuple[float, float]:
    speeds = np.hypot(winds[:, 0], winds[:, 1])
    return float(np.mean(speeds)), float(np.percentile(speeds, float(rules["peak_percentile"])))


def measure(w: Windows, rules: Mapping[str, Any]) -> Measured | None:
    """The wind over the flight with its intervals, or None when it cannot be measured."""
    if len(w) < int(rules["min_windows"]):
        return None
    speeds = np.hypot(*(np.einsum("nji,nj->ni", w.turn, w.ground).T))
    if np.percentile(speeds, 90) - np.percentile(speeds, 10) < float(rules["min_speed_span"]):
        return None
    best = fit(w, rules)
    if best is None:
        return None
    winds = window_winds(w, best)
    speed, peak = _stats(winds, rules)
    rng = np.random.default_rng(int(rules["seed"]))
    samples: dict[str, list[float]] = {"speed": [], "peak": [], "k1": [], "k2": [], "trim": []}
    for _ in range(int(rules["resamples"])):
        rows = rng.integers(0, len(w), len(w))
        again = fit(w.take(rows), rules, start=best)
        if again is None:
            continue
        s, p = _stats(window_winds(w.take(rows), again), rules)
        samples["speed"].append(s)
        samples["peak"].append(p)
        samples["k1"].append(again.k1)
        samples["k2"].append(again.k2)
        samples["trim"].append(trim_deg(again.bias))
    if len(samples["speed"]) < int(rules["resamples"]) // 2:
        return None
    tail = (1.0 - float(rules["level"])) / 2.0 * 100.0
    intervals = {k: (float(np.percentile(v, tail)), float(np.percentile(v, 100.0 - tail)))
                 for k, v in samples.items()}  # fmt: skip
    low, high = intervals["speed"]
    if high - low > float(rules["max_interval_ms"]):
        return None
    return Measured(best, winds, speed, peak, intervals)


def trim_deg(bias: Array) -> float:
    """How far the aircraft leans in still air, in degrees."""
    return math.degrees(math.atan2(float(np.hypot(*bias)), G))


def blowing_from(wind: Array) -> float:
    """The direction the wind comes from, in degrees from north."""
    return math.degrees(math.atan2(-float(wind[1]), -float(wind[0]))) % 360.0


def _ekf_wind(flight: FlightData, w: Windows) -> float | None:
    """The estimator's own wind speed over the windows, when it fuses drag."""
    x2 = flight.table("XKF2", ["TimeUS", "C", "VWN", "VWE"])
    if x2 is None or not x2.num_rows or not {"VWN", "VWE"} <= set(x2.column_names):
        return None
    t = _ints(x2.column("TimeUS").to_numpy())
    rows = _primary(flight, t, _ints(x2.column("C").to_numpy()))
    t, vn, ve = t[rows], si.values(x2, "VWN")[rows], si.values(x2, "VWE")[rows]
    inside = np.zeros(len(t), bool)
    for a, b in zip(w.start_us, w.end_us, strict=True):
        inside |= (t >= a) & (t < b)
    if not inside.any():
        return None
    return float(np.mean(np.hypot(vn[inside], ve[inside])))


def _wind(flight: FlightData, reg: Registry) -> Result:
    rules: Mapping[str, Any] = reg.rules["wind"]
    if flight.vehicle != "copter" or flight.fingerprint.get("vehicle_class") not in (
        None, "multirotor",
    ):  # fmt: skip
        return Result(skipped="measured wind is worked out for multirotors")
    e = ekf(flight)
    if e is None:
        return Result(skipped="no estimator attitude and velocity (XKF1) in the log")
    w = windows(e, flight.arm_us, flight.disarm_us, rules)
    m = measure(w, rules)
    if m is None:
        return Result(
            skipped="the flight holds too little steady flight at changing speed and heading"
            " to tell the aircraft's drag from the wind"
        )
    segment = Segment("flight", int(w.start_us[0]), int(w.end_us[-1]))
    mean_wind = np.mean(m.winds, axis=0)
    speeds_ground = np.hypot(*(np.einsum("nji,nj->ni", w.turn, w.ground).T))
    evidence = Evidence(
        id="wind:flight",
        kind="series",
        summary=(
            f"{m.speed:.1f} m/s of wind from {blowing_from(mean_wind):.0f} deg over"
            f" {len(w)} steady windows at {float(np.median(w.height)):.0f} m"
        ),
        messages=("XKF1",),
        fields=EKF_FIELDS,
        segment=segment,
        values={
            "windows": len(w),
            "height_m": round(float(np.median(w.height)), 1),
            "from_deg": round(blowing_from(mean_wind)),
            "ground_speed_p10": round(float(np.percentile(speeds_ground, 10)), 2),
            "ground_speed_p90": round(float(np.percentile(speeds_ground, 90)), 2),
            "model": "quadratic" if m.fit.quadratic else "linear",
            "residual_rms": round(m.fit.rms, 4),
            # the fit itself, so the airframe's other flights can use it
            "k1": round(m.fit.k1, 5),
            "k2": round(m.fit.k2, 6),
            "bias_x": round(float(m.fit.bias[0]), 4),
            "bias_y": round(float(m.fit.bias[1]), 4),
        },
    )
    out = Result(evidence=[evidence])

    def metric(mid: str, value: float, key: str | None) -> None:
        spread = {} if key is None else {"low": m.intervals[key][0], "high": m.intervals[key][1]}
        out.metrics.append(MetricValue(mid, "flight", value, None, evidence.id, spread))

    metric("wind.speed_ms", m.speed, "speed")
    metric("wind.peak_ms", m.peak, "peak")
    metric("wind.drag_linear", m.fit.k1, "k1")
    if m.fit.quadratic:
        metric("wind.drag_quadratic", m.fit.k2, "k2")
    metric("wind.trim_deg", trim_deg(m.fit.bias), "trim")
    if drag_fusion(flight.params, reg.rules["no_gnss"]["readiness"]):
        ekf_speed = _ekf_wind(flight, w)
        if ekf_speed is not None:
            ev = Evidence(
                id="wind:ekf",
                kind="series",
                summary=f"the estimator's own wind, with drag fusion: {ekf_speed:.1f} m/s",
                messages=("XKF2",),
                fields=("VWN", "VWE"),
                segment=segment,
                values={"windows": len(w)},
            )
            out.evidence.append(ev)
            out.metrics.append(MetricValue("wind.ekf_speed_ms", "flight", ekf_speed, None, ev.id))
    return out


WIND = Analyzer(
    name="wind",
    version=1,
    run=_wind,
    metrics=(
        "wind.speed_ms",
        "wind.peak_ms",
        "wind.drag_linear",
        "wind.drag_quadratic",
        "wind.trim_deg",
        "wind.ekf_speed_ms",
    ),
    findings=(),
)
