"""A flight that drew more power than its airframe's other flights predict, for its plan
(spec: Power and efficiency analysis, "Recommendations", the energy anomaly; rules in
`knowledge/rules/performance.json` "anomaly").

- **The prediction:** the airframe's power model fitted without this flight, at each of
  its points' weight, airspeed, climb and air.
- **By phase:** its hover points and its level points apart, each over its own time, so
  the excess says where it went. Hover alone points at the propulsion (a damaged or wrong
  propeller, a motor or ESC working harder, a pack sagging more) or at a weight entered
  wrong; level alone at drag (something added or hanging, a payload's shape).
- **How much is too much:** the larger of a fixed share and a multiple of how far the
  airframe's other flights sit from that same model (a robust spread), so an airframe
  whose flights agree closely flags less, and the flight judged never widens it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.performance.airframe import AirframeModel, rows
from flight_insights.performance.fit import G, Model, energy_error, fit
from flight_insights.performance.points import FlightPoints, Point

Array = np.ndarray[Any, Any]

HOVER = "performance.energy_hover"
LEVEL = "performance.energy_level"
CODES = {"hover": HOVER, "level": LEVEL}
EVIDENCE = "energy:flight"


def spread(errors: Sequence[float]) -> float:
    """A robust standard deviation: 1.4826 times the median absolute deviation."""
    x = np.asarray(errors, np.float64)
    if len(x) == 0:
        return 0.0
    return float(1.4826 * np.median(np.abs(x - np.median(x))))


@dataclass(frozen=True)
class Against:
    """A flight against its airframe's model fitted without it."""

    model: Model  # fitted without the flight
    flight: FlightPoints
    mass_kg: float
    threshold: float  # the excess, as a share, beyond which the flight is flagged
    others: int  # the other flights the threshold was judged from

    def predicted(self, points: Sequence[Point], rules: Mapping[str, Any]) -> Array:
        """The model's power, W, at each point's weight, airspeed, climb and air."""
        return self.model.power(
            np.full(len(points), self.mass_kg * G), np.array([p.airspeed for p in points]),
            np.array([p.climb for p in points]), np.array([p.density for p in points]), rules,
        )  # fmt: skip


def against(flight_id: str, am: AirframeModel, rules: Mapping[str, Any]) -> Against | None:
    """The model without the flight and how far its other flights sit from it; None when
    the flight has no weight or points, or there are too few other flights to judge."""
    spec: Mapping[str, Any] = rules["anomaly"]
    mine = next((f for f in am.flights if f.flight_id == flight_id), None)
    if am.fitted is None or am.disc_area is None or mine is None or mine.mass_kg is None:
        return None
    without = fit([r for r in rows(am.flights) if r.flight != flight_id], am.disc_area,
                  am.coaxial, rules)  # fmt: skip
    if without is None:
        return None
    # how far each other flight is from that model, as power drawn over power predicted:
    # the scatter between flights, with this one kept out of it
    others = [
        1.0 / (1.0 + energy_error(without, got, rules)) - 1.0
        for f in am.flights
        if f.flight_id != flight_id and (got := rows((f,)))
    ]
    if len(others) < int(spec["min_flights"]) - 1:
        return None
    threshold = max(float(spec["min_excess"]), float(spec["sigmas"]) * spread(others))
    return Against(without, mine, mine.mass_kg, threshold, len(others))


def excess(a: Against, points: Sequence[Point], rules: Mapping[str, Any]) -> float:
    """The power the points drew over what the model predicts for them, less one."""
    seconds = np.array([p.seconds for p in points])
    drawn = float(np.sum(np.array([p.power for p in points]) * seconds))
    return drawn / float(np.sum(a.predicted(points, rules) * seconds)) - 1.0


def anomaly(flight_id: str, am: AirframeModel, rules: Mapping[str, Any],
            analysis: Mapping[str, Any]) -> list[dict[str, Any]]:  # fmt: skip
    """The flight's hover and level power against the model without it."""
    spec: Mapping[str, Any] = rules["anomaly"]
    a = against(flight_id, am, rules)
    if a is None:
        return []
    present = {e["id"] for e in analysis.get("evidence", ())}
    out: list[dict[str, Any]] = []
    for phase, code in CODES.items():
        points = [p for p in a.flight.points if p.phase == phase]
        seconds = np.array([p.seconds for p in points])
        if not points or float(seconds.sum()) < float(spec["min_seconds"]):
            continue
        over = excess(a, points, rules)
        if over <= a.threshold:
            continue
        drawn = float(np.sum(np.array([p.power for p in points]) * seconds))
        out.append({
            "id": f"performance:energy_{phase}:{flight_id}", "analyzer": "performance",
            "code": code, "severity": "caution", "confidence": "low",
            "params": {
                "excess_pct": round(100 * over), "threshold_pct": round(100 * a.threshold),
                "seconds": round(float(seconds.sum())), "mass_kg": round(a.mass_kg, 1),
                "flights": a.others, "drawn_w": round(drawn / float(seconds.sum())),
            },
            "evidence": [e for e in (EVIDENCE,) if e in present], "changes": [],
            "stage": "operations", "origin": "measured",
        })  # fmt: skip
    return out
