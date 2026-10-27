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
- **The strongest wind it can hold** (the wind envelope; rules in `wind.json`,
  "envelope"). Holding position in a wind w, the airspeed is the wind, so the drag the
  aircraft must lean against is its drag at that airspeed; tilted, it meets the wind at
  w cos(tilt) along its body axis, the airspeed the drag was fitted on, so the lean it
  needs solves g sin(tilt) = k1 w cos(tilt) + k2 (w cos(tilt))^2, with the lean in still
  air against it. The position controller leans no further than its limit (4.7.0,
  `AC_PosControl.cpp`): the configured maximum (`PSC_ANGLE_MAX`, else `ATC_ANGLE_MAX`),
  and the lean that keeps the throttle, boosted for the tilt, within 80% of full
  (`AC_AttitudeControl_Multi.cpp` `update_althold_lean_angle_max`), read from the throttle
  the flight measured hovering and at its highest airspeeds. The envelope is the wind at
  which the lean needed reaches that limit, with an interval from the same bootstrap as
  the drag.

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
    resamples: tuple[Fit, ...] = ()  # the bootstrap's fits, for what is worked out from them


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
    fits: list[Fit] = []
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
        fits.append(again)
    if len(samples["speed"]) < int(rules["resamples"]) // 2:
        return None
    tail = (1.0 - float(rules["level"])) / 2.0 * 100.0
    intervals = {k: (float(np.percentile(v, tail)), float(np.percentile(v, 100.0 - tail)))
                 for k, v in samples.items()}  # fmt: skip
    low, high = intervals["speed"]
    if high - low > float(rules["max_interval_ms"]):
        return None
    return Measured(best, winds, speed, peak, intervals, tuple(fits))


def trim_deg(bias: Array) -> float:
    """How far the aircraft leans in still air, in degrees."""
    return math.degrees(math.atan2(float(np.hypot(*bias)), G))


def blowing_from(wind: Array) -> float:
    """The direction the wind comes from, in degrees from north."""
    return math.degrees(math.atan2(-float(wind[1]), -float(wind[0]))) % 360.0


@dataclass(frozen=True)
class LeanLimit:
    """How far the position controller leans to hold position (Copter 4.7.0)."""

    deg: float
    by: str  # "lean": the configured maximum; "thrust": the headroom to hold height
    configured_deg: float
    thrust_deg: float | None  # None when the flight's throttle is not known
    throttle: float | None  # the throttle the firmware's limit reads (CTUN.ThI)
    throttle_from: str | None  # "hover" or "fast": steady hovers or the fastest steady
    # windows, whichever needed more; "learned": CTUN.ThH
    current_limited: bool  # MOT_BAT_CURR_MAX can lower full throttle below 1


def configured_lean(params: Mapping[str, float], rules: Mapping[str, Any]) -> float:
    """The position controller's configured lean limit: `PSC_ANGLE_MAX` when set, else
    `ATC_ANGLE_MAX` within the firmware's 10 to 80 degrees (AC_PosControl.cpp
    get_lean_angle_max_rad, AC_AttitudeControl.cpp lean_angle_max_rad)."""
    psc = float(params.get("PSC_ANGLE_MAX", 0.0) or 0.0)
    if psc > 0:
        return psc
    atc = float(params.get("ATC_ANGLE_MAX", rules["angle_max_default_deg"]))
    return min(max(atc, float(rules["angle_min_deg"])), float(rules["angle_max_deg"]))


def thrust_lean(throttle: float, rules: Mapping[str, Any]) -> float:
    """The lean the firmware allows while holding height: acos(throttle / (0.8 x full)),
    never under 10 degrees (AC_AttitudeControl_Multi.cpp update_althold_lean_angle_max,
    AC_AttitudeControl.cpp get_althold_lean_angle_max_rad)."""
    ratio = min(max(throttle / float(rules["throttle_limit"]), 0.0), 1.0)
    return max(math.degrees(math.acos(ratio)), float(rules["angle_min_deg"]))


