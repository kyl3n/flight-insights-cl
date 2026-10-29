"""The Energy section of a flight's Propulsion view (spec: User interface, "Propulsion and
power", Energy; Power and efficiency analysis, "Range, endurance and speed"; mock-up
Propulsion; rules in `knowledge/rules/energy.json` "view").

- **The figures:** the energy, charge and distance the energy analyzer measures
  (`analyzers/energy.py` `flight_energy`, from every sample), the share of the packs'
  capacity used, energy per km over the whole flight and at cruise, the climb's energy per
  metre and the part of it above hovering, and the hover power at the flight's air and at
  standard air.
- **Where the energy went:** the packs' energy by flight phase. When every motor's ESC
  reports its voltage and current over the whole flight (never on the simulator, whose
  ESC current and voltage are placeholders), the phases are the ESCs' input in each, and
  the harness (the pack's voltage less the ESCs', times its current) and the avionics
  (what is left) are their own parts. Battery heat, each pack's current squared times its
  logged resistance, is energy the cells gave that never reached the terminals.
- **Pack power against time**, reduced for display only (`views.envelope`), with the steady
  tiles' phases and the fast segment (`analyzers/fast.py`) as spans of log time.
- **On the airframe's curve:** the flight's points (`performance/points.py`: steady tiles
  averaged per phase and 1 m/s of airspeed) in hover and level flight, with the airframe's
  model at the flight's weight and mean air and its bootstrap band. How far the flight sits
  from the model is judged against the model fitted without it (`performance/efficiency.py`),
  so it cannot pull the curve toward itself, with a 95% interval over its own points, and
  the energy anomaly findings that judgement raises.
- **At speed:** the fast segment's watched metrics, as the flight's analysis stored them.

Nothing here leaves the laptop: the view is drawn by the app's own UI. The distance is how
far the aircraft went, never where; no position is read.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

from flight_insights.analyzers import steady, wind
from flight_insights.analyzers.battery_power import PREFIXES, Supply, supply
from flight_insights.analyzers.energy import between, flight_energy, running
from flight_insights.analyzers.fast import Fast, segment
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.motors_health import motors
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si
from flight_insights.knowledge import Pack
from flight_insights.performance import airframe as perf
from flight_insights.performance.efficiency import Against, against, anomaly, excess
from flight_insights.performance.fit import G
from flight_insights.performance.packs import measured
from flight_insights.performance.points import Point
from flight_insights.performance.project import STANDARD_DENSITY
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store
from flight_insights.views import MARGIN_US, envelope

Array = np.ndarray[Any, Any]
SHOWN = ("hover", "level")  # the points drawn on the level-flight curve


class EnergyViewError(ValueError):
    """A flight the view cannot show."""


def _latest(store: Store, flight_id: str) -> tuple[str, list[dict[str, Any]]] | None:
    row = store.conn.execute(
        "SELECT id, evidence_json FROM analysis_run WHERE flight_id = ? "
        "ORDER BY created_hlc DESC LIMIT 1",
        (flight_id,),
    ).fetchone()
    return None if row is None else (str(row[0]), list(json.loads(row[1])))


def _r(x: float | None, digits: int = 1) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), digits)


def _capacity(store: Store, reg: Registry, flight: FlightData, packs: Supply,
              flight_id: str) -> tuple[float | None, str]:  # fmt: skip
    """The packs' capacity, mAh: as measured over the airframe's latest flights when every
    counted monitor has it, else as the parameters set it."""
    names = [PREFIXES[i] for i in packs.instances if 0 <= i < len(PREFIXES)]
    got = measured(store, reg, flight_id)
    if names and all(n in got for n in names):
        return sum(got[n].capacity_mah for n in names), "measured"
    set_mah = [float(flight.params.get(f"{n}_CAPACITY", 0.0)) for n in names]
    if names and all(c > 0 for c in set_mah):
        return sum(set_mah), "parameters"
    return None, "none"


def _escs(flight: FlightData, packs: Supply, max_gap_s: float) -> tuple[Array, Array] | str:
    """The ESCs' summed input power and the harness loss, W, at the packs' rows; or why
    there are none."""
    if flight.simulated:
        return "the simulator's ESC current and voltage are placeholders"
    esc = flight.table("ESC", ["TimeUS", "Instance", "Volt", "Curr"])
    if esc is None or not esc.num_rows or not {"Volt", "Curr"} <= set(esc.column_names):
        return "the log has no ESC voltage and current"
    t = np.asarray(esc.column("TimeUS").to_numpy(), np.int64)
    inst = np.asarray(esc.column("Instance").to_numpy(), np.int64)
    volt, curr = si.values(esc, "Volt"), si.values(esc, "Curr")
    wanted = [m.channel - 1 for m in motors(flight.params)]
    if not wanted or not set(wanted) <= {int(i) for i in np.unique(inst)}:
        return "not every motor's ESC reports"
    power = np.zeros(len(packs.t))
    volts: list[Array] = []
    for i in wanted:
        rows = inst == i
        ti, k = t[rows], np.argsort(t[rows], kind="stable")
        ti, vi, ai = ti[k], volt[rows][k], curr[rows][k]
        inside = ti[(ti >= flight.arm_us) & (ti <= flight.disarm_us)]
        edges = np.concatenate(([flight.arm_us], inside, [flight.disarm_us])) / 1e6
        if len(inside) < 2 or float(np.max(np.diff(edges))) > max_gap_s:
            return "an ESC's telemetry has gaps over the flight"
        if not (vi > 0).any():
            return "an ESC reports no voltage"
        power += np.interp(packs.t, ti, vi * ai)
        volts.append(np.interp(packs.t, ti, vi))
    harness = np.maximum(packs.volt - np.median(np.array(volts), axis=0), 0.0) * packs.current
    return power, harness


def breakdown(flight: FlightData, reg: Registry, packs: Supply, found: steady.Tiles,
               labels: Array, used_wh: float, phase_wh: Mapping[str, float]
               ) -> dict[str, Any]:  # fmt: skip
    """The packs' energy by use, Wh, adding up to what the cells gave."""
    view: Mapping[str, Any] = reg.rules["energy"]["view"]
    a, b = flight.arm_us, flight.disarm_us
    parts = {k: float(v) for k, v in phase_wh.items()}
    split = _escs(flight, packs, float(reg.rules["batteries"]["max_gap_s"]))
    why: str | None = None
    if isinstance(split, str):
        why = split
    else:
        esc_j, harness_j = running(packs.t, split[0]), running(packs.t, split[1])
        esc_wh = between(packs.t, esc_j, a, b) / 3600.0
        harness_wh = between(packs.t, harness_j, a, b) / 3600.0
        avionics_wh = used_wh - esc_wh - harness_wh
        if avionics_wh < -float(view["max_esc_excess"]) * used_wh:
            why = "the ESCs report more power than the packs give: their current reads high"
        else:
            parts = dict.fromkeys(phase_wh, 0.0)
            for s, e, phase in zip(found.start_us, found.end_us, labels, strict=True):
                parts[str(phase)] += between(packs.t, esc_j, int(s), int(e)) / 3600.0
            parts["untiled"] = esc_wh - sum(v for k, v in parts.items() if k != "untiled")
            parts["harness"] = harness_wh
            parts["avionics"] = max(avionics_wh, 0.0)
    heat_wh: float | None = None
    ohms = np.nan_to_num(packs.resistance, nan=0.0)
    if (ohms > 0).any():
        heat_w = np.sum(packs.currents**2 * ohms[:, None], axis=0)
        heat_wh = between(packs.t, running(packs.t, heat_w), a, b) / 3600.0
        parts["heat"] = heat_wh
    return {
        "parts": [{"part": k, "wh": round(v, 2)} for k, v in parts.items()],
        "terminal_wh": round(used_wh, 2),
        "cells_wh": None if heat_wh is None else round(used_wh + heat_wh, 2),
        "split": why is None,
        "split_why": why,
        "heat_why": None if heat_wh is not None else "the packs logged no resistance",
    }


