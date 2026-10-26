"""Payload bands (spec: Performance trends and ideal ranges, "Like with like").

Each flight's hover thrust at standard air (`payload.hover_thrust`, which follows its
weight) is read against the airframe's lightest flights, in steps of 10%, so a heavy
hover is compared only with hovers about as heavy, whatever the flight was labelled.

- **The lightest flights** are those within 5% of the lightest one; their median is
  the reference, so one light outlier does not move every band.
- **Band k** holds the flights within half a step of the reference times 1.1^k: band 0
  is the lightest, band 2 about 20% heavier.
- **Kilograms.** A flight weighed at take-off gives the airframe's kilograms per percent
  of thrust (the median over its weighed flights), which turns every band and flight
  into kilograms. With two or more weighed, each one's weight is also estimated from
  the others', which checks the estimate against the scale.

A flight with no hover thrust (too little steady hover, or a log without the motor
outputs) has no band and keeps its payload label.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any


@dataclass(frozen=True)
class Flown:
    id: str
    thrust: float | None  # payload.hover_thrust, % of full thrust at standard air
    takeoff_kg: float | None  # as weighed at import


@dataclass(frozen=True)
class Bands:
    step: float
    lightest: float | None = None  # the reference hover thrust, %
    kg_per_percent: float | None = None
    band: Mapping[str, int] = field(default_factory=dict[str, int])  # flight id -> band
    check: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def bounds(self, k: int) -> tuple[float, float]:
        """A band's hover thrust, low and high, in percent."""
        assert self.lightest is not None
        r = 1.0 + self.step
        return self.lightest * r ** (k - 0.5), self.lightest * r ** (k + 0.5)

    def kg(self, thrust: float | None) -> float | None:
        if thrust is None or self.kg_per_percent is None:
            return None
        return thrust * self.kg_per_percent

    def describe(self, k: int) -> dict[str, Any]:
        low, high = self.bounds(k)
        kg_low, kg_high = self.kg(low), self.kg(high)
        return {
            "band": k,
            "low": round(low, 2),
            "high": round(high, 2),
            "above_lightest": round(100.0 * ((1.0 + self.step) ** k - 1.0)),
            "kg": None
            if kg_low is None or kg_high is None
            else [round(kg_low, 1), round(kg_high, 1)],
        }


def bands(flights: Sequence[Flown], rules: Mapping[str, Any]) -> Bands:
    step = float(rules["band_step"])
    measured = [f for f in flights if f.thrust is not None and f.thrust > 0]
    if not measured:
        return Bands(step)
    values = sorted(f.thrust for f in measured if f.thrust is not None)
    lightest = float(
        median(v for v in values if v <= values[0] * (1.0 + float(rules["lightest_within"])))
    )
    band = {
        f.id: math.floor(math.log(f.thrust / lightest) / math.log(1.0 + step) + 0.5)
        for f in measured
        if f.thrust is not None
    }
    weighed = [(f, f.takeoff_kg / f.thrust) for f in measured if f.takeoff_kg and f.thrust]
    check: list[dict[str, Any]] = []
    if len(weighed) >= 2:
        for i, (f, _) in enumerate(weighed):
            assert f.thrust is not None and f.takeoff_kg is not None
            others = median(r for j, (_, r) in enumerate(weighed) if j != i)
            estimated = f.thrust * others
            check.append(
                {"flight": f.id, "weighed_kg": f.takeoff_kg, "estimated_kg": round(estimated, 2),
                 "error_pct": round(100.0 * (estimated - f.takeoff_kg) / f.takeoff_kg, 1)}
            )  # fmt: skip
    per = float(median(r for _, r in weighed)) if weighed else None
    return Bands(step, lightest, per, band, check)
