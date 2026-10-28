"""Energy, range and speed (spec: Power and efficiency analysis, "Range, endurance and
speed"; rules in `knowledge/rules/energy.json`).

So far it measures the speed sweep: the flight that gives the airframe's power against
airspeed, which ArduPilot's simulator cannot show (its motors draw power in proportion
to thrust, with no drop at moderate forward speed).

- **Tiles.** The flight is cut into 2 s tiles of the primary estimator core (XKF1). A
  tile is level and steady when it flies well above take-off at a real ground speed,
  hardly accelerating, climbing or turning its track.
- **Legs.** Back-to-back steady tiles holding one speed and one track make a leg; a leg
  counts once it is long enough to read power at that speed.
- **Pairs.** A leg and the next one, heading the opposite way soon after, are one speed
  flown both ways: their mean speed cancels the wind along the legs, whether the pilot
  held the airspeed or ArduPilot the ground speed (`WP_SPD`), so it reads as an airspeed.
- **Levels.** Pairs sorted by speed split into speed levels at gaps.

Only ground velocity is read (never a position), and only counts and speeds leave.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import Analyzer, Evidence, MetricValue, Result, Segment
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.wind import ekf

Array = np.ndarray[Any, Any]
FIELDS = ("VN", "VE", "VD", "PD")


@dataclass(frozen=True)
class Tile:
    start_us: int
    end_us: int
    speed: float  # ground speed, m/s
    track: float  # degrees from north, where it goes


@dataclass(frozen=True)
class Leg:
    start_us: int
    end_us: int
    speed: float  # mean ground speed, m/s
    track: float  # degrees from north, of its first tile

    @property
    def seconds(self) -> float:
        return (self.end_us - self.start_us) / 1e6


@dataclass(frozen=True)
class Pair:
    """A leg and the next one flown the opposite way."""

    out: Leg
    back: Leg

    @property
    def speed(self) -> float:
        """The mean of the two: the wind along the legs cancels."""
        return (self.out.speed + self.back.speed) / 2


@dataclass(frozen=True)
class Level:
    pairs: tuple[Pair, ...]

    @property
    def speed(self) -> float:
        return float(np.mean([p.speed for p in self.pairs]))


def _turn(a: float, b: float) -> float:
    """The smaller angle between two directions, degrees."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def tiles(e: Mapping[str, Array], arm_us: int, disarm_us: int, rules: Mapping[str, Any]
          ) -> list[Tile]:  # fmt: skip
    """The level, steady tiles between arming and disarming."""
    t = e["t"]
    flown = (t >= arm_us) & (t <= disarm_us)
    if not flown.any():
        return []
    ground_pd = float(e["PD"][np.argmax(flown)])
    step = int(float(rules["tile_s"]) * 1e6)
    edges = np.arange(arm_us, disarm_us - step + 1, step)
    lo = np.searchsorted(t, edges, side="left")
    hi = np.searchsorted(t, edges + step, side="left")
    out: list[Tile] = []
    for start, a, b in zip(edges, lo, hi, strict=True):
        if b - a < int(rules["min_samples"]):
            continue
        s = slice(a, b)
        tt = (t[s] - t[s].mean()) / 1e6
        if np.ptp(tt) <= 0:
            continue
        vn, ve, vd = e["VN"][s], e["VE"][s], e["VD"][s]
        speed = float(np.hypot(np.mean(vn), np.mean(ve)))
        height = -(float(np.mean(e["PD"][s])) - ground_pd)
        accel = math.hypot(np.polyfit(tt, vn, 1)[0], np.polyfit(tt, ve, 1)[0])
        heading = np.unwrap(np.arctan2(ve, vn))
        turn = abs(math.degrees(float(np.polyfit(tt, heading, 1)[0])))
        if (height < float(rules["min_height_m"])
                or speed < float(rules["min_speed_ms"])
                or accel > float(rules["max_accel"])
                or abs(float(np.mean(vd))) > float(rules["max_climb"])
                or turn > float(rules["max_turn_deg_s"])):  # fmt: skip
            continue
        track = math.degrees(math.atan2(float(np.mean(ve)), float(np.mean(vn)))) % 360.0
        out.append(Tile(int(start), int(start + step), speed, track))
    return out