def _climb(reg: Registry, found: steady.Tiles, labels: Array,
           phase_wh: Mapping[str, float], phase_s: Mapping[str, float]
           ) -> dict[str, Any] | None:  # fmt: skip
    """Energy per metre climbed, and the part of it above hovering."""
    up = labels == "climb"
    seconds = (found.end_us[up] - found.start_us[up]) / 1e6
    metres = float(np.sum(found.climb[up] * seconds))
    if metres < float(reg.rules["energy"]["view"]["min_climb_m"]):
        return None
    climb_s = float(np.sum(seconds))
    out: dict[str, Any] = {
        "wh_per_m": round(phase_wh["climb"] / metres, 3),
        "rate_ms": round(metres / climb_s, 2),
        "metres": round(metres),
        "above_hover_wh_per_m": None,
    }
    if phase_s["hover"] > 0:
        hover_w = phase_wh["hover"] * 3600.0 / phase_s["hover"]
        above = phase_wh["climb"] - hover_w * climb_s / 3600.0
        out["above_hover_wh_per_m"] = round(above / metres, 3)
    return out


def _spans(found: steady.Tiles, labels: Array) -> list[dict[str, Any]]:
    """Back-to-back tiles of one phase as one span, in log seconds."""
    out: list[dict[str, Any]] = []
    for s, e, phase in zip(found.start_us.tolist(), found.end_us.tolist(), labels, strict=True):
        if out and out[-1]["phase"] == str(phase) and s <= out[-1]["end_us"]:
            out[-1]["end_us"] = e
        else:
            out.append({"phase": str(phase), "start_us": s, "end_us": e})
    return [{"phase": x["phase"], "start_s": round(x["start_us"] / 1e6, 3),
             "end_s": round(x["end_us"] / 1e6, 3)} for x in out]  # fmt: skip


