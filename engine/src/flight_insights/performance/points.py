"""A flight's points for the airframe's power model (spec: Power and efficiency analysis,
"Range, endurance and speed", "The power model"; rules in `knowledge/rules/performance.json`
"points").

- **Which tiles.** The flight's steady 2 s tiles (`analyzers/steady.py`) in hover, level
  flight, climb and descent. Left out: a tile in a mode the rules name (a landing), one
  starting within the settling time of a mode change, and one whose power wanders more
  than the rules allow (the aircraft working, not flying steady).
- **Airspeed.** The tile's ground velocity less the wind. The wind is each drag window's
  own (`wind.window_winds`, from the drag the flight measured itself, else its airframe's
  in the same payload band and build), smoothed by a running median over the rules' time;
  with no drag to read it from, the wind is taken as calm and the points say so.
- **Power.** The packs' mean power over the tile: every monitor that measures its own pack
  (`battery_power.supply`), every row.
- **Air.** The density from the barometer's pressure: the simulator's standard atmosphere
  in SITL, else at the weather record's air temperature, else at the barometer's own.
- **Points.** Tiles averaged, weighted by time, per phase, airspeed bin and climb bin. The
  flight's take-off weight comes with them: weighed at import, else from the airframe's
  weighed flights through the payload bands, else the setup's typical take-off weight,
  else unknown.

Only velocities, attitude, height above take-off, pressure and the packs' voltage and
current are read, never a position; the points stay on this laptop.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from flight_insights.analyzers import steady
from flight_insights.analyzers.battery_power import Supply, supply
from flight_insights.analyzers.flight import FlightData, FlightError
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.tune import modes
from flight_insights.analyzers.wind import ekf, window_winds, windows
from flight_insights.dataflash import si
from flight_insights.knowledge import Pack
from flight_insights.performance.propeller import Anchor, flight_anchor
from flight_insights.sim.export import isa_density_at
from flight_insights.store.cache import CacheError, ColumnCache
from flight_insights.store.db import Store

Array = np.ndarray[Any, Any]
Wind = Callable[[Array], Array]  # tile centres, µs -> (n, 2) north, east, where the air goes


@dataclass(frozen=True)
class Point:
    phase: str
    airspeed: float  # m/s, the mean of the tiles' airspeeds
    climb: float  # m/s, up positive
    power: float  # W, at the packs' terminals
    density: float  # kg/m^3
    seconds: float
    tiles: int


@dataclass(frozen=True)
class FlightPoints:
    flight_id: str
    points: tuple[Point, ...]
    mass_kg: float | None
    mass_from: str
    wind_from: str  # 'flight', 'airframe' or 'none' (taken as calm)
    density_from: str
    left_out: Mapping[str, float] = field(default_factory=dict[str, float])  # seconds, by why
    anchor: Anchor | None = None  # the propeller's operating point in hover
    anchor_why: str | None = None  # why there is none


def calm(centres: Array) -> Array:
    return np.zeros((len(centres), 2))


def smoothed(times_us: Array, winds: Array, span_s: float) -> Wind:
    """Each drag window's wind, as a running median over `span_s` centred on each tile."""
    order = np.argsort(times_us)
    t, w = times_us[order].astype(np.float64), winds[order]
    half = span_s * 1e6 / 2

    def at(centres: Array) -> Array:
        out = np.zeros((len(centres), 2))
        for k, c in enumerate(centres):
            near = (t >= c - half) & (t <= c + half)
            if near.any():
                out[k] = np.median(w[near], axis=0)
            else:
                out[k] = w[int(np.argmin(np.abs(t - c)))]
        return out

    return at


