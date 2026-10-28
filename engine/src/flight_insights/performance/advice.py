"""What the plan proposes from the airframe's power model (spec: Power and efficiency
analysis, "Range, endurance and speed", "Recommendations"): the cruise speed the setup's
Priority calls for, and the speed to fly home.

- **Cruise (`WP_SPD`):** for endurance, the airspeed of least power; otherwise the ground
  speed to set for out and back with the least energy per km of radius, averaged over the
  winds the airframe has met (each flight's measured `wind.speed_ms`, leaving out any it
  could not make way against), each from every direction. ArduPilot holds the set ground
  speed on both legs, at whatever airspeed the wind takes (`optimize.climate`).
- **Home (`RTL_SPEED_MS`):** the ground speed of least energy per metre into the
  airframe's typical wind (the median of those measured) as a headwind.
- **Within what was measured.** Speeds are searched up to the fastest level airspeed flown
  (into the headwind, its airspeed). A setting past that is only lowered when the best
  lies inside the span with the cost already rising at its end: past the end it only
  rises further. Its cost is then read at the end, so the gain is a lower bound.
- Nothing is proposed while the setting is within the rules' band of the best, nor when
  the best is slower than the rules' least cruise speed (a hover is the best endurance).
- **Lowering** is always proposed. **Raising** goes only up to the verified speed (the
  fastest fast segment held at this weight or heavier with everything in target,
  `envelope.verified`) less the typical wind, so the airspeed a headwind adds stays within
  what was verified. The same limit lets the plan's never-raise rule pass a raise of
  `WP_SPD`, `LOIT_SPEED_MS` or `RTL_SPEED_MS` up to it. When the best cruise speed lies
  past it, a finding says so: flying faster at this weight verifies more.
- Each finding is low confidence until the model is checked against the airframe's own
  flights to reserve (spec: Testing, "Range and speed").
- They rest on the flight's own energy (`energy:flight`).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from flight_insights.analyzers.registry import Registry
from flight_insights.knowledge import Pack as KnowledgePack
from flight_insights.performance.airframe import PerformanceError
from flight_insights.performance.envelope import verified
from flight_insights.performance.fit import G
from flight_insights.performance.optimize import Array, climate, grid, held
from flight_insights.performance.project import STANDARD_DENSITY, pack_curve, per_pack
from flight_insights.performance.report import gather
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store
from flight_insights.trends import analysed_flights, metric_of

CRUISE = "performance.cruise_speed"
UNVERIFIED = "performance.cruise_unverified"
HOME = "performance.rtl_speed"
EVIDENCE = "energy:flight"


@dataclass
class Advice:
    findings: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    raise_limits: dict[str, float] = field(
        default_factory=dict[str, float]
    )  # the most a raise sets


@dataclass(frozen=True)
class Choice:
    best: float  # m/s, the least cost within the span
    proposed: float | None  # the setting to propose; None: none
    unverified: bool  # the best is a raise past the limit
    loss_pct: float  # what the current setting costs over the proposed one
    gain_pct: float  # what the proposed one saves on the current


def _round(x: float, step: float) -> float:
    return round(round(x / step) * step, 2)


def _down(x: float, step: float) -> float:
    return round(math.floor(x / step + 1e-9) * step, 2)


def choose(cost: Callable[[Array], Array], span: float, current: float, rules: Mapping[str, Any],
            step: float, limit: float | None) -> Choice | None:  # fmt: skip
    """The setting to propose in place of `current`, searching up to `span`; None when the
    current one is within the band of the best, cannot be judged, or the best is slower
    than the least cruise speed."""
    speeds = grid(span, float(rules["speeds"]["step_ms"]))
    if speeds.size == 0:
        return None
    c = cost(speeds)
    k = int(np.argmin(c))
    best, share = float(speeds[k]), float(rules["speeds"]["band_share"])
    near = speeds[c <= c[k] * (1 + share)]
    if best < float(rules["advice"]["min_cruise_ms"]):
        return None
    if current <= span:
        if near.min() - 1e-9 <= current <= near.max() + 1e-9:
            return None
    elif c[-1] <= c[k] * (1 + share):
        return None  # still near the best at the span's end: past it cannot be judged

    def at(x: float) -> float:
        return float(cost(np.array([min(x, span)]))[0])

    proposed: float | None = _round(best, step)
    unverified = proposed > current and (limit is None or proposed > limit)
    if unverified:
        raised = _down(limit, step) if limit is not None else None
        proposed = raised if raised is not None and raised > current and at(raised) < at(
            current) else None  # fmt: skip
    if proposed is None or proposed == current:
        return Choice(best, None, unverified, 0.0, 0.0)
    now, then = at(current), at(proposed)
    return Choice(best, proposed, unverified, 100 * (now / then - 1), 100 * (1 - then / now))


def _finding(flight_id: str, code: str, params: dict[str, Any],
             changes: list[tuple[str, float]]) -> dict[str, Any]:  # fmt: skip
    return {
        "id": f"performance:{code.split('.')[1]}:{flight_id}", "analyzer": "performance",
        "code": code, "severity": "info", "confidence": "low", "params": params,
        "evidence": [EVIDENCE], "stage": "navigation", "origin": "measured",
        "changes": [{"param": p, "proposed": v, "stage": "navigation"} for p, v in changes],
    }  # fmt: skip


def speed_advice(store: Store, cache: ColumnCache, pack: KnowledgePack, reg: Registry,
                 flight_id: str, params: Mapping[str, float],
                 analysis: Mapping[str, Any]) -> Advice:  # fmt: skip
    """The speed findings for a flight's plan, and how far its never-raise rule may let
    the speed settings rise."""
    flight = store.get("flight", flight_id)
    if flight is None or flight["airframe_id"] is None:
        return Advice()
    if not any(e["id"] == EVIDENCE for e in analysis.get("evidence", ())):
        return Advice()
    try:
        i = gather(store, cache, pack, flight["airframe_id"])
    except PerformanceError:
        return Advice()
    fit = i.model.fitted
    if fit is None or i.pack is None or i.top_ms is None:
        return Advice()
    rules: Mapping[str, Any] = reg.rules["performance"]
    spec: Mapping[str, Any] = rules["advice"]
    own = {f.flight_id: f.mass_kg for f in i.model.flights}.get(flight_id)
    mass = own or flight.get("takeoff_kg") or i.typical_kg
    if mass is None:
        return Advice()
    top, model, kg = i.top_ms, fit.model, float(mass)
    density = i.usual_density or STANDARD_DENSITY
    w = kg * G
    curve = pack_curve(model, rules, i.pack, 1.0, i.failsafe, i.reserve_share, [kg], density,
                       top)  # fmt: skip
    cells = per_pack(curve)
    flights, _bands = analysed_flights(store, reg, flight["airframe_id"])
    found = [metric_of(store, f.run_id, "wind.speed_ms", None) for f in flights if f.run_id]
    measured = [x[0] for x in found if x is not None]
    winds = [x for x in measured if x < top]  # those it can make way against
    typical = float(np.median(winds)) if winds else 0.0
    v = verified(i.fast, kg, rules)
    limit = max(v.speed_ms - typical, 0.0) if v is not None else None
    out = Advice()
    if limit is not None:
        out.raise_limits = dict.fromkeys(spec["raise_up_to_verified"], limit)
    common = {"mass_kg": round(kg, 1), "winds": len(winds), "typical_wind_ms": round(typical, 1)}

    # cruise: what the Priority calls for
    endurance = i.answers.get("priority") == "endurance"

    def cruise_cost(g: Array) -> Array:
        if endurance:
            return cells(model.power(w, g, 0.0, density, rules))
        return climate(model, w, density, rules, winds, g, cells)

    cruise = params.get("WP_SPD")
    if cruise is not None:
        ch = choose(cruise_cost, top, cruise, rules, float(spec["cruise_step_ms"]), limit)
        if ch is not None and ch.unverified:
            out.findings.append(_finding(flight_id, UNVERIFIED, common | {
                "best_ms": round(ch.best, 1), "limit_ms": round(limit or 0.0, 1),
                "verified_ms": round(v.speed_ms, 1) if v else 0.0}, []))  # fmt: skip
        if ch is not None and ch.proposed is not None:
            out.findings.append(_finding(flight_id, CRUISE, common | {
                "current_ms": cruise, "proposed_ms": ch.proposed, "best_ms": round(ch.best, 1),
                "loss_pct": round(ch.loss_pct, 1), "gain_pct": round(ch.gain_pct, 1),
            }, [("WP_SPD", ch.proposed)]))  # fmt: skip

    # home: into the typical wind, its airspeed within the span
    home_set = params.get("RTL_SPEED_MS")
    home = home_set if home_set else cruise
    if home is not None and top > typical:
        ch = choose(lambda g: held(model, w, density, rules, g, -typical, 0.0, cells),
                     top - typical, home, rules, float(spec["home_step_ms"]), limit)  # fmt: skip
        if ch is not None and ch.proposed is not None:
            out.findings.append(_finding(flight_id, HOME, common | {
                "current_ms": home, "proposed_ms": ch.proposed, "best_ms": round(ch.best, 1),
                "loss_pct": round(ch.loss_pct, 1), "gain_pct": round(ch.gain_pct, 1),
            }, [("RTL_SPEED_MS", ch.proposed)]))  # fmt: skip
    return out