def _mean(points: list[Point], value: Callable[[Point], float]) -> float:
    return float(np.average([value(p) for p in points], weights=[p.seconds for p in points]))


def _compared(a: Against, points: list[Point], view: Mapping[str, Any],
              rules: Mapping[str, Any]) -> dict[str, Any]:  # fmt: skip
    """The points' excess over the model without the flight, with a 95% interval over
    resamples of the points."""
    drawn = np.array([p.power * p.seconds for p in points])
    predicted = a.predicted(points, rules) * np.array([p.seconds for p in points])
    rng = np.random.default_rng(int(view["seed"]))
    picks = rng.integers(0, len(points), (int(view["interval_draws"]), len(points)))
    draws = drawn[picks].sum(axis=1) / predicted[picks].sum(axis=1) - 1.0
    low, high = np.percentile(draws, [2.5, 97.5])
    by = {ph: _r(100 * excess(a, got, rules)) if (got := [p for p in points if p.phase == ph])
          else None for ph in SHOWN}  # fmt: skip
    return {
        "excess_pct": _r(100 * excess(a, points, rules)),
        "interval_pct": [_r(100 * float(low)), _r(100 * float(high))],
        "by_phase_pct": by,
        "threshold_pct": _r(100 * a.threshold),
        "others": a.others,
    }


