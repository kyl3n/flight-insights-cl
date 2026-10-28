"""Flight time, range and radius against the take-off weight, with how sure they are (spec:
Power and efficiency analysis, "Range, endurance and speed", "What the user gets"; rules in
`knowledge/rules/performance.json` "projections").

- **The pack.** What a pack gives before its reserve depends on its load: worked harder it
  sags more, and its failsafes and cutoff come sooner. It is worked out once over the loads
  the projections need (`battery.usable_curve`), and each figure reads the energy at its
  own power.
- **Flight time:** hovering in still air, and at the best-endurance airspeed.
- **Range:** in still air at the airspeed of most metres per pack, the pack's energy at
  that power over the power, times the airspeed. The pack's sag at higher power puts it a
  little slower than the model's own tangent.
- **Radius:** each standard trip (`performance/radius.py`) on the energy the pack gives at
  the trip's mean power, repeated until the two agree. A varying load loses a little more to
  the pack's resistance than a steady one at its mean (the loss grows with the square of
  the current), a fraction of a per cent, well inside the model's own error.
- **Against weight:** from the empty weight to the heaviest take-off, each marked measured
  where the weight is within the weights flown (by the rules' margin), else extrapolated.
- **How sure:** each figure again for every bootstrap model (once there are enough flights
  to resample), each with a zero-mean model error sized by the leave-one-flight-out energy
  errors: the 95% interval about the model's own figure. The card uses the low end.
- **Efficiency:** the hover's figure of merit (the ideal power `W v_h` over the packs'
  power) and grams per watt in standard air, and at the best-range airspeed Wh/km, Wh per
  kg-km and the cost of transport `P / (W V)`.
- **What-ifs:** what each figure gains from less weight, less drag, less avionics draw, and
  more capacity with the weight it adds.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from flight_insights.performance.battery import Failsafe, Pack, Usable, usable_curve
from flight_insights.performance.fit import Fitted, G, Model
from flight_insights.performance.optimize import Array, Cells, best
from flight_insights.performance.radius import Radius, Trip, radius, standard

TRIPS = ("out_and_back", "drop", "site")
STANDARD_DENSITY = 1.225  # kg/m^3, sea level in the standard atmosphere


def per_pack(curve: Usable) -> Cells:
    """Terminal power to a power whose cost per metre ranks airspeeds by the metres a pack
    flies: `P E0 / E(P)`, so `best` finds the airspeeds of the most time and distance."""
    full = float(np.max(curve.energy_wh))

    def cells(power: Array) -> Array:
        e = curve.at(power)
        return np.asarray(np.where(e > 0, power * full / np.maximum(e, 1e-12), np.inf))

    return cells


def pack_curve(model: Model, rules: Mapping[str, Any], pack: Pack, start_soc: float,
               failsafe: Failsafe | None, reserve_share: float, masses_kg: Sequence[float],
               density: float, top: float) -> Usable:  # fmt: skip
    """The pack's usable energy over every load the projections at these weights ask for:
    from below the lightest hover to above the heaviest weight's climb and fastest leg."""
    spec: Mapping[str, Any] = rules["projections"]
    w = np.asarray(masses_kg, np.float64) * G
    climb = float(rules["trips"]["climb_ms"])
    hover = model.power(w, 0.0, 0.0, density, rules)
    fast = model.power(w, top, 0.0, density, rules)
    up = model.power(w, 0.0, climb, density, rules)
    low = float(np.min(hover)) * float(spec["low_load"])
    high = float(max(np.max(fast), np.max(up), np.max(hover))) * float(spec["high_load"])
    return usable_curve(pack, start_soc, failsafe, reserve_share, low, high,
                        int(spec["load_points"]), int(spec["discharge_steps"]))  # fmt: skip


def _fixed_s(trip: Trip) -> float:
    return trip.height_m / trip.climb_ms + trip.site_s + trip.height_m / trip.descent_ms