def held_throttle(flight: FlightData, w: Windows, fast: Array) -> tuple[float, str] | None:
    """The throttle the firmware's lean limit reads (CTUN.ThI, before the tilt boost):
    the highest of its mean over each steady hover (the pack sags through the flight) and
    over the steady windows `fast` at the highest airspeeds flown, since holding a wind is
    flying at its airspeed; else the hover throttle it learned (CTUN.ThH) over the steady
    windows."""
    ctun = flight.table("CTUN", ["TimeUS", "ThI", "ThH"])
    if ctun is None or not ctun.num_rows or "ThI" not in ctun.column_names:
        return None
    t = _ints(ctun.column("TimeUS").to_numpy())
    thi = si.values(ctun, "ThI")
    spans = [(s.start_us, s.end_us, "hover") for s in flight.hover()]
    fast_rows = np.zeros(len(t), bool)
    for a, b in zip(w.start_us[fast], w.end_us[fast], strict=True):
        fast_rows |= (t >= a) & (t < b)
    means = [(float(np.mean(thi[(t >= a) & (t < b)])), how) for a, b, how in spans
             if ((t >= a) & (t < b)).any()]  # fmt: skip
    if fast_rows.any():
        means.append((float(np.mean(thi[fast_rows])), "fast"))
    if means:
        return max(means)
    if "ThH" not in ctun.column_names or not len(w):
        return None
    inside = np.zeros(len(t), bool)
    for a, b in zip(w.start_us, w.end_us, strict=True):
        inside |= (t >= a) & (t < b)
    if not inside.any():
        return None
    return float(np.median(si.values(ctun, "ThH")[inside])), "learned"


def lean_limit(params: Mapping[str, float], throttle: tuple[float, str] | None,
               rules: Mapping[str, Any]) -> LeanLimit:  # fmt: skip
    configured = configured_lean(params, rules)
    thrust = None if throttle is None else thrust_lean(throttle[0], rules)
    by = "thrust" if thrust is not None and thrust < configured else "lean"
    return LeanLimit(
        min(configured, thrust) if thrust is not None else configured, by, configured,
        thrust, None if throttle is None else throttle[0],
        None if throttle is None else throttle[1],
        float(params.get("MOT_BAT_CURR_MAX", 0.0) or 0.0) > 0,
    )  # fmt: skip


def _push(f: Fit, airspeed: float) -> float:
    """The drag's specific force at a body-axis airspeed, with the lean in still air on
    the same side, m/s/s."""
    return f.k1 * airspeed + f.k2 * airspeed * airspeed + float(np.hypot(*f.bias))


def lean_needed(f: Fit, wind: float) -> float:
    """The tilt, in degrees, that holds position in a steady horizontal wind, against the
    worst heading (the wind along a body axis, the lean in still air on the same side).
    Tilted, the aircraft meets the wind at w cos(tilt) along that axis, the airspeed its
    drag was fitted on, so the tilt solves g sin(tilt) = drag(w cos(tilt))."""
    low, high = 0.0, math.pi / 2
    for _ in range(60):  # the left side rises with the tilt and the right falls
        mid = (low + high) / 2
        if G * math.sin(mid) < _push(f, wind * math.cos(mid)):
            low = mid
        else:
            high = mid
    return math.degrees((low + high) / 2)


def strongest(f: Fit, limit_deg: float) -> float:
    """The steady wind at which the lean needed reaches `limit_deg`, in m/s: the body-axis
    airspeed whose drag needs that tilt, over cos(tilt)."""
    tilt = math.radians(limit_deg)
    push = G * math.sin(tilt) - float(np.hypot(*f.bias))
    if push <= 0:
        return 0.0
    if f.k2 > 0:
        airspeed = (-f.k1 + math.sqrt(f.k1 * f.k1 + 4.0 * f.k2 * push)) / (2.0 * f.k2)
    else:
        airspeed = push / f.k1
    return airspeed / math.cos(tilt)