def _on_curve(store: Store, cache: ColumnCache, pack: Pack, reg: Registry, flight_id: str,
              airframe_id: str | None, evidence: list[dict[str, Any]]
              ) -> tuple[dict[str, Any], list[dict[str, Any]]]:  # fmt: skip
    """The flight's points on its airframe's curve, and the anomaly findings."""
    rules: Mapping[str, Any] = reg.rules["performance"]
    view: Mapping[str, Any] = reg.rules["energy"]["view"]
    out: dict[str, Any] = {"why": None, "points": [], "mass_kg": None, "mass_from": None,
                           "density_ratio": None, "wind_from": None, "level": None,
                           "curve": None, "hover": None, "against": None}  # fmt: skip
    if airframe_id is None:
        return out | {"why": "the flight has no airframe"}, []
    am = perf.load(store, cache, pack, airframe_id)
    mine = next((f for f in am.flights if f.flight_id == flight_id), None)
    if mine is None:
        why = am.skipped.get(flight_id) or "the flight gave the power model no points"
        return out | {"why": why}, []
    shown = [p for p in mine.points if p.phase in SHOWN]
    out |= {
        "points": [{"phase": p.phase, "airspeed_ms": round(p.airspeed, 2),
                    "climb_ms": round(p.climb, 2), "power_w": round(p.power),
                    "seconds": round(p.seconds, 1), "tiles": p.tiles} for p in mine.points],
        "mass_kg": _r(mine.mass_kg), "mass_from": mine.mass_from, "wind_from": mine.wind_from,
    }  # fmt: skip
    if mine.points:
        out["density_ratio"] = round(_mean(list(mine.points), lambda p: p.density)
                                     / STANDARD_DENSITY, 3)  # fmt: skip
    fitted = am.fitted
    if fitted is None or mine.mass_kg is None or not shown:
        why = am.why or ("the flight's weight is not known" if mine.mass_kg is None
                         else "the flight has no hover or level points")  # fmt: skip
        return out | {"why": why}, []
    density = _mean(shown, lambda p: p.density)
    top = max(float(view["curve_top_ms"]),
              max(p.airspeed for p in shown) + float(view["curve_margin_ms"]))  # fmt: skip
    speeds = np.arange(0.0, top + 1e-9, float(view["curve_step_ms"]))
    weight = np.full(len(speeds), mine.mass_kg * G)
    zeros = np.zeros(len(speeds))

    def at(model: Any, rho: float) -> Array:
        return np.asarray(model.power(weight, speeds, zeros, np.full(len(speeds), rho), rules))

    curve: dict[str, Any] = {
        "airspeed_ms": [round(float(v), 2) for v in speeds],
        "power_w": [round(float(v)) for v in at(fitted.model, density)],
        "low_w": None, "high_w": None,
    }  # fmt: skip
    if len(fitted.replicates) >= int(view["min_replicates"]):
        band = np.array([at(m, density) for m in fitted.replicates])
        low, high = np.percentile(band, [2.5, 97.5], axis=0)
        curve["low_w"] = [round(float(v)) for v in low]
        curve["high_w"] = [round(float(v)) for v in high]
    hover = [p for p in shown if p.phase == "hover"]
    measured_w = _mean(hover, lambda p: p.power) if hover else None
    hover_out: dict[str, Any] | None = None
    if measured_w is not None:
        rho = _mean(hover, lambda p: p.density)
        here = float(fitted.model.power(mine.mass_kg * G, 0.0, 0.0, rho, rules))
        standard = float(fitted.model.power(mine.mass_kg * G, 0.0, 0.0, STANDARD_DENSITY, rules))
        hover_out = {"measured_w": round(measured_w), "model_w": round(here),
                     "standard_w": round(measured_w * standard / here),
                     "model_standard_w": round(standard)}  # fmt: skip
    out |= {"level": fitted.level, "curve": curve, "hover": hover_out}
    a = against(flight_id, am, rules)
    if a is None:
        return out, []
    out["against"] = _compared(a, shown, view, rules)
    return out, anomaly(flight_id, am, rules, {"evidence": evidence})