def trip_radius(model: Model, rules: Mapping[str, Any], curve: Usable, mass_kg: float,
                density: float, trip: Trip, wind_ms: float, angle_rad: float,
                top: float) -> Radius | None:  # fmt: skip
    """The trip's radius on what the pack gives at the trip's mean power."""
    spec: Mapping[str, Any] = rules["projections"]
    budget = float(curve.at(model.power(mass_kg * G, 0.0, 0.0, density, rules))[()])
    found: Radius | None = None
    for _ in range(int(spec["budget_rounds"])):
        if budget <= 0:
            return None
        found = radius(model, rules, mass_kg, density, trip, budget, wind_ms, angle_rad, top)
        if found is None:
            return None
        legs = found.metres / found.out.ground + found.metres / found.back.ground
        seconds = _fixed_s(trip) + legs
        again = float(curve.at(budget * 3600 / seconds)[()])
        if abs(again - budget) <= float(spec["budget_tolerance"]) * budget:
            break
        budget = again
    return found


def worst_trip(model: Model, rules: Mapping[str, Any], curve: Usable, mass_kg: float,
               density: float, trip: Trip, wind_ms: float,
               top: float) -> tuple[float, list[tuple[float, float]]]:  # fmt: skip
    """The least radius over the directions relative to the wind, and each (degrees,
    metres) all round. A wind from one side costs what it does from the other, so the half
    turn is flown and mirrored."""
    step = float(rules["trips"]["direction_step_deg"])
    half: dict[float, float] = {}
    for deg in np.arange(0.0, 180.0 + step / 2, step):
        r = trip_radius(model, rules, curve, mass_kg, density, trip, wind_ms,
                        math.radians(float(deg)), top)  # fmt: skip
        half[float(deg)] = 0.0 if r is None else r.metres
    by = [(d, half[d if d <= 180 else 360 - d]) for d in np.arange(0.0, 360.0, step).tolist()]
    return min(m for _, m in by), by


@dataclass(frozen=True)
class Figures:
    mass_kg: float
    hover_s: float  # hovering in still air
    endurance_s: float  # at the best-endurance airspeed
    endurance_ms: float
    range_m: float  # still air, at the best-range airspeed
    range_ms: float
    radius_m: Mapping[str, float]  # per standard trip, still air; 0 where it cannot fly it
    measured: bool  # the weight is within the weights flown
    efficiency: Efficiency


@dataclass(frozen=True)
class Efficiency:
    """How well the airframe turns power into lift and distance at one weight."""

    figure_of_merit: float  # hover in standard air: the ideal power W v_h over the packs' power
    grams_per_watt: float  # hover in standard air
    wh_per_km: float  # at the best-range airspeed, at the terminals, in the air given
    wh_per_kg_km: float  # the same per kg of take-off weight
    cost_of_transport: float  # P / (W V) at the best-range airspeed


def efficiency(model: Model, rules: Mapping[str, Any], mass_kg: float, density: float,
               airspeed: float) -> Efficiency:  # fmt: skip
    """Hover figures in standard air, so airframes and days compare; cruise figures at the
    given airspeed and air."""
    w = mass_kg * G
    hover = float(model.power(w, 0.0, 0.0, STANDARD_DENSITY, rules)[()])
    ideal = w * math.sqrt(w / (2 * STANDARD_DENSITY * model.disc_area))
    cruise = float(model.power(w, airspeed, 0.0, density, rules)[()])
    per_km = cruise / airspeed * 1000 / 3600 if airspeed > 0 else math.inf
    return Efficiency(
        figure_of_merit=ideal / hover,
        grams_per_watt=mass_kg * 1000 / hover,
        wh_per_km=per_km,
        wh_per_kg_km=per_km / mass_kg,
        cost_of_transport=cruise / (w * airspeed) if airspeed > 0 else math.inf,
    )


