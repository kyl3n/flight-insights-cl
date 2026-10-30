"""The app's own track record on propeller changes (spec: Power and efficiency analysis,
"Propeller and motor choice", "Closing the loop").

When an airframe's flights move from one propeller to another (its build's setup answers
change the propeller, the motor staying the same), the new propeller's hover is
predicted from the flights on the one before it, exactly as a candidate is
(`performance/propfamily.py`: the anchor nearest each new flight's thrust, the families
as the platform's tables calibrate them now), and graded against what the new flights
measured:

- **speed:** the predicted hover speed against the ESCs' measured median, every flight
  with an anchor;
- **power:** the predicted electrical power per motor (through the motor model) against
  each ESC's own volts times amps, where both flights measured the ESCs' own power, with
  whether it fell inside its 95% interval.

A change of motor as well is left ungraded: the motor model is the one flown now. Only
speeds, powers and weights are read; nothing about where the aircraft flew.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.performance.motor import RPM_TO_RAD, Motor
from flight_insights.performance.propeller import Anchor, anchor_view
from flight_insights.performance.propfamily import Family, Operating, Prop, predict


@dataclass(frozen=True)
class Flown:
    """One flight's hover on the propeller its build named."""

    flight_id: str
    order: float  # when it flew, for telling before from after
    anchor: Anchor
    prop: Prop
    motor_key: str  # the motor its build named: a change of motor is not graded


def _operating(a: Anchor, prop: Prop, motor: Motor | None) -> Operating:
    v = anchor_view(a, prop.diameter_in, motor)
    return Operating(a.rpm, float(v["ct"]), v.get("cp"), a.density)


def grade(rules: Mapping[str, Any], families: Mapping[str, Family], motor: Motor | None,
          flown: Sequence[Flown]) -> list[dict[str, Any]]:  # fmt: skip
    """Each propeller change in the flights, graded: the propeller before and after, how
    many flights after, and the speed and power errors with the interval's coverage."""
    ordered = sorted(flown, key=lambda f: f.order)
    out: list[dict[str, Any]] = []
    for k in range(1, len(ordered)):
        before, after = ordered[k - 1], ordered[k]
        if before.prop.key == after.prop.key:
            continue
        old = [f for f in ordered[:k] if f.prop.key == before.prop.key]
        new = [f for f in ordered[k:] if f.prop.key == after.prop.key]
        nxt = next((i for i, f in enumerate(ordered[k:]) if f.prop.key != after.prop.key), None)
        if nxt is not None:
            new = [f for f in ordered[k : k + nxt] if f.prop.key == after.prop.key]
        row: dict[str, Any] = {"before": before.prop.key, "after": after.prop.key,
                               "flights": len(new), "graded": False}  # fmt: skip
        if before.motor_key != after.motor_key:
            row["why"] = "the motor changed too"
            out.append(row)
            continue
        speed: list[float] = []
        power: list[float] = []
        inside: list[bool] = []
        for f in new:
            a = min(old, key=lambda o: abs(math.log(o.anchor.thrust_n / f.anchor.thrust_n)))
            at = _operating(a.anchor, a.prop, motor)
            got = predict(rules, families, after.prop, f.anchor.thrust_n, f.anchor.density,
                          before.prop, at)  # fmt: skip
            speed.append(math.log(got.rpm / f.anchor.rpm))
            both = a.anchor.power_from == "escs" and f.anchor.power_from == "escs"
            if motor is not None and both and got.shaft_w and got.power_spread:
                omega = got.rpm * RPM_TO_RAD
                e = math.log(float(motor.power(got.shaft_w / omega, omega)) / f.anchor.power_w)
                power.append(e)
                inside.append(abs(e) <= 1.96 * got.power_spread)
        row["graded"] = True
        row["speed_error"] = float(np.median(np.abs(np.expm1(speed))))
        if power:
            row["power_error"] = float(np.median(np.abs(np.expm1(power))))
            row["power_largest"] = float(np.max(np.abs(np.expm1(power))))
            row["inside_95"] = float(np.mean(inside))
        out.append(row)
    return out


def record(graded: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The track record over every graded change: how many, and their errors' medians."""
    done = [g for g in graded if g["graded"]]
    powers = [g["power_error"] for g in done if "power_error" in g]
    return {
        "changes": len(done), "flights": sum(int(g["flights"]) for g in done),
        "speed_error": float(np.median([g["speed_error"] for g in done])) if done else None,
        "power_error": float(np.median(powers)) if powers else None,
    }  # fmt: skip