def _watch(store: Store, run_id: str, evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    seg = next((e for e in evidence if e.get("id") == "fast:segment"), None)
    if seg is None:
        return None
    rows = store.conn.execute(
        "SELECT metric, value, zone, evidence_id FROM metric_value WHERE run_id = ? AND "
        "segment = 'fast' ORDER BY metric",
        (run_id,),
    ).fetchall()
    return {
        "values": seg.get("values", {}),
        "metrics": [{"metric": str(m), "value": float(v), "zone": z, "evidence": ev}
                    for m, v, z, ev in rows],
    }  # fmt: skip


def energy(store: Store, cache: ColumnCache, pack: Pack, reg: Registry, flight_id: str,
           opened: Callable[[str], FlightData]) -> dict[str, Any]:  # fmt: skip
    """The Energy section of one flight."""
    flight = opened(flight_id)
    if flight.vehicle != "copter" or flight.fingerprint.get("vehicle_class") not in (
        None, "multirotor",
    ):  # fmt: skip
        raise EnergyViewError("energy is worked out for multirotors")
    e = wind.ekf(flight)
    if e is None:
        raise EnergyViewError("no estimator velocity (XKF1) in the log")
    packs = supply(flight, reg.rules["batteries"])
    if packs is None or not packs.instances:
        raise EnergyViewError("no battery monitor measures the packs' power")
    rules: Mapping[str, Any] = reg.rules["energy"]
    fe = flight_energy(e, packs, flight.arm_us, flight.disarm_us, rules["phases"],
                       float(reg.rules["batteries"]["max_gap_s"]))  # fmt: skip
    found = steady.tiles(e, flight.arm_us, flight.disarm_us, float(rules["phases"]["tile_s"]),
                         int(rules["phases"]["min_samples"]))  # fmt: skip
    labels = steady.phases(found, rules["phases"])
    least = float(rules["flight"]["min_distance_m"])
    capacity, capacity_from = _capacity(store, reg, flight, packs, flight_id)
    fast = segment(flight, reg, e)
    row = store.get("flight", flight_id) or {}
    latest = _latest(store, flight_id)
    evidence = latest[1] if latest else []
    on_curve, findings = _on_curve(store, cache, pack, reg, flight_id, row.get("airframe_id"),
                                   evidence)  # fmt: skip
    level = [p for p in on_curve["points"] if p["phase"] == "level"]
    cruise_ms = (
        float(np.average([p["airspeed_ms"] for p in level], weights=[p["seconds"] for p in level]))
        if level else None
    )  # fmt: skip
    start, end = (flight.arm_us - MARGIN_US) / 1e6, (flight.disarm_us + MARGIN_US) / 1e6
    return {
        "flight_id": flight_id,
        "simulated": flight.simulated,
        "window_s": [round(start, 3), round(end, 3)],
        "armed_s": [round(flight.arm_us / 1e6, 3), round(flight.disarm_us / 1e6, 3)],
        "figures": {
            "used_wh": round(fe.used_wh, 1),
            "used_mah": round(fe.used_mah),
            "capacity_mah": None if capacity is None else round(capacity),
            "capacity_from": capacity_from,
            "share_pct": None if capacity is None else round(100 * fe.used_mah / capacity, 1),
            "flown_m": round(fe.flown_m),
            "seconds": round((flight.disarm_us - flight.arm_us) / 1e6, 1),
            "wh_per_km": _r(fe.wh_per_km) if fe.flown_m >= least else None,
            "cruise_wh_per_km": _r(fe.cruise_wh_per_km) if fe.level_m >= least else None,
            "cruise_airspeed_ms": _r(cruise_ms),
            "bridged_s": round(fe.bridged_s, 1),
            "monitors": len(packs.instances),
            "read_from": packs.how,
            "climb": _climb(reg, found, labels, fe.phase_wh, fe.phase_s),
        },
        "phase_s": {k: round(v, 1) for k, v in fe.phase_s.items()},
        "breakdown": breakdown(flight, reg, packs, found, labels, fe.used_wh, fe.phase_wh),
        "power": envelope(packs.t / 1e6, packs.power, start, end),
        "phases": _spans(found, labels),
        "fast": None if isinstance(fast, str) else _fast(fast),
        "fast_why": fast if isinstance(fast, str) else None,
        "on_curve": on_curve,
        "findings": findings,
        "watch": None if latest is None else _watch(store, latest[0], evidence),
    }


def _fast(f: Fast) -> dict[str, Any]:
    return {
        "spans_s": [[round(a / 1e6, 3), round(b / 1e6, 3)] for a, b in f.spans],
        "seconds": round(f.seconds, 1), "speed_ms": round(f.speed, 2),
        "top_ms": round(f.top, 2), "speed_from": "air" if f.air else "ground",
    }  # fmt: skip
