"""What the Range & speed screen and the range card show for an airframe (spec: Power and
efficiency analysis, "Range, endurance and speed", "What the user gets"): the inputs, read
from the airframe's setup, its latest parameters and its flights, each with where it came
from, and what the power model gives with them.

- **The model:** the airframe's kept power model (`performance/airframe.py`).
- **The pack:** the setup's chemistry, cells and capacity, charged full; its resistance the
  median of its flights' measured `power.battery_resistance` (none measured: none).
- **The failsafe:** the latest flight's `BATT_LOW_VOLT` for `BATT_LOW_TIMER` (on the
  sag-compensated voltage with `BATT_FS_VOLTSRC` 1) and `BATT_CAPACITY` less
  `BATT_LOW_MAH`; the reserve the setup's `reserve_pct` (20% unanswered).
- **The weights:** the setup's empty and heaviest take-off; figures at the weight asked,
  else the typical take-off, else the heaviest flown.
- **The air:** the airframe's usual (the median of its points' density), or standard.
- **The top:** the fastest level airspeed flown, so the speeds stay where the model was
  measured. An airframe that has only hovered gets its hover time alone: the speeds, range
  and radius wait for level flight.
- **The drag, for the lean:** the latest flight that measured its own (its `wind:flight`
  evidence), at its weight and air, with the throttle its lean limit read
  (`wind:envelope`).
- **The build-up:** each flight's fast segment (`fast.*`, `analyzers/fast.py`) with its
  weight, and the ESCs' thermal models from the latest flight with ESC telemetry.

- **The batteries:** the pack's usable energy against load, with the loads the figures fly
  at, and each of the airframe's packs' capacity and resistance across the flights it flew
  alone (`packs.history`), against the end-of-life conventions.
- **Builds side by side:** the platform's current parts against a candidate set, from
  the makers' bench tables a person entered (`performance/bench.py`): the candidate is
  the airframe's own model with its propulsion power scaled by the tables' ratio at the
  hover thrust per motor; every candidate figure is an estimate, not flown.
- **The what-ifs:** each lever alone; the weight's is measured when the flights span more
  than one weight, every other lever is the model's alone until a flight with the change
  confirms it.

Anything an input is missing for is left out, with what to enter or fly to get it. Each
figure says what ends it (the reserve, the cutoff, or a failsafe the parameters set sooner:
`Usable.stopped`), with the failsafe it read.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.flight import FlightError
from flight_insights.analyzers.motors_health import motors
from flight_insights.knowledge import Pack as KnowledgePack
from flight_insights.performance import airframe as perf
from flight_insights.performance import bench, packs
from flight_insights.performance.battery import Failsafe, Pack, Usable, chemistry
from flight_insights.performance.envelope import Fast, verified
from flight_insights.performance.envelope import Inputs as EnvelopeInputs
from flight_insights.performance.envelope import step as next_step
from flight_insights.performance.fit import Fitted, G, Model
from flight_insights.performance.optimize import Cells, best, leg, terminals
from flight_insights.performance.points import FlightPoints
from flight_insights.performance.project import (
    STANDARD_DENSITY,
    TRIPS,
    against_weight,
    draws,
    figures,
    hover_spread,
    measured,
    pack_curve,
    per_pack,
    spread,
    what_ifs,
    worst_trip,
)
from flight_insights.performance.radius import standard
from flight_insights.performance.thermal import Thermal, from_flight
from flight_insights.performance.top_speed import Basis, lowest_volts, top_speed
from flight_insights.store.cache import CacheError, ColumnCache
from flight_insights.store.db import Store
from flight_insights.trends import Flight, analysed_flights, metric_of
from flight_insights.weather.measured import drag

FAST_METRICS = (
    "fast.vibe_p95", "fast.clips", "fast.output_max", "fast.saturation_pct", "fast.lean_share",
    "fast.attitude_error_deg", "fast.velocity_error_ms", "fast.height_loss_m",
    "fast.thrust_loss",
)  # fmt: skip


@dataclass
class Inputs:
    model: perf.AirframeModel
    params: Mapping[str, float] = field(default_factory=dict[str, float])
    answers: Mapping[str, Any] = field(default_factory=dict[str, Any])
    pack: Pack | None = None
    failsafe: Failsafe = field(default_factory=Failsafe)
    reserve_share: float = 0.2
    empty_kg: float | None = None
    typical_kg: float | None = None
    max_kg: float | None = None
    flown_kg: tuple[float, float] | None = None
    usual_density: float | None = None
    top_ms: float | None = None
    basis: Basis | None = None
    current_a: float | None = None
    pack_mass_kg: float | None = None
    esc_limit_c: float | None = None
    thermals: Sequence[Thermal] = ()
    motors: int = 4
    fast: Sequence[Fast] = ()
    latest_flight: str | None = None
    flights: Sequence[Flight] = ()
    sources: dict[str, str] = field(default_factory=dict[str, str])
    missing: dict[str, str] = field(default_factory=dict[str, str])


def _number(answers: Mapping[str, Any], key: str) -> float | None:
    v = answers.get(key)
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and v > 0 else None


def _evidence(store: Store, run_id: str, eid: str) -> Mapping[str, Any] | None:
    row = store.conn.execute("SELECT evidence_json FROM analysis_run WHERE id = ?",
                             (run_id,)).fetchone()  # fmt: skip
    if row is None:
        return None
    found = [e for e in json.loads(row[0]) if e.get("id") == eid]
    return found[0].get("values", {}) if found else None


def measured_pack(store: Store, reg: Registry, flight_id: str) -> tuple[float, float, int] | None:
    """The packs as the airframe's latest flights up to this one measured them, taken as
    in parallel (as their power is summed): the capacities added, the resistances under
    held load combined; None unless every monitor of the latest fitted flight has both."""
    spec: Mapping[str, Any] = reg.rules["performance"]["packs"]
    upto = packs.flown_upto(store, reg, flight_id)
    fitted = [packs.fits(store, f.run_id) for f in upto if f.run_id is not None]
    fitted = [x for x in fitted if x]
    if not fitted:
        return None
    capacity = packs.measured(store, reg, flight_id)
    monitors = set(fitted[-1])
    if not monitors <= set(capacity):
        return None
    conductance = 0.0
    for m in monitors:
        ohms = [x[m].resistance_mohm for x in fitted if m in x][-int(spec["flights"]) :]
        conductance += 1.0 / max(float(np.median(ohms)) / 1000.0, 1e-6)
    flights = max(len(capacity[m].flights) for m in monitors)
    return sum(capacity[m].capacity_mah for m in monitors), 1.0 / conductance, flights


def gather(store: Store, cache: ColumnCache, pack: KnowledgePack, airframe_id: str) -> Inputs:
    """Everything the figures need, with where each came from and what is missing."""
    reg = Registry.load(pack)
    model = perf.load(store, cache, pack, airframe_id)
    out = Inputs(model)
    flights, _bands = analysed_flights(store, reg, airframe_id)
    if not flights:
        out.missing["flights"] = "import and analyse a flight of the airframe"
        return out
    try:
        latest = FlightData.load(store, cache, pack, flights[-1].id)
    except (FlightError, CacheError) as e:
        out.missing["flights"] = str(e)
        return out
    out.params, out.answers, out.latest_flight = latest.params, latest.answers, latest.id
    out.flights = flights
    out.motors = max(len(motors(latest.params)), 1)
    a = latest.answers
    out.empty_kg, out.typical_kg, out.max_kg = (
        _number(a, "empty_kg"), _number(a, "typical_takeoff_kg"), _number(a, "max_takeoff_kg"),
    )  # fmt: skip
    out.current_a, out.pack_mass_kg = _number(a, "max_current_a"), _number(a, "pack_mass_kg")
    out.esc_limit_c = _number(a, "esc_temp_protection_c")
    reserve = _number(a, "reserve_pct")
    out.reserve_share = (reserve or 20.0) / 100.0
    out.sources["reserve"] = "the setup's reserve" if reserve else "20%, the setup leaves it open"

    weighed = [f.mass_kg for f in model.flights if f.mass_kg is not None]
    if weighed:
        out.flown_kg = (min(weighed), max(weighed))
    points = [(p, f) for f in model.flights for p in f.points]
    if points:
        out.usual_density = float(np.median([p.density for p, _ in points]))
        level = [p.airspeed for p, _ in points if p.phase == "level"]
        out.top_ms = max(level) if level else None
    if out.top_ms is None:
        out.missing["top"] = "fly level legs (the speed card) so the speeds have a measured span"

    # the pack: as its monitors measured it (in parallel), else the setup's and BAT.Res
    cells, capacity = _number(a, "cells"), _number(a, "capacity_mah")
    got = measured_pack(store, reg, flights[-1].id)
    if cells is None or (capacity is None and got is None):
        out.missing["pack"] = "enter the pack's cells and capacity in the setup"
    else:
        name = str(a.get("battery_chemistry") or "LiPo")
        resistances = [metric_of(store, f.run_id, "power.battery_resistance", None)
                       for f in flights if f.run_id]  # fmt: skip
        ohms = [v[0] / 1000.0 for v in resistances if v is not None]
        r = float(np.median(ohms)) if ohms else 0.0
        if got is not None:
            mah, r, n = got
            out.sources["pack"] = (f"{int(cells)}S {name} of {mah:.0f} mAh and {1000 * r:.1f} mOhm "
                                   f"as {n} flights measured it")  # fmt: skip
        else:
            assert capacity is not None
            mah = capacity
            out.sources["pack"] = f"{int(cells)}S {name} of {capacity:g} mAh from the setup, " + (
                f"{1000 * r:.0f} mOhm over {len(ohms)} flights" if ohms
                else "no resistance measured"
            )  # fmt: skip
        out.pack = Pack(chemistry(reg.rules["ocv"], name), int(cells), mah / 1000.0, r)
    p = latest.params
    low_v = float(p.get("BATT_LOW_VOLT", 0.0) or 0.0)
    out.failsafe = Failsafe(
        low_volt=low_v if low_v > 0 else None,
        timer_s=float(p.get("BATT_LOW_TIMER", 10.0)),
        resting=int(p.get("BATT_FS_VOLTSRC", 0)) == 1,
        capacity_mah=float(p["BATT_CAPACITY"]) if p.get("BATT_CAPACITY") else None,
        low_mah=float(p["BATT_LOW_MAH"]) if p.get("BATT_LOW_MAH") else None,
    )

    # the drag the latest flight that measured its own gave, at its weight and air
    by_id = {f.flight_id: f for f in model.flights}
    for f in reversed(flights):
        got = drag(store, reg, f.id)
        fp = by_id.get(f.id)
        if got is None or got[1] != (f.id,) or fp is None or fp.mass_kg is None or not fp.points:
            continue
        envelope = _evidence(store, f.run_id, "wind:envelope") if f.run_id else None
        throttle = (envelope or {}).get("throttle")
        air = float(np.median([x.density for x in fp.points]))
        hover = float(throttle) if isinstance(throttle, int | float) else None
        out.basis = Basis(got[0], fp.mass_kg, air, hover)
        out.sources["drag"] = f"flight {f.id}'s own drag at {fp.mass_kg:g} kg"
        break
    if out.basis is None:
        out.missing["drag"] = "fly a flight that changes speed and heading so it measures its drag"

    # the build-up: each flight's fast segment, and the latest ESC thermal models
    for f in flights:
        if not f.run_id:
            continue
        speed = metric_of(store, f.run_id, "fast.speed_ms", "fast")
        seconds = metric_of(store, f.run_id, "fast.seconds", "fast")
        if speed is None or seconds is None:
            continue
        zones = dict(store.conn.execute(
            "SELECT metric, zone FROM metric_value WHERE run_id = ? AND segment = 'fast'",
            (f.run_id,),
        ).fetchall())  # fmt: skip
        watched = {m: (v[0], zones.get(m)) for m in FAST_METRICS
                   if (v := metric_of(store, f.run_id, m, "fast")) is not None}  # fmt: skip
        mass = by_id[f.id].mass_kg if f.id in by_id else f.takeoff_kg
        out.fast = [*out.fast, Fast(f.id, speed[0], seconds[0], mass, watched)]
    for f in reversed(flights):
        try:
            found = from_flight(FlightData.load(store, cache, pack, f.id), reg.rules["performance"])
        except (FlightError, CacheError):
            continue
        if found:
            out.thermals = found
            out.sources["esc"] = f"flight {f.id}'s ESC telemetry"
            break
    return out


def _weight(i: Inputs, asked: float | None) -> tuple[float | None, str]:
    if asked is not None:
        return asked, "as asked"
    if i.typical_kg is not None:
        return i.typical_kg, "the setup's typical take-off"
    if i.flown_kg is not None:
        return i.flown_kg[1], "the heaviest flown"
    return None, "enter the typical take-off weight in the setup, or weigh a flight at import"


def _round(x: Any) -> Any:
    if isinstance(x, float):
        return round(x, 4) if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}  # type: ignore[misc]
    if isinstance(x, list | tuple):
        return [_round(v) for v in x]  # type: ignore[misc]
    return x


def _speeds(model: Model, rules: Mapping[str, Any], weight: float, density: float, top: float,
            cells: Cells) -> dict[str, Any]:  # fmt: skip
    """The speeds ranked through the pack (`cells`), with the power and energy per km at
    the terminals, as the packs give them."""
    s = best(model, weight, density, rules, top, cells)
    out = asdict(s)
    out["endurance_w"] = float(model.power(weight, s.endurance, 0.0, density, rules)[()])
    at = float(model.power(weight, s.range, 0.0, density, rules)[()])
    out["range_wh_per_km"] = at / s.range * 1000 / 3600 if s.range > 0 else None
    return out


def _headwind(model: Model, weight: float, density: float, rules: Mapping[str, Any],
              top: float, wind_ms: float) -> dict[str, Any]:  # fmt: skip
    """The best leg into a headwind: its airspeed and ground speed, or none that makes way."""
    x = leg(model, weight, density, rules, top, -wind_ms, 0.0)
    return {"wind_ms": wind_ms, **(asdict(x) if x else {"airspeed": None})}


def _battery(store: Store, reg: Registry, airframe_id: str, flights: Sequence[Flight],
             curve: Usable, loads: Mapping[str, float]) -> dict[str, Any]:  # fmt: skip
    """The pack's usable energy against load (down to the reserve or what ends it sooner),
    with the loads the figures fly at; and each of the airframe's packs' capacity and
    resistance on the flights it flew alone on one monitor, against the end-of-life
    conventions."""
    spec: Mapping[str, Any] = reg.rules["performance"]["packs"]
    out: dict[str, Any] = {
        "usable": [{"power_w": float(p), "energy_wh": float(e), "ends": s} for p, e, s
                   in zip(curve.powers, curve.energy_wh, curve.stopped_by, strict=True)],
        "loads": dict(loads), "aged_capacity": float(spec["aged_capacity"]),
        "aged_resistance": float(spec["aged_resistance"]),
        "caution_resistance": float(spec["caution_resistance"]), "packs": [],
    }  # fmt: skip
    rows = store.conn.execute(
        "SELECT DISTINCT bp.id, bp.pack_code, bp.capacity_mah FROM battery_pack bp "
        "JOIN flight_battery fb ON fb.battery_pack_id = bp.id "
        "JOIN flight f ON f.id = fb.flight_id WHERE f.airframe_id = ? ORDER BY bp.pack_code",
        (airframe_id,),
    ).fetchall()
    for pack_id, code, rated in rows:
        got = packs.history(store, reg, str(pack_id), flights)
        if not got:
            continue
        label = next((x.label_mah for x in got if x.label_mah), None)
        out["packs"].append({
            "code": str(code), "rated_mah": float(rated) if rated else label,
            "flights": [{"capacity_mah": x.capacity_mah, "resistance_mohm": x.resistance_mohm}
                        for x in got],
        })  # fmt: skip
    return out


def _builds(store: Store, airframe_id: str, i: Inputs, model: Model,
            rules: Mapping[str, Any], mass: float, density: float, top: float,
            f: Any) -> dict[str, Any]:  # fmt: skip
    """The platform's current parts against its candidate set, from the bench tables a
    person entered: the tables, the flown hover power against the current table, and
    the candidate's figures, power curve and range against weight beside the flown
    build's. Left at the tables while either set is missing or a hover thrust runs past
    a table."""
    assert i.pack is not None
    row = store.get("airframe", airframe_id)
    assert row is not None
    tables = bench.benches(store, str(row["platform_id"]))
    out: dict[str, Any] = {"platform_id": str(row["platform_id"]),
                           "tables": [b.view() for b in tables], "motors": i.motors}  # fmt: skip
    found: dict[str, bench.Bench] = {}
    for b in tables:
        found.setdefault(b.role, b)
    current, candidate = found.get("current"), found.get("candidate")

    def per_motor(kg: float) -> float:
        return kg * 1000.0 / i.motors

    if current is not None:
        # flown hover power per motor against the bench at the same thrust
        flown = float(model.power(mass * G, 0.0, 0.0, density, rules)[()]) - model.avionics
        at = current.power_at(per_motor(mass))
        out["flown_vs_bench"] = {"thrust_g": per_motor(mass), "flown_w": flown / i.motors,
                                 "bench_w": at,
                                 "share": flown / i.motors / at if at else None}  # fmt: skip
    if current is None or candidate is None:
        out["missing"] = "current" if current is None else "candidate"
        return out
    delta = (i.motors * (candidate.motor_mass_g - current.motor_mass_g) / 1000.0
             if candidate.motor_mass_g and current.motor_mass_g else 0.0)  # fmt: skip
    heavy, empty = mass + delta, (i.empty_kg or mass) + delta
    c = bench.compare(current, candidate, per_motor(heavy))
    out["compare"] = c | {"mass_kg": heavy, "delta_kg": delta}
    out["headroom"] = {
        "current": current.max_thrust_g * i.motors / (mass * 1000.0),
        "candidate": candidate.max_thrust_g * i.motors / (heavy * 1000.0),
    }
    if c["share"] is None:
        return out
    other = bench.propulsion(model, float(c["share"]))
    curve_b = pack_curve(other, rules, i.pack, 1.0, i.failsafe, i.reserve_share,
                         [empty, heavy, (i.max_kg or mass) + delta], density, top)  # fmt: skip
    g = figures(other, rules, curve_b, heavy, empty, density, top, None)
    step = float(rules["report"]["curve_step_ms"])
    v = np.arange(0.0, top + step / 2, step)
    zeros = np.zeros_like(v)
    out["figures"] = {"current": asdict(f), "candidate": asdict(g) if g else None}
    out["power"] = {
        "airspeed_ms": v.tolist(),
        "current_w": np.asarray(model.power(np.full(len(v), mass * G), v, zeros, density,
                                            rules)).tolist(),
        "candidate_w": np.asarray(other.power(np.full(len(v), heavy * G), v, zeros, density,
                                              rules)).tolist(),
    }  # fmt: skip
    if i.empty_kg is not None and i.max_kg is not None:
        rows: list[dict[str, Any]] = []
        for kg in np.linspace(i.empty_kg, i.max_kg, int(rules["projections"]["weight_steps"])):
            share = bench.compare(current, candidate, per_motor(float(kg) + delta))["share"]
            got = (figures(bench.propulsion(model, float(share)), rules, curve_b,
                           float(kg) + delta, empty, density, top, None, trips=())
                   if share is not None else None)  # fmt: skip
            rows.append(
                {
                    "mass_kg": float(kg),
                    "range_m": got.range_m if got else None,
                    "endurance_s": got.endurance_s if got else None,
                }
            )
        out["against_weight"] = rows
    return out


def _curves(fitted: Fitted, am_flights: Sequence[FlightPoints], rules: Mapping[str, Any],
            curve: Usable, masses: Sequence[float], mass: float, density: float, top: float,
            ) -> dict[str, Any]:  # fmt: skip
    """What the power and speed charts draw: the power against airspeed at each weight with
    its 95% band, and at the chosen weight the energy per km over the ground into each
    headwind and the flight time; the curves reach past the fastest level airspeed flown,
    which the screen marks. With the flights' hover and level points (airspeed, power,
    weight and seconds only)."""
    spec: Mapping[str, Any] = rules["report"]
    end = max(top * float(spec["curve_beyond"]), top + float(spec["curve_beyond_ms"]))
    v = np.arange(0.0, end + 1e-9, float(spec["curve_step_ms"]))
    zeros = np.zeros(len(v))
    models = draws(fitted, rules)
    tail = (1.0 - float(rules["projections"]["level"])) / 2 * 100
    power: list[dict[str, Any]] = []
    for kg in masses:
        w = np.full(len(v), kg * G)
        mid = np.asarray(fitted.model.power(w, v, zeros, density, rules))
        drawn = np.array([np.asarray(m.power(w, v, zeros, density, rules)) for m in models])
        # the band laid about the model's own curve, as the figures' intervals are
        low = mid + np.percentile(drawn, tail, axis=0) - np.median(drawn, axis=0)
        high = mid + np.percentile(drawn, 100 - tail, axis=0) - np.median(drawn, axis=0)
        power.append({"mass_kg": kg, "power_w": mid.tolist(), "low_w": low.tolist(),
                      "high_w": high.tolist()})  # fmt: skip
    at = np.asarray(fitted.model.power(np.full(len(v), mass * G), v, zeros, density, rules))
    # over the ground: none where the headwind leaves no way to make
    per_km = {
        str(h): [float(p / (s - h) / 3.6) if s - h > 0.5 else None
                 for s, p in zip(v, at, strict=True)]
        for h in (float(x) for x in spec["headwinds_ms"])
    }  # fmt: skip
    points = [
        {"flight": k, "mass_kg": f.mass_kg, "phase": p.phase, "airspeed_ms": p.airspeed,
         "power_w": p.power, "seconds": p.seconds}
        for k, f in enumerate(am_flights) if f.mass_kg is not None
        for p in f.points if p.phase in ("hover", "level")
    ]  # fmt: skip
    return {
        "airspeed_ms": v.tolist(), "top_ms": top, "power": power,
        "wh_per_km": per_km, "flight_s": np.asarray(curve.seconds(at)).tolist(),
        "points": points,
    }  # fmt: skip


def report(store: Store, cache: ColumnCache, pack: KnowledgePack, airframe_id: str,
           mass_kg: float | None = None, air: str = "usual", trip: str = "out_and_back",
           wind_ms: float = 0.0) -> dict[str, Any]:  # fmt: skip
    """The airframe's figures at a weight, in its usual or standard air, with the radius of
    a standard trip in a wind; each section left out with what it needs when it cannot be
    worked out."""
    reg = Registry.load(pack)
    rules: Mapping[str, Any] = reg.rules["performance"]
    i = gather(store, cache, pack, airframe_id)
    out: dict[str, Any] = {
        "airframe_id": airframe_id, "missing": dict(i.missing), "sources": i.sources,
        "level": i.model.fitted.level if i.model.fitted else None, "why": i.model.why,
    }  # fmt: skip
    model = i.model.fitted.model if i.model.fitted else None
    mass, mass_from = _weight(i, mass_kg)
    density = STANDARD_DENSITY if air == "standard" else (i.usual_density or STANDARD_DENSITY)
    out["inputs"] = {
        "mass_kg": mass, "mass_from": mass_from, "density": density, "air": air,
        "empty_kg": i.empty_kg, "max_kg": i.max_kg, "flown_kg": i.flown_kg, "top_ms": i.top_ms,
        "reserve_share": i.reserve_share, "trip": trip, "wind_ms": wind_ms,
        "speed_goal": i.answers.get("speed_goal") or "efficient",
    }  # fmt: skip
    if model is None or mass is None:
        if mass is None:
            out["missing"]["weight"] = mass_from
        return _round(out)
    if i.top_ms is None:
        # it has only hovered: the hover time is all its own measurements tell
        if i.pack is not None and i.model.fitted is not None:
            curve = pack_curve(model, rules, i.pack, 1.0, i.failsafe, i.reserve_share, [mass],
                               density, 0.0)  # fmt: skip
            out["hover"] = {
                "mass_kg": mass, "measured": measured(rules, i.flown_kg, mass),
                "hover_s": asdict(hover_spread(i.model.fitted, rules, curve, mass, density)),
            }  # fmt: skip
        return _round(out)
    top = i.top_ms
    w = mass * G
    if i.pack is None:
        out["speeds"] = _speeds(model, rules, w, density, top, terminals)
        return _round(out)
    heaviest = i.max_kg or mass
    masses = [m for m in (i.empty_kg, mass, heaviest) if m is not None]
    curve = pack_curve(model, rules, i.pack, 1.0, i.failsafe, i.reserve_share, masses, density, top)
    out["speeds"] = _speeds(model, rules, w, density, top, per_pack(curve))
    f = figures(model, rules, curve, mass, i.empty_kg or mass, density, top, i.flown_kg)
    out["figures"] = asdict(f) if f is not None else None
    if f is None:
        out["missing"]["hover"] = "the pack cannot hold a hover at this weight"
        return _round(out)
    assert i.model.fitted is not None
    # what ends each figure: the reserve, or a failsafe the parameters set sooner
    out["failsafe"] = asdict(i.failsafe)
    loads = {
        "hover": float(model.power(w, 0.0, 0.0, density, rules)),
        "endurance": float(model.power(w, f.endurance_ms, 0.0, density, rules)),
        "range": float(model.power(w, f.range_ms, 0.0, density, rules)),
    }
    out["ends"] = {k: curve.stopped(v) for k, v in loads.items()}
    out["battery"] = _battery(store, reg, airframe_id, i.flights, curve, loads)
    out["builds"] = _builds(store, airframe_id, i, model, rules, mass, density, top, f)
    out["spread"] = {k: asdict(v) for k, v in
                     spread(i.model.fitted, rules, curve, mass, i.empty_kg or mass, density,
                            top).items()}  # fmt: skip
    out["curves"] = _curves(i.model.fitted, i.model.flights, rules, curve,
                            sorted(set(masses)), mass, density, top)  # fmt: skip
    cruise = i.params.get("WP_SPD")
    out["wp_spd_ms"] = float(cruise) if cruise else None
    if i.empty_kg is not None and i.max_kg is not None:
        out["against_weight"] = [asdict(x) for x in against_weight(
            model, rules, curve, i.empty_kg, i.max_kg, density, top, i.flown_kg)]  # fmt: skip
    else:
        out["missing"]["against_weight"] = "enter the empty and heaviest take-off weights"
    t = standard(trip if trip in TRIPS else "out_and_back", rules,
                 max(mass - (i.empty_kg or mass), 0.0))  # fmt: skip
    least, by = worst_trip(model, rules, curve, mass, density, t, wind_ms, top)
    out["radius"] = {"trip": t.kind, "wind_ms": wind_ms, "worst_m": least, "by_direction": by}
    # the radius against wind speed: the worst way round, and with the wind across
    radius_wind: list[dict[str, Any]] = []
    for wind in (float(x) for x in rules["report"]["radius_winds_ms"]):
        worst, around = worst_trip(model, rules, curve, mass, density, t, wind, top)
        across = next((m for d, m in around if abs(d - 90.0) < 1e-6), None)
        radius_wind.append({"wind_ms": wind, "worst_m": worst, "across_m": across})
    out["radius_wind"] = radius_wind
    out["headwind"] = [_headwind(model, w, density, rules, top, h)
                       for h in rules["report"]["headwinds_ms"]]  # fmt: skip
    # each lever's change as a share of what it changes; the weight's effect is measured
    # once the flights span weights, the rest the model's alone
    spans = i.flown_kg is not None and i.flown_kg[1] > i.flown_kg[0]
    base = {"mass": mass, "drag": model.drag_area, "avionics": model.avionics,
            "capacity": i.pack.capacity_ah}  # fmt: skip
    out["what_ifs"] = [
        asdict(x) | {"share": x.change / base[x.lever] if base[x.lever] else None,
                     "basis": "measured" if x.lever == "mass" and spans else "theory"}
        for x in what_ifs(model, rules, i.pack, 1.0, i.failsafe, i.reserve_share, mass,
                          i.empty_kg or mass, i.pack_mass_kg, density, top)
        if x.change != 0
    ]  # fmt: skip
    if i.basis is not None:
        volts = lowest_volts(i.params, i.pack)
        ts = top_speed(model, rules, reg.rules["wind"]["envelope"], i.params, i.basis, mass,
                       density, volts, i.current_a, top)  # fmt: skip
        out["top_speed"] = asdict(ts)
        env = EnvelopeInputs(model, i.basis, i.params, volts, i.current_a, i.thermals,
                             i.esc_limit_c, i.motors)  # fmt: skip
        s = next_step(i.fast, env, mass, density, reg)
        out["build_up"] = {
            "verified_ms": s.verified.speed_ms if s.verified else None,
            "verified_flight": s.verified.flight_id if s.verified else None,
            "next_ms": s.next_ms, "checks": [asdict(c) for c in s.checks],
            "missing": list(s.missing), "binding": s.binding, "offered": s.offered,
        }  # fmt: skip
    return _round(out)


def range_card(store: Store, cache: ColumnCache, pack: KnowledgePack,
               airframe_id: str) -> dict[str, Any]:  # fmt: skip
    """The printable card: a row for each of the empty, typical and heaviest take-off
    weights, each figure at the low end of its interval; the cruise speed the setup's
    Priority calls for (best endurance for endurance, else best range); the radius out and
    back in still air and the rules' winds, the worst way round; the most it can fly and
    what limits it; the best ground speed into each headwind; and what it is good for:
    the weights and speeds flown, and the latest flight's parameters. The radius in each
    wind is the model's; in still air also its low end. An airframe that has only hovered
    gets the hover time alone."""
    reg = Registry.load(pack)
    rules: Mapping[str, Any] = reg.rules["performance"]
    i = gather(store, cache, pack, airframe_id)
    fit = i.model.fitted
    out: dict[str, Any] = {"airframe_id": airframe_id, "missing": dict(i.missing),
                           "sources": i.sources, "rows": []}  # fmt: skip
    if fit is None or i.pack is None:
        out["missing"].setdefault("model", i.model.why or "no power model yet")
        return _round(out)
    density = i.usual_density or STANDARD_DENSITY
    weights = sorted({m for m in (i.empty_kg, i.typical_kg, i.max_kg) if m is not None})
    if not weights and i.flown_kg is not None:
        weights = sorted(set(i.flown_kg))
    top = i.top_ms
    endurance = i.answers.get("priority") == "endurance"
    curve = pack_curve(fit.model, rules, i.pack, 1.0, i.failsafe, i.reserve_share, weights,
                       density, top or 0.0)  # fmt: skip
    trip = standard("out_and_back", rules)
    volts = lowest_volts(i.params, i.pack)
    for kg in weights:
        if top is None:  # only hovered: the hover time alone
            h = hover_spread(fit, rules, curve, kg, density)
            out["rows"].append({"mass_kg": kg, "hovers": True, "measured":
                                measured(rules, i.flown_kg, kg), "hover_min": h.low / 60}
                               if h.median > 0 else {"mass_kg": kg, "hovers": False})  # fmt: skip
            continue
        f = figures(fit.model, rules, curve, kg, i.empty_kg or kg, density, top, i.flown_kg,
                    trips=("out_and_back",))  # fmt: skip
        if f is None:
            out["rows"].append({"mass_kg": kg, "hovers": False})
            continue
        low = spread(fit, rules, curve, kg, i.empty_kg or kg, density, top)
        w = kg * G
        speeds = best(fit.model, w, density, rules, top, per_pack(curve))
        winds = rules["report"]["headwinds_ms"]
        row: dict[str, Any] = {
            "mass_kg": kg, "hovers": True, "measured": f.measured,
            "hover_min": low["hover_s"].low / 60, "endurance_min": low["endurance_s"].low / 60,
            "endurance_ms": f.endurance_ms,
            "cruise_ms": speeds.endurance if endurance else speeds.range,
            "cruise_for": "endurance" if endurance else "range",
            "range_km": low["range_m"].low / 1000,
            "radius_km": {str(h): worst_trip(fit.model, rules, curve, kg, density, trip, h,
                                             top)[0] / 1000 for h in winds},
            "headwind": [{"wind_ms": h, **({"airspeed": x.airspeed, "ground": x.ground}
                                           if (x := leg(fit.model, w, density, rules, top, -h, 0.0))
                                           else {"airspeed": None, "ground": None})}
                         for h in winds],
        }  # fmt: skip
        row["radius_still_low_km"] = low["radius_m"].low / 1000
        if i.basis is not None:
            ts = top_speed(fit.model, rules, reg.rules["wind"]["envelope"], i.params, i.basis,
                           kg, density, volts, i.current_a, top)  # fmt: skip
            row["top_speed_ms"], row["top_speed_by"] = ts.airspeed, ts.by
        v = verified(i.fast, kg, rules)
        row["verified_ms"] = v.speed_ms if v else None
        out["rows"].append(row)
    out["valid"] = {"flown_kg": i.flown_kg, "top_ms": top, "density": density,
                    "reserve_share": i.reserve_share,
                    "params_flight": i.latest_flight}  # fmt: skip
    return _round(out)