def tile_points(
    e: Mapping[str, Array],
    packs: Supply,
    density: Callable[[Array, Array], Array],
    wind: Wind,
    arm_us: int,
    disarm_us: int,
    mode_spans: Sequence[tuple[int, int, str]],
    rules: Mapping[str, Any],
    phase_rules: Mapping[str, Any],
) -> tuple[list[Point], dict[str, float]]:
    """The flight's points, and the seconds of steady tiles left out, by why. `density`
    gives each tile's air from its start and end times."""
    found = steady.tiles(e, arm_us, disarm_us, float(phase_rules["tile_s"]),
                         int(phase_rules["min_samples"]))  # fmt: skip
    labels = steady.phases(found, phase_rules)
    wanted = set(rules["phases"])
    keep = np.array([str(p) in wanted for p in labels], bool)
    left: dict[str, float] = {}
    if not keep.any():
        return [], left
    t = found.take(keep)
    names = labels[keep]
    step = float(phase_rules["tile_s"])
    starts, ends = t.start_us, t.end_us
    # the packs' power over each tile, and how much it wanders
    power = np.zeros(len(t))
    wander = np.zeros(len(t))
    for k, (a, b) in enumerate(zip(starts, ends, strict=True)):
        inside = (packs.t >= a) & (packs.t < b)
        p = packs.power[inside]
        if len(p) < 2:
            power[k] = np.nan
            continue
        power[k] = float(np.mean(p))
        wander[k] = float(np.std(p)) / max(abs(power[k]), 1e-9)
    excluded = set(rules["excluded_modes"])
    settle = float(rules["settle_s"]) * 1e6
    why = np.full(len(t), "", dtype=object)
    for start, end, name in mode_spans:
        during = (starts < end) & (ends > start)
        if name in excluded:
            why[during & (why == "")] = "mode"
        why[(starts >= start) & (starts < start + settle) & (why == "")] = "mode change"
    why[np.isnan(power) & (why == "")] = "no power"
    why[(wander > float(rules["max_power_cv"])) & (why == "")] = "power not steady"
    for reason in set(why[why != ""]):
        left[str(reason)] = float(np.count_nonzero(why == reason)) * step
    ok = why == ""
    if not ok.any():
        return [], left
    centres = ((starts + ends) // 2)[ok]
    air = t.velocity[ok, :2] - wind(centres)
    airspeed = np.hypot(air[:, 0], air[:, 1])
    climb = t.climb[ok]
    rho = density(starts[ok], ends[ok])
    phase = names[ok]
    bins = (np.round(airspeed / float(rules["airspeed_bin_ms"])),
            np.round(climb / float(rules["climb_bin_ms"])))  # fmt: skip
    out: list[Point] = []
    keys = sorted({(str(p), int(a), int(c)) for p, a, c in zip(phase, *bins, strict=True)})
    for name, a, c in keys:
        rows = (phase == name) & (bins[0] == a) & (bins[1] == c)
        n = int(np.count_nonzero(rows))
        out.append(Point(
            phase=name, airspeed=float(np.mean(airspeed[rows])), climb=float(np.mean(climb[rows])),
            power=float(np.mean(power[ok][rows])), density=float(np.mean(rho[rows])),
            seconds=n * step, tiles=n,
        ))  # fmt: skip
    return out, left


def _density(flight: FlightData, store: Store, gas_constant: float
             ) -> tuple[Callable[[Array, Array], Array], str] | None:  # fmt: skip
    """Each tile's air density from the barometer, and where its temperature came from."""
    from flight_insights.weather import record as weather_record

    baro = flight.table("BARO")
    if baro is None or baro.num_rows == 0 or "Press" not in baro.column_names:
        return None
    t = np.asarray(baro.column("TimeUS").to_numpy(), np.int64)
    primary = int(flight.params.get("BARO_PRIMARY", 0))
    rows = (
        np.asarray(baro.column("I").to_numpy(), np.int64) == primary
        if "I" in baro.column_names
        else np.ones(len(t), bool)
    )
    t, pressure = t[rows], si.values(baro, "Press")[rows]
    celsius = si.values(baro, "Temp")[rows] if "Temp" in baro.column_names else None
    kept = weather_record.kept(store, [flight.id]).get(flight.id, {}).get("temperature_c")
    if flight.simulated:
        how = "the simulator's standard atmosphere"
    elif isinstance(kept, int | float) and math.isfinite(kept):
        how = "the barometer's pressure at the weather record's air temperature"
    elif celsius is not None:
        how = "the barometer's pressure at its own temperature (the board's, a little warm)"
    else:
        return None

    def mean(values: Array, a: Array, b: Array) -> Array:
        out = np.empty(len(a))
        for k, (x, y) in enumerate(zip(a, b, strict=True)):
            inside = (t >= x) & (t < y)
            out[k] = np.mean(values[inside]) if inside.any() else np.interp((x + y) / 2, t, values)
        return out

    def at(a: Array, b: Array) -> Array:
        p = mean(pressure, a, b)
        if flight.simulated:
            return isa_density_at(p)
        if isinstance(kept, int | float) and math.isfinite(kept):
            return p / (gas_constant * (float(kept) + 273.15))
        assert celsius is not None
        return p / (gas_constant * (mean(celsius, a, b) + 273.15))

    return at, how


def _wind(store: Store, reg: Registry, flight_id: str, e: Mapping[str, Array], arm_us: int,
          disarm_us: int, span_s: float) -> tuple[Wind, str]:  # fmt: skip
    from flight_insights.weather.measured import drag

    got = drag(store, reg, flight_id)
    if got is None:
        return calm, "none"
    fit, sources = got
    w = windows(e, arm_us, disarm_us, reg.rules["wind"])
    if len(w) == 0:
        return calm, "none"
    winds = window_winds(w, fit)
    centres = (w.start_us + w.end_us) // 2
    return smoothed(centres, winds, span_s), "flight" if sources == (flight_id,) else "airframe"


def _mass(store: Store, reg: Registry, flight: FlightData) -> tuple[float | None, str]:
    from flight_insights.trends import analysed_flights

    if flight.airframe_id is None:
        return None, "the flight has no airframe"
    flights, bands = analysed_flights(store, reg, flight.airframe_id)
    mine = next((f for f in flights if f.id == flight.id), None)
    if mine is not None and mine.takeoff_kg:
        return float(mine.takeoff_kg), "weighed at import"
    if mine is not None and (kg := bands.kg(mine.thrust)) is not None:
        return kg, "estimated from the airframe's weighed flights (payload bands)"
    typical = flight.answers.get("typical_takeoff_kg")
    if isinstance(typical, int | float) and typical > 0:
        return float(typical), "the setup's typical take-off weight (the flight was not weighed)"
    return None, "not known: no weighed flight and no typical take-off weight in the setup"


def flight_points(store: Store, cache: ColumnCache, pack: Pack, flight_id: str
                  ) -> FlightPoints | str:  # fmt: skip
    """The flight's points for the power model, or why it has none."""
    reg = Registry.load(pack)
    try:
        flight = FlightData.load(store, cache, pack, flight_id)
    except (FlightError, CacheError) as err:
        return str(err)
    if flight.vehicle != "copter" or flight.fingerprint.get("vehicle_class") not in (
        None, "multirotor",
    ):  # fmt: skip
        return "the power model is worked out for multirotors"
    e = ekf(flight)
    if e is None:
        return "no estimator velocity (XKF1) in the log"
    packs = supply(flight, reg.rules["batteries"])
    if packs is None or not packs.instances:
        return "no battery monitor measures the packs' power"
    rules: Mapping[str, Any] = reg.rules["performance"]["points"]
    air = _density(flight, store, float(reg.rules["payload"]["gas_constant"]))
    if air is None:
        return "no barometer pressure to give the air's density"
    density, density_from = air
    wind, wind_from = _wind(store, reg, flight_id, e, flight.arm_us, flight.disarm_us,
                            float(rules["wind_median_s"]))  # fmt: skip
    points, left = tile_points(e, packs, density, wind, flight.arm_us, flight.disarm_us,
                               modes(flight, reg), rules,
                               reg.rules["energy"]["phases"])  # fmt: skip
    mass, mass_from = _mass(store, reg, flight)
    anchor: Anchor | str = mass_from
    if mass is not None:
        anchor = flight_anchor(flight, reg, mass, density, (packs.t, packs.power))
    return FlightPoints(flight_id, tuple(points), mass, mass_from, wind_from, density_from, left,
                        anchor if isinstance(anchor, Anchor) else None,
                        anchor if isinstance(anchor, str) else None)  # fmt: skip