def figures(model: Model, rules: Mapping[str, Any], curve: Usable, mass_kg: float,
            empty_kg: float, density: float, top: float,
            flown_kg: tuple[float, float] | None = None,
            trips: Sequence[str] = TRIPS) -> Figures | None:  # fmt: skip
    """The figures at one take-off weight; the drop leaves what is above the empty weight.
    None when the pack cannot hold a hover at this weight."""
    w = mass_kg * G
    hover = float(model.power(w, 0.0, 0.0, density, rules)[()])
    if float(curve.at(hover)[()]) <= 0:
        return None
    s = best(model, w, density, rules, top, per_pack(curve))
    at = model.power(w, np.array([s.endurance, s.range]), 0.0, density, rules)
    seconds = curve.seconds(at)
    out: dict[str, float] = {}
    for kind in trips:
        trip = standard(kind, rules, max(mass_kg - empty_kg, 0.0))
        r = trip_radius(model, rules, curve, mass_kg, density, trip, 0.0, 0.0, top)
        out[kind] = 0.0 if r is None else r.metres
    margin = float(rules["projections"]["measured_margin"])
    measured = flown_kg is not None and (
        flown_kg[0] * (1 - margin) <= mass_kg <= flown_kg[1] * (1 + margin)
    )
    return Figures(
        mass_kg=mass_kg,
        hover_s=float(curve.seconds(hover)[()]),
        endurance_s=float(seconds[0]),
        endurance_ms=s.endurance,
        range_m=float(seconds[1]) * s.range,
        range_ms=s.range,
        radius_m=out,
        measured=measured,
        efficiency=efficiency(model, rules, mass_kg, density, s.range),
    )


def against_weight(model: Model, rules: Mapping[str, Any], curve: Usable, empty_kg: float,
                   max_kg: float, density: float, top: float,
                   flown_kg: tuple[float, float] | None = None) -> list[Figures]:  # fmt: skip
    """The figures from the empty weight to the heaviest take-off; weights the pack cannot
    hover are left out."""
    found = [
        figures(model, rules, curve, float(m), empty_kg, density, top, flown_kg)
        for m in np.linspace(empty_kg, max_kg, int(rules["projections"]["weight_steps"]))
    ]
    return [f for f in found if f is not None]


def scaled(model: Model, share: float) -> Model:
    """The model with every power `share` times what it gives."""
    return replace(model, avionics=model.avionics * share, induced=model.induced * share,
                   flight=model.flight * share, profile=model.profile * share)  # fmt: skip


@dataclass(frozen=True)
class Spread:
    low: float
    median: float
    high: float


SPREAD = ("hover_s", "endurance_s", "range_m", "radius_m")


def _values(f: Figures | None) -> dict[str, float]:
    if f is None:
        return dict.fromkeys(SPREAD, 0.0)
    return {"hover_s": f.hover_s, "endurance_s": f.endurance_s, "range_m": f.range_m,
            "radius_m": f.radius_m["out_and_back"]}  # fmt: skip


def spread(fitted: Fitted, rules: Mapping[str, Any], curve: Usable, mass_kg: float,
           empty_kg: float, density: float, top: float) -> dict[str, Spread]:  # fmt: skip
    """The figures' 95% interval at one weight, about the model's own figure (the median).
    Each draw is a bootstrap model (once the airframe has the rules' flights; with fewer,
    resampling them mostly refits one flight alone, and the model itself is drawn) with a
    model error: a zero-mean normal error with the leave-one-flight-out errors' root mean
    square (a flight's energy predicted `1 + e` times what it took, so the power is the
    model's over `1 + e`). The leave-one-out errors are those of a model fitted on fewer
    flights, so they size the spread rather than shift it. The radius is the out-and-back
    trip's."""
    spec: Mapping[str, Any] = rules["projections"]
    rng = np.random.default_rng(int(rules["fit"]["seed"]))
    errors = np.array(list(fitted.lofo.values()), np.float64)
    sigma = float(np.sqrt(np.mean(errors**2))) if len(errors) else 0.0
    enough = fitted.flights >= int(spec["bootstrap_min_flights"]) and fitted.replicates
    models = fitted.replicates if enough else (fitted.model,) * int(rules["fit"]["bootstrap"])
    centre = _values(figures(fitted.model, rules, curve, mass_kg, empty_kg, density, top,
                             trips=("out_and_back",)))  # fmt: skip
    got: dict[str, list[float]] = {k: [] for k in SPREAD}
    for m in models:
        e = float(rng.normal(0.0, sigma)) if sigma > 0 else 0.0
        found = _values(figures(scaled(m, 1.0 / (1.0 + e)), rules, curve, mass_kg, empty_kg,
                                density, top, trips=("out_and_back",)))  # fmt: skip
        for k in SPREAD:
            got[k].append(found[k])
    tail = (1.0 - float(spec["level"])) / 2 * 100
    out: dict[str, Spread] = {}
    for k, v in got.items():
        mid = float(np.median(v))
        out[k] = Spread(centre[k] + float(np.percentile(v, tail)) - mid, centre[k],
                        centre[k] + float(np.percentile(v, 100 - tail)) - mid)  # fmt: skip
    return out


