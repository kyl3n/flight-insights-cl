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
- **Over time** (`over_time`): each flight's power against the airframe's model at its own
  points, hover and level apart, turns the model's hover g/W and figure of merit and its
  energy per km at one weight in standard air into the flight's own, so flights flown at
  other weights, in other air and at other speeds line up on one scale. The same rule
  marks the flights that drew too much; the model there includes the flight (one fit for
  all), which can only hide part of an excess, never make one.
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


def ratio(model: Model, mass_kg: float, points: Sequence[Point],
          rules: Mapping[str, Any]) -> float:  # fmt: skip
    """The power the points drew over what the model predicts for them at this weight."""
    seconds = np.array([p.seconds for p in points])
    drawn = float(np.sum(np.array([p.power for p in points]) * seconds))
    predicted = model.power(
        np.full(len(points), mass_kg * G), np.array([p.airspeed for p in points]),
        np.array([p.climb for p in points]), np.array([p.density for p in points]), rules,
    )  # fmt: skip
    return drawn / float(np.sum(predicted * seconds))


@dataclass(frozen=True)
class OverTime:
    """One flight on the airframe's scale: its hover and level power over the model's, and
    the efficiency figures those make at the reference weight in standard air."""

    flight_id: str
    hover: float | None  # power drawn over predicted, hovering
    level: float | None  # the same in level flight
    grams_per_watt: float | None
    figure_of_merit: float | None
    wh_per_km: float | None
    flagged: tuple[str, ...]  # the phases it drew too much in


Thresholds = dict[str, float | None]


def over_time(model: Model, flights: Sequence[FlightPoints], rules: Mapping[str, Any],
              hover_gw: float, hover_fm: float,
              wh_per_km: float | None) -> tuple[list[OverTime], Thresholds]:  # fmt: skip
    """Each flight with a weight and enough of a phase, in the order given, and each phase's
    threshold where enough flights judge it (the anomaly rule's)."""
    spec: Mapping[str, Any] = rules["anomaly"]
    least = float(spec["min_seconds"])
    ratios: dict[str, dict[str, float]] = {"hover": {}, "level": {}}
    for f in flights:
        if f.mass_kg is None:
            continue
        for phase in ratios:
            pts = [p for p in f.points if p.phase == phase]
            if pts and sum(p.seconds for p in pts) >= least:
                ratios[phase][f.flight_id] = ratio(model, f.mass_kg, pts, rules)
    flagged: dict[str, set[str]] = {"hover": set(), "level": set()}
    thresholds: dict[str, float | None] = {}
    for phase, got in ratios.items():
        thresholds[phase] = None
        if len(got) < int(spec["min_flights"]):
            continue
        for fid, r in got.items():
            others = [v - 1.0 for k, v in got.items() if k != fid]
            limit = max(float(spec["min_excess"]), float(spec["sigmas"]) * spread(others))
            if r - 1.0 > limit:
                flagged[phase].add(fid)
        # the line the screen draws: judged from every flight's spread
        thresholds[phase] = max(
            float(spec["min_excess"]),
            float(spec["sigmas"]) * spread([v - 1.0 for v in got.values()]),
        )
    out: list[OverTime] = []
    for f in flights:
        h, lv = ratios["hover"].get(f.flight_id), ratios["level"].get(f.flight_id)
        if h is None and lv is None:
            continue
        out.append(OverTime(
            f.flight_id, h, lv,
            hover_gw / h if h else None, hover_fm / h if h else None,
            wh_per_km * lv if lv and wh_per_km is not None else None,
            tuple(p for p in ("hover", "level") if f.flight_id in flagged[p]),
        ))  # fmt: skip
    return out, thresholds


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
