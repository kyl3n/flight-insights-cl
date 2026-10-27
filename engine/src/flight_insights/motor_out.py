"""Losing a motor (spec: Propulsion, "Motor-out margin"): what the remaining motors must
each make to hold the aircraft level at its weight, from the frame's own motor factors
(knowledge/frames/, read from ArduPilot's mixer).

With motor k stopped, the others' thrusts T_j (each as a share of one motor's full
thrust) must carry the weight W and leave no roll or pitch moment, and, where the frame
must hold its heading, no yaw torque. Two answers:

- **What ArduPilot's mixer does** (`mixer`). The mixer asks each motor for its throttle,
  roll, pitch and yaw factors times four demands, so its thrusts can only lie in the
  space those factors span. Once the attitude and height controllers have settled on a
  stopped motor, the four demands are the ones that balance it, and with five or more
  motors left that steady state is unique. In ArduPilot's simulator, an X8 at 40.5 kg
  losing each motor in turn ran its busiest motor at 1.52 times its hover thrust, against
  this model's 1.5.
- **The best any split could do** (`solve`). Of all the ways to balance, the one that
  asks the least of the busiest motor, by a linear program: minimise t with T_j <= t,
  T_j >= 0 and the balance equations. No mixer can ask less of it; ArduPilot's asks as
  little only where the frame leaves no other choice (a hexa, a Y6, a co-rotating X8):
  for an X8 it is 1.33 against the mixer's 1.5 times hover.

A motor's arm on each axis is its mixer factor over its throttle factor: the mixer scales
a co-rotating X8's top layer down by 0.9 to keep the layers apart in frequency, which is
control, not geometry; both layers sit on the same arm. A propeller's yaw torque is in
proportion to its thrust (both go with the square of its speed), so the yaw factor's
sign and size stand for it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.simplex import linprog

Array = np.ndarray[Any, Any]


@dataclass(frozen=True)
class MotorOut:
    failed: int  # the motor's number, from 1
    peak: float | None  # the busiest remaining motor's thrust, of its full; None: no balance
    thrusts: tuple[float, ...]  # every motor's, the failed one at 0


def arms(motors: Sequence[Mapping[str, Any]]) -> Array:
    """Each motor's roll, pitch and yaw arm: its factors over its throttle factor."""
    return np.array([[m["roll"], m["pitch"], m["yaw"]] for m in motors], float) / np.array(
        [[max(float(m["throttle"]), 1e-9)] for m in motors]
    )


def solve(motors: Sequence[Mapping[str, Any]], failed: int, weight: float,
          hold_yaw: bool = True) -> MotorOut:  # fmt: skip
    """Motor `failed` (from 1) stopped: the least thrust the busiest other motor must make
    to carry `weight` (in full single-motor thrusts) with no roll or pitch moment, and no
    yaw torque when `hold_yaw`."""
    arm = arms(motors)
    alive = [i for i in range(len(motors)) if i != failed - 1]
    k = len(alive)
    # variables: the k thrusts, t, and k slacks for T_j - t + s_j = 0
    n = 2 * k + 1
    rows: list[Array] = []
    rhs: list[float] = []

    def row(coeffs: Array, value: float) -> None:
        r = np.zeros(n)
        r[:k] = coeffs
        rows.append(r)
        rhs.append(value)

    row(np.ones(k), weight)
    axes = (0, 1, 2) if hold_yaw else (0, 1)
    for axis in axes:
        row(arm[alive, axis], 0.0)
    for j in range(k):
        r = np.zeros(n)
        r[j], r[k], r[k + 1 + j] = 1.0, -1.0, 1.0
        rows.append(r)
        rhs.append(0.0)
    cost = np.zeros(n)
    cost[k] = 1.0
    x = linprog(cost, np.array(rows), np.array(rhs))
    if x is None:
        return MotorOut(failed, None, ())
    thrusts = np.zeros(len(motors))
    thrusts[alive] = x[:k]
    rounded = tuple(round(max(float(v), 0.0), 6) + 0.0 for v in thrusts)  # no -0.0
    return MotorOut(failed, float(x[k]), rounded)


def mixer(motors: Sequence[Mapping[str, Any]], failed: int, weight: float) -> MotorOut:
    """Motor `failed` (from 1) stopped, as ArduPilot's mixer flies on: the thrusts its four
    demands settle on to carry `weight` (in full single-motor thrusts) with no roll, pitch
    or yaw moment. No balance when the motors left cannot give one, or only with a motor
    below zero thrust."""
    arm = arms(motors)
    alive = [i for i in range(len(motors)) if i != failed - 1]
    factors = np.array([[m["throttle"], m["roll"], m["pitch"], m["yaw"]] for m in motors],
                       float)[alive]  # fmt: skip
    balance = np.vstack([np.ones(len(alive)), arm[alive].T])  # weight, then the moments
    system = balance @ factors
    if abs(np.linalg.det(system)) < 1e-9:
        return MotorOut(failed, None, ())
    demands = np.linalg.solve(system, np.array([weight, 0.0, 0.0, 0.0]))
    alive_thrusts = factors @ demands
    if float(alive_thrusts.min()) < -1e-9:
        return MotorOut(failed, None, ())
    thrusts = np.zeros(len(motors))
    thrusts[alive] = alive_thrusts
    rounded = tuple(round(max(float(v), 0.0), 6) + 0.0 for v in thrusts)  # no -0.0
    return MotorOut(failed, float(alive_thrusts.max()), rounded)


def worst(motors: Sequence[Mapping[str, Any]], weight: float, hold_yaw: bool = True,
          best: bool = False) -> list[MotorOut]:  # fmt: skip
    """Every motor lost in turn, the hardest first: one with no balance at all, then the
    highest peak. As ArduPilot's mixer flies on, or with `best`, the best split (holding
    the heading when `hold_yaw`)."""
    out = [solve(motors, i + 1, weight, hold_yaw) if best else mixer(motors, i + 1, weight)
           for i in range(len(motors))]  # fmt: skip
    return sorted(out, key=lambda m: (m.peak is not None, -(m.peak or 0.0), m.failed))


def output_for(thrust: float, expo: float) -> float:
    """The motor output x (0 to 1 over its spin range) whose thrust (1 - e) x + e x^2 is
    `thrust` of full: the inverse of ArduPilot's thrust curve, MOT_THST_EXPO e."""
    if abs(expo) < 1e-9:
        return thrust
    return float((-(1.0 - expo) + np.sqrt((1.0 - expo) ** 2 + 4.0 * expo * thrust)) / (2.0 * expo))


def thrust_at(output: float, expo: float) -> float:
    return (1.0 - expo) * output + expo * output * output