def legs(found: Sequence[Tile], rules: Mapping[str, Any]) -> list[Leg]:
    """Back-to-back tiles at one speed and track, long enough to count."""
    out: list[Leg] = []
    run: list[Tile] = []

    def close() -> None:
        if run and (run[-1].end_us - run[0].start_us) / 1e6 >= float(rules["min_leg_s"]):
            speed = float(np.mean([x.speed for x in run]))
            out.append(Leg(run[0].start_us, run[-1].end_us, speed, run[0].track))

    for tile in found:
        if run and (
            tile.start_us != run[-1].end_us
            or abs(tile.speed - float(np.mean([x.speed for x in run])))
            > float(rules["leg_speed_ms"])
            or _turn(tile.track, run[0].track) > float(rules["leg_track_deg"])
        ):
            close()
            run = []
        run.append(tile)
    close()
    return out


def pairs(found: Sequence[Leg], rules: Mapping[str, Any]) -> list[Pair]:
    """Each leg with the next, when that one starts soon after, heads the opposite way and
    is near its speed; a leg pairs once."""
    out: list[Pair] = []
    i = 0
    while i + 1 < len(found):
        a, b = found[i], found[i + 1]
        if (
            (b.start_us - a.end_us) / 1e6 <= float(rules["pair_gap_s"])
            and 180.0 - _turn(a.track, b.track) <= float(rules["reciprocal_deg"])
            and abs(a.speed - b.speed) <= float(rules["pair_speed_ms"])
        ):
            out.append(Pair(a, b))
            i += 2
        else:
            i += 1
    return out


def levels(found: Sequence[Pair], rules: Mapping[str, Any]) -> list[Level]:
    """Pairs grouped into speed levels, slowest first."""
    groups: list[list[Pair]] = []
    for pair in sorted(found, key=lambda p: p.speed):
        if groups and pair.speed - groups[-1][-1].speed <= float(rules["level_gap_ms"]):
            groups[-1].append(pair)
        else:
            groups.append([pair])
    return [Level(tuple(g)) for g in groups]


def _energy(flight: FlightData, reg: Registry) -> Result:
    rules: Mapping[str, Any] = reg.rules["energy"]["sweep"]
    if flight.vehicle != "copter" or flight.fingerprint.get("vehicle_class") not in (
        None, "multirotor",
    ):  # fmt: skip
        return Result(skipped="the speed sweep is worked out for multirotors")
    e = ekf(flight)
    if e is None:
        return Result(skipped="no estimator velocity (XKF1) in the log")
    found = legs(tiles(e, flight.arm_us, flight.disarm_us, rules), rules)
    both = levels(pairs(found, rules), rules)
    segment = (
        Segment("flight", found[0].start_us, found[-1].end_us)
        if found
        else Segment("flight", flight.arm_us, flight.disarm_us)
    )
    evidence = Evidence(
        id="energy:sweep",
        kind="series",
        summary=(f"{len(found)} steady straight legs; {len(both)} speeds flown both ways"),
        messages=("XKF1",),
        fields=FIELDS,
        segment=segment,
        values={
            "legs": len(found),
            "pairs": sum(len(lv.pairs) for lv in both),
            "levels": len(both),
            "leg_seconds": round(sum(g.seconds for g in found), 1),
            "slowest_ms": round(both[0].speed, 2) if both else None,
            "fastest_ms": round(both[-1].speed, 2) if both else None,
            "min_levels": int(rules["min_levels"]),
        },
    )
    out = Result(evidence=[evidence])
    out.metrics.append(MetricValue("energy.sweep_levels", "flight", float(len(both)), None,
                                   evidence.id))  # fmt: skip
    if both:
        out.metrics.append(MetricValue("energy.sweep_fastest_ms", "flight", both[-1].speed,
                                       None, evidence.id))  # fmt: skip
    return out


ENERGY = Analyzer(
    name="energy",
    version=1,
    run=_energy,
    metrics=("energy.sweep_levels", "energy.sweep_fastest_ms"),
    findings=(),
)
