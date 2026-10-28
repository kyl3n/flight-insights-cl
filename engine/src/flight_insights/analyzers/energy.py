"""Energy, range and speed (spec: Power and efficiency analysis, "Range, endurance and
speed"; rules in `knowledge/rules/energy.json`).

It measures a flight's energy, and the speed sweep: the flight that gives the airframe's
power against airspeed, which ArduPilot's simulator cannot show (its motors draw power in
proportion to thrust, with no drop at moderate forward speed).

- **Energy.** The packs' power (every monitor that measures its own pack,
  `battery_power.supply`) integrated from arming to disarming by the trapezoid rule over
  every sample, and the charge the same way.
- **Distance flown.** The primary estimator core's horizontal ground speed integrated
  over the same time, in whole metres: how far it went, never where.
- **Where the energy went.** The packs' energy in each steady tile's flight phase
  (`analyzers/steady.py`): low, hover, level, climb, descent and manoeuvre, and what fell
  between tiles. Energy per km is the flight's energy over its distance, and at cruise the
  level tiles' energy over their own distance.

- **Tiles.** The flight's 2 s tiles of the primary estimator core (XKF1,
  `analyzers/steady.py`). A tile is level and steady when it flies well above take-off
  at a real ground speed, hardly accelerating, climbing or turning its track.
- **Legs.** Back-to-back steady tiles holding one speed and one track make a leg; a leg
  counts once it is long enough to read power at that speed.
- **Pairs.** A leg and the next one, heading the opposite way soon after, are one speed
  flown both ways: their mean speed cancels the wind along the legs, whether the pilot
  held the airspeed or ArduPilot the ground speed (`WP_SPD`), so it reads as an airspeed.
- **Levels.** Pairs sorted by speed split into speed levels at gaps.

Only ground velocity, height above take-off and the packs' voltage and current are read
(never a position); counts, speeds, energies and the distance flown leave.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.analyzers import steady
from flight_insights.analyzers.battery_power import Supply, energy_wh, supply
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import Analyzer, Evidence, MetricValue, Result, Segment
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.wind import ekf

Array = np.ndarray[Any, Any]
FIELDS = ("VN", "VE", "VD", "PD")
POWER_FIELDS = ("Volt", "Curr")


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
    found = steady.tiles(e, arm_us, disarm_us, float(rules["tile_s"]), int(rules["min_samples"]))
    keep = (
        (found.height >= float(rules["min_height_m"]))
        & (found.speed >= float(rules["min_speed_ms"]))
        & (found.horizontal_accel <= float(rules["max_accel"]))
        & (np.abs(found.velocity[:, 2]) <= float(rules["max_climb"]))
        & (found.track_rate <= float(rules["max_turn_deg_s"]))
    )
    level = found.take(keep)
    return [
        Tile(int(a), int(b), float(v), float(k))
        for a, b, v, k in zip(level.start_us, level.end_us, level.speed, level.track, strict=True)
    ]


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


@dataclass(frozen=True)
class FlightEnergy:
    used_wh: float  # at the packs' terminals, arming to disarming
    used_mah: float
    bridged_s: float  # time across gaps in the battery rows, integrated over
    flown_m: float  # horizontal distance over the ground
    level_m: float  # of it, in level tiles
    phase_wh: dict[str, float]  # each of steady.PHASES, and "untiled"
    phase_s: dict[str, float]

    @property
    def wh_per_km(self) -> float:
        return self.used_wh / (self.flown_m / 1000.0)

    @property
    def cruise_wh_per_km(self) -> float:
        return self.phase_wh["level"] / (self.level_m / 1000.0)


def running(t_us: Array, values: Array) -> Array:
    """The running trapezoid integral of `values` over time in seconds, from the first row."""
    tt = t_us.astype(np.float64) / 1e6
    out = np.zeros(len(tt))
    if len(tt) > 1:
        out[1:] = np.cumsum(np.diff(tt) * (values[1:] + values[:-1]) / 2)
    return out


def between(t_us: Array, total: Array, start_us: int, end_us: int) -> float:
    """What a running integral gathered between two log times."""
    a, b = np.interp([start_us, end_us], t_us.astype(np.float64), total)
    return float(b - a)


def flight_energy(e: Mapping[str, Array], packs: Supply, arm_us: int, disarm_us: int,
                  rules: Mapping[str, Any], max_gap_s: float) -> FlightEnergy:  # fmt: skip
    """The flight's energy, charge and distance, and its energy by phase."""
    joules = running(packs.t, packs.power)
    coulombs = running(packs.t, packs.current)
    metres = running(e["t"], np.hypot(e["VN"], e["VE"]))
    used = between(packs.t, joules, arm_us, disarm_us) / 3600.0
    found = steady.tiles(e, arm_us, disarm_us, float(rules["tile_s"]), int(rules["min_samples"]))
    labels = steady.phases(found, rules)
    wh: dict[str, float] = dict.fromkeys(steady.PHASES, 0.0)
    seconds: dict[str, float] = dict.fromkeys(steady.PHASES, 0.0)
    level_m = 0.0
    for a, b, phase in zip(found.start_us, found.end_us, labels, strict=True):
        wh[str(phase)] += between(packs.t, joules, int(a), int(b)) / 3600.0
        seconds[str(phase)] += (int(b) - int(a)) / 1e6
        if phase == "level":
            level_m += between(e["t"], metres, int(a), int(b))
    wh["untiled"] = used - sum(wh.values())
    seconds["untiled"] = (disarm_us - arm_us) / 1e6 - sum(seconds.values())
    return FlightEnergy(
        used_wh=used,
        used_mah=between(packs.t, coulombs, arm_us, disarm_us) / 3.6,
        bridged_s=energy_wh(packs.t, packs.power, arm_us, disarm_us, max_gap_s)[1],
        flown_m=between(e["t"], metres, arm_us, disarm_us),
        level_m=level_m,
        phase_wh=wh,
        phase_s=seconds,
    )