@dataclass(frozen=True)
class WhatIf:
    lever: str  # 'mass', 'drag', 'avionics' or 'capacity'
    change: float  # kg, m^2 of flat plate, W, or Ah
    hover_s: float  # what each figure gains (negative: loses)
    endurance_s: float
    range_m: float
    radius_m: float  # out and back, still air


def what_ifs(model: Model, rules: Mapping[str, Any], pack: Pack, start_soc: float,
             failsafe: Failsafe | None, reserve_share: float, mass_kg: float,
             empty_kg: float, pack_mass_kg: float | None, density: float,
             top: float) -> list[WhatIf]:  # fmt: skip
    """Each lever by the rules' step at this weight. More capacity is the same cells in
    parallel: its resistance falls as its capacity grows, and it weighs its share more of
    the pack's mass; with the pack's mass unknown it is left out."""
    spec: Mapping[str, Any] = rules["projections"]["what_ifs"]
    more = float(spec["capacity_share"])
    heavier = mass_kg + (pack_mass_kg or 0.0) * more
    # one grid of loads for every lever, so a lever that leaves a power alone leaves its figure
    curve = pack_curve(model, rules, pack, start_soc, failsafe, reserve_share,
                       [mass_kg, heavier], density, top)  # fmt: skip

    def at(m: Model, kg: float, c: Usable = curve) -> Figures | None:
        return figures(m, rules, c, kg, empty_kg, density, top, trips=("out_and_back",))

    base = at(model, mass_kg)
    if base is None:
        return []
    less = float(spec["mass_share"]) * mass_kg
    drag = float(spec["drag_share"])
    avionics = min(float(spec["avionics_w"]), model.avionics)
    levers: list[tuple[str, float, Figures | None]] = [
        ("mass", less, at(model, mass_kg + less)),
        ("drag", drag * model.drag_area,
         at(replace(model, drag_area=model.drag_area * (1 + drag)), mass_kg)),
        ("avionics", -avionics, at(replace(model, avionics=model.avionics - avionics), mass_kg)),
    ]  # fmt: skip
    if pack_mass_kg is not None:
        bigger = replace(pack, capacity_ah=pack.capacity_ah * (1 + more),
                         resistance=pack.resistance / (1 + more))  # fmt: skip
        grown = pack_curve(model, rules, bigger, start_soc, failsafe, reserve_share,
                           [mass_kg, heavier], density, top)  # fmt: skip
        levers.append(("capacity", pack.capacity_ah * more, at(model, heavier, grown)))
    return [
        WhatIf(
            lever,
            change,
            (f.hover_s if f else 0.0) - base.hover_s,
            (f.endurance_s if f else 0.0) - base.endurance_s,
            (f.range_m if f else 0.0) - base.range_m,
            (f.radius_m["out_and_back"] if f else 0.0) - base.radius_m["out_and_back"],
        )
        for lever, change, f in levers
    ]
