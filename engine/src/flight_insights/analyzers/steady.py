"""Steady tiles: a flight cut into short tiles of the primary estimator core (XKF1), and
each tile's flight phase (spec: Power and efficiency analysis, "Range, endurance and
speed"; phase rules in `knowledge/rules/energy.json`).

- **Tiles.** From arming to disarming, back-to-back tiles of one length; a tile with too
  few rows, or all at one time, is left out. Each holds its mean velocity, the slope of
  its velocity (the acceleration), how fast its track turns, its height above take-off
  and its largest tilt. The analyzers that need steady flight read the same tiles and
  keep the ones their own rules allow: the wind analyzer its drag windows, the energy
  analyzer its sweep legs.
- **Phases.** A tile is hover, level, climb, descent or manoeuvre by its ground velocity
  alone; below the height the rules set it is low, near the ground, whatever it does.

Only velocities, attitude and height above take-off are read (never a position).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

Array = np.ndarray[Any, Any]
PHASES = ("low", "hover", "level", "climb", "descent", "manoeuvre")


@dataclass(frozen=True)
class Tiles:
    """One row a tile, in time order."""

    start_us: Array
    end_us: Array
    lo: Array  # the tile's first row in the estimator arrays
    hi: Array  # one past its last
    velocity: Array  # (n, 3) mean north, east, down, m/s
    accel: Array  # (n, 3) the velocity's slope north, east, down, m/s/s
    track_rate: Array  # how fast the track turns, deg/s
    height: Array  # mean above take-off, m
    tilt: Array  # the largest tilt from level, deg

    def __len__(self) -> int:
        return len(self.start_us)

    @property
    def speed(self) -> Array:
        """Ground speed, m/s."""
        return np.hypot(self.velocity[:, 0], self.velocity[:, 1])

    @property
    def climb(self) -> Array:
        """Climb rate, m/s, up positive."""
        return -self.velocity[:, 2]

    @property
    def track(self) -> Array:
        """Degrees from north, where it goes."""
        return np.array([math.degrees(math.atan2(e, n)) % 360.0 for n, e, _ in self.velocity])

    @property
    def horizontal_accel(self) -> Array:
        return np.hypot(self.accel[:, 0], self.accel[:, 1])

    def take(self, rows: Array) -> Tiles:
        return Tiles(*(getattr(self, f)[rows] for f in self.__dataclass_fields__))


def _empty() -> Tiles:
    ints = np.zeros(0, np.int64)
    return Tiles(ints, ints, ints, ints, np.zeros((0, 3)), np.zeros((0, 3)), *np.zeros((3, 0)))


def tiles(e: Mapping[str, Array], arm_us: int, disarm_us: int, tile_s: float,
          min_samples: int) -> Tiles:  # fmt: skip
    """The flight's tiles of `tile_s` from `e`, the primary core's XKF1 in time order
    (`wind.ekf`): t in microseconds, Roll and Pitch in degrees, VN, VE, VD and PD."""
    t = e["t"]
    flown = (t >= arm_us) & (t <= disarm_us)
    if not flown.any():
        return _empty()
    ground_pd = float(e["PD"][np.argmax(flown)])
    step = int(float(tile_s) * 1e6)
    edges = np.arange(arm_us, disarm_us - step + 1, step)
    roll, pitch = np.radians(e["Roll"]), np.radians(e["Pitch"])
    tilt = np.degrees(np.arccos(np.clip(np.cos(roll) * np.cos(pitch), -1, 1)))
    lo = np.searchsorted(t, edges, side="left")
    hi = np.searchsorted(t, edges + step, side="left")
    rows: list[tuple[int, int, int, Array, Array, float, float, float]] = []
    for start, a, b in zip(edges, lo, hi, strict=True):
        if b - a < int(min_samples):
            continue
        s = slice(a, b)
        tt = (t[s] - t[s].mean()) / 1e6
        if np.ptp(tt) <= 0:
            continue
        vn, ve, vd = e["VN"][s], e["VE"][s], e["VD"][s]
        accel = np.array([np.polyfit(tt, x, 1)[0] for x in (vn, ve, vd)])
        velocity = np.array([np.mean(vn), np.mean(ve), np.mean(vd)])
        heading = np.unwrap(np.arctan2(ve, vn))
        turn = abs(math.degrees(float(np.polyfit(tt, heading, 1)[0])))
        height = -(float(np.mean(e["PD"][s])) - ground_pd)
        rows.append((int(start), int(a), int(b), velocity, accel, turn, height,
                     float(np.max(tilt[s]))))  # fmt: skip
    if not rows:
        return _empty()
    start = np.array([r[0] for r in rows], np.int64)
    return Tiles(
        start, start + step,
        np.array([r[1] for r in rows], np.int64), np.array([r[2] for r in rows], np.int64),
        np.array([r[3] for r in rows]), np.array([r[4] for r in rows]),
        np.array([r[5] for r in rows]), np.array([r[6] for r in rows]),
        np.array([r[7] for r in rows]),
    )  # fmt: skip


def phases(found: Tiles, rules: Mapping[str, Any]) -> Array:
    """Each tile's phase (one of PHASES) by its ground velocity: low below min_height_m;
    a climb or descent at climb_ms or more; else a hover under hover_speed_ms; else level
    at level_speed_ms or more when it holds its speed, height and track (max_accel,
    level_climb_ms, max_turn_deg_s); else a manoeuvre."""
    out = np.full(len(found), "manoeuvre", dtype=object)
    climb = found.climb
    steady = (
        (np.abs(climb) <= float(rules["level_climb_ms"]))
        & (found.horizontal_accel <= float(rules["max_accel"]))
        & (found.track_rate <= float(rules["max_turn_deg_s"]))
    )
    level = steady & (found.speed >= float(rules["level_speed_ms"]))
    out[level] = "level"
    out[found.speed < float(rules["hover_speed_ms"])] = "hover"
    out[climb >= float(rules["climb_ms"])] = "climb"
    out[climb <= -float(rules["climb_ms"])] = "descent"
    out[found.height < float(rules["min_height_m"])] = "low"
    return out