def _flight(flight: FlightData, reg: Registry, e: Mapping[str, Array], out: Result) -> None:
    packs = supply(flight, reg.rules["batteries"])
    if packs is None or not packs.instances:
        return
    rules: Mapping[str, Any] = reg.rules["energy"]
    fe = flight_energy(e, packs, flight.arm_us, flight.disarm_us, rules["phases"],
                       float(reg.rules["batteries"]["max_gap_s"]))  # fmt: skip
    least = float(rules["flight"]["min_distance_m"])
    per_km = fe.wh_per_km if fe.flown_m >= least else None
    cruise = fe.cruise_wh_per_km if fe.level_m >= least else None
    evidence = Evidence(
        id="energy:flight",
        kind="series",
        summary=(
            f"{fe.used_wh:.0f} Wh ({fe.used_mah:.0f} mAh) from the packs over "
            f"{fe.flown_m / 1000:.2f} km flown"
        ),
        messages=("BAT", "XKF1"),
        fields=(*POWER_FIELDS, *FIELDS),
        segment=Segment("flight", flight.arm_us, flight.disarm_us),
        values={
            "used_wh": round(fe.used_wh, 1),
            "used_mah": round(fe.used_mah),
            "bridged_s": round(fe.bridged_s, 1),
            "flown_m": round(fe.flown_m),
            "wh_per_km": None if per_km is None else round(per_km, 1),
            "cruise_wh_per_km": None if cruise is None else round(cruise, 1),
            **{f"{k}_wh": round(v, 1) for k, v in fe.phase_wh.items()},
            **{f"{k}_s": round(v, 1) for k, v in fe.phase_s.items()},
            "monitors": len(packs.instances),
            "read_from": packs.how,
        },
    )
    out.evidence.append(evidence)
    out.metrics.append(MetricValue("energy.used_wh", "flight", fe.used_wh, None, evidence.id))
    out.metrics.append(MetricValue("energy.distance_m", "flight", float(round(fe.flown_m)),
                                   None, evidence.id))  # fmt: skip
    if per_km is not None:
        out.metrics.append(MetricValue("energy.wh_per_km", "flight", per_km, None, evidence.id))
    if cruise is not None:
        out.metrics.append(MetricValue("energy.cruise_wh_per_km", "flight", cruise, None,
                                       evidence.id))  # fmt: skip


def _energy(flight: FlightData, reg: Registry) -> Result:
    rules: Mapping[str, Any] = reg.rules["energy"]["sweep"]
    if flight.vehicle != "copter" or flight.fingerprint.get("vehicle_class") not in (
        None, "multirotor",
    ):  # fmt: skip
        return Result(skipped="energy and the speed sweep are worked out for multirotors")
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
    _flight(flight, reg, e, out)
    return out


ENERGY = Analyzer(
    name="energy",
    version=2,
    run=_energy,
    metrics=(
        "energy.sweep_levels",
        "energy.sweep_fastest_ms",
        "energy.used_wh",
        "energy.distance_m",
        "energy.wh_per_km",
        "energy.cruise_wh_per_km",
    ),
    findings=(),
)