def airspeeds(w: Windows, f: Fit) -> Array:
    """Each window's airspeed, m/s."""
    v = _airspeed(w, f.wind)
    return np.hypot(v[:, 0], v[:, 1])


def _envelope(flight: FlightData, w: Windows, m: Measured, reg: Registry, out: Result,
              segment: Segment) -> None:  # fmt: skip
    rules: Mapping[str, Any] = reg.rules["wind"]["envelope"]
    speeds = airspeeds(w, m.fit)
    flown = float(np.percentile(speeds, float(rules["flown_percentile"])))
    fast = speeds >= np.percentile(speeds, float(rules["fast_percentile"]))
    limit = lean_limit(flight.params, held_throttle(flight, w, fast), rules)
    # the most the flight leaned in steady flight, which is where its throttle is measured
    leaned = float(
        np.percentile(
            np.degrees(np.arcsin(np.clip(np.hypot(w.drag[:, 0], w.drag[:, 1]) / G, 0.0, 1.0))),
            float(rules["flown_percentile"]),
        )
    )
    # a hold limited by thrust at more lean than flown rests on a throttle never measured
    # there: in ArduPilot's simulator it rose from 0.36 to 0.48 between 30 and 53 degrees
    thrust_unmeasured = limit.by == "thrust" and limit.deg > leaned
    reference = float(rules["reference_ms"])
    hold = strongest(m.fit, limit.deg)
    tail = (1.0 - float(reg.rules["wind"]["level"])) / 2.0 * 100.0

    def interval(values: list[float], floor: float | None = None) -> dict[str, float]:
        if not values:
            return {}
        low = float(np.percentile(values, tail))
        return {"low": low if floor is None else min(low, floor),
                "high": float(np.percentile(values, 100.0 - tail))}  # fmt: skip

    learned = limit.throttle_from == "learned"
    ev = Evidence(
        id="wind:envelope",
        kind="fact",
        summary=(
            f"holds position in up to {hold:.1f} m/s of steady wind, leaning to"
            f" {limit.deg:.0f} deg"
            f" ({'its thrust headroom' if limit.by == 'thrust' else 'its lean limit'})"
        ),
        messages=("XKF1", "CTUN"),
        fields=(*EKF_FIELDS, "ThH" if learned else "ThI"),
        segment=segment,
        values={
            "limit_deg": round(limit.deg, 1),
            "limit_by": limit.by,
            "configured_deg": round(limit.configured_deg, 1),
            "thrust_deg": None if limit.thrust_deg is None else round(limit.thrust_deg, 1),
            "throttle": None if limit.throttle is None else round(limit.throttle, 3),
            "throttle_from": limit.throttle_from,
            "current_limited": limit.current_limited,
            "airspeed_flown_ms": round(flown, 1),
            "lean_flown_deg": round(leaned, 1),
            "extrapolated": hold > flown,
            "thrust_unmeasured": thrust_unmeasured,
            "reference_ms": reference,
        },
    )
    out.evidence.append(ev)
    out.metrics.append(MetricValue(
        "wind.envelope_ms", "flight", hold, None, ev.id,
        interval([strongest(f, limit.deg) for f in m.resamples],
                 flown if thrust_unmeasured else None),
    ))  # fmt: skip
    out.metrics.append(MetricValue(
        "wind.lean_at_reference_deg", "flight", lean_needed(m.fit, reference), None, ev.id,
        interval([lean_needed(f, reference) for f in m.resamples]),
    ))  # fmt: skip


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
    _envelope(flight, w, m, reg, out, segment)
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
    version=2,
    run=_wind,
    metrics=(
        "wind.speed_ms",
        "wind.peak_ms",
        "wind.drag_linear",
        "wind.drag_quadratic",
        "wind.trim_deg",
        "wind.ekf_speed_ms",
        "wind.envelope_ms",
        "wind.lean_at_reference_deg",
    ),
    findings=(),
)
