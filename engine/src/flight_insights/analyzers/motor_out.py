"""The motor-out margin (spec: Propulsion, "Motor-out margin"; rules in
`knowledge/rules/motor_out.json`): losing each motor in turn at this flight's weight,
what would the busiest of the others have to do?

- **The weight** comes from steady hover, read as the payload estimate reads it: each
  motor's output through ArduPilot's thrust curve at the measured battery voltage, times
  the tilt, at the air the aircraft flew in (not standard air: this flight's own
  conditions are the ones asked about). It is counted in full single-motor thrusts.
- **The geometry** is the frame's own mixer factors (`FRAME_CLASS`, `FRAME_TYPE`; see
  knowledge/frames/).
- **For each motor in turn,** the thrusts ArduPilot's mixer settles on once the motor
  has stopped (`motor_out.py`): its four demands can only reach the thrusts its factors
  span, so the balance it holds is unique. The busiest remaining motor's thrust goes back
  through the curve to an output at the hover voltage, and the worst motor sets the
  margin against the class's target. The best any split could do (a linear program,
  holding the heading where the frame class must) is given beside it: on an X8 it asks
  11% less of the busiest motor than ArduPilot's mixer does.
- **The heaviest take-off** that still meets the target: the needed thrust grows in
  proportion to the weight.

A quad has no margin (it cannot fly on three motors), so it is skipped; helicopters and
frames the mixer does not set up have no table. Real motor losses, compared with the
prediction, come later.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import (
    Analyzer,
    Evidence,
    Finding,
    MetricValue,
    Result,
    Segment,
)
from flight_insights.analyzers.payload import hover_thrust
from flight_insights.analyzers.registry import Registry
from flight_insights.knowledge import KnowledgeError
from flight_insights.motor_out import output_for, thrust_at, worst


def motor_frame(flight: FlightData, reg: Registry) -> tuple[Mapping[str, Any], str] | str:
    """The flight's frame from the firmware's table, with its name ("OCTA/X"), or why
    there is none."""
    p = flight.params
    if "FRAME_CLASS" not in p:
        return "the parameters hold no FRAME_CLASS"
    cls, kind = str(int(p["FRAME_CLASS"])), str(int(p.get("FRAME_TYPE", 1)))
    try:
        frames = reg.pack.frames(flight.firmware or "0")
    except KnowledgeError as e:
        return f"no motor frame table: {e}"
    entry = frames.get(cls)
    if entry is None:
        return f"FRAME_CLASS {cls} is not a frame the matrix mixer sets up"
    types: Mapping[str, Any] = entry["types"]
    frame = types.get(kind) or types.get("default")
    if frame is None or "copter" not in frame.get("vehicles", ["copter"]):
        return f"FRAME_TYPE {kind} is not one of the {entry['name']} frames Copter sets up"
    return frame, f"{entry['name']}/{frame['name']}"


def _motor_out(flight: FlightData, reg: Registry) -> Result:
    if not flight.logged:
        return Result(skipped="a parameter file has no flight to weigh")
    if flight.vehicle != "copter":
        return Result(skipped="the motor-out margin is for Copter")
    rules: Mapping[str, Any] = reg.rules["motor_out"]
    found = motor_frame(flight, reg)
    if isinstance(found, str):
        return Result(skipped=found)
    frame, name = found
    target: Mapping[str, Any] = rules["classes"].get(str(int(flight.params["FRAME_CLASS"])), {})
    if not target.get("max_output"):
        return Result(skipped=f"a {name} cannot fly on one motor fewer: no margin is possible")
    hover = hover_thrust(flight, reg)
    if isinstance(hover, str):
        return Result(skipped=hover)
    motors: list[Mapping[str, Any]] = frame["motors"]
    if len(hover.names) != len(motors):
        return Result(skipped=f"{len(hover.names)} motor outputs for a {len(motors)}-motor frame")

    carried = float(np.median(hover.thrust * hover.tilt))  # of one motor's full thrust
    weight = carried * len(motors)
    hold_yaw = bool(target["hold_yaw"])
    max_output = float(target["max_output"])
    scale = hover.volt_ref / hover.volts  # output for a given x at the hover voltage

    def output(thrust: float) -> float:
        return output_for(thrust, hover.expo) * scale

    losses = worst(motors, weight, hold_yaw)
    best = worst(motors, weight, hold_yaw, best=True)[0]
    seg = Segment("hover", hover.windows[0][0], hover.windows[-1][1])
    needed = [None if m.peak is None else round(100.0 * output(m.peak), 1)
              for m in sorted(losses, key=lambda m: m.failed)]  # fmt: skip
    hardest = losses[0]
    values: dict[str, Any] = {
        "frame": name, "motors": len(motors), "hold_yaw": hold_yaw,
        "hover_thrust_pct": round(100.0 * carried, 1), "volts": round(hover.volts, 2),
        "expo": hover.expo, "target_pct": round(100.0 * max_output, 1),
        "output_needed_pct": needed, "worst_motor": hardest.failed,
    }  # fmt: skip
    messages = ("RCOU", "BAT", "ATT", "CTUN")
    fields = (*hover.names, "Volt", "Roll", "Pitch", "Alt")
    out = Result()
    if hardest.peak is None:
        ev = Evidence(id="motor_out:hover", kind="metric",
                      summary=f"{name}: losing motor {hardest.failed}, no thrust split "
                      "balances the aircraft", messages=messages, fields=fields, segment=seg,
                      values=values)  # fmt: skip
        out.evidence.append(ev)
        out.findings.append(Finding("motor_out.no_balance", "caution", "high", (ev,),
                                    {"motor": hardest.failed}))  # fmt: skip
        return out

    busiest = int(np.argmax(hardest.thrusts)) + 1
    need = output(hardest.peak)
    ratio = thrust_at(max_output / scale, hover.expo) / hardest.peak
    margin = round(100.0 * (max_output - need), 1)
    values |= {"busiest_motor": busiest, "worst_output_pct": round(100.0 * need, 1),
               "weight_ratio": round(ratio, 3),
               "best_split_output_pct": None if best.peak is None
               else round(100.0 * output(best.peak), 1)}  # fmt: skip
    ev = Evidence(
        id="motor_out:hover", kind="metric",
        summary=f"{name} at {100 * carried:.1f}% hover thrust: losing motor {hardest.failed}, "
        f"motor {busiest} would need {100 * need:.1f}% output against a "
        f"{100 * max_output:g}% target ({'heading held' if hold_yaw else 'heading may go'})",
        messages=messages, fields=fields, segment=seg, values=values,
    )  # fmt: skip
    out.evidence.append(ev)
    for metric, value in (("motor_out.margin", margin),
                          ("motor_out.weight_ratio", round(ratio, 3))):  # fmt: skip
        out.metrics.append(MetricValue(metric, "hover", value,
                                       reg.metrics[metric].zone(value), ev.id))  # fmt: skip
    if margin < 0:
        out.findings.append(Finding(
            "motor_out.short", "caution", "medium", (ev,),
            {"motor": hardest.failed, "busiest": busiest, "needed": round(100.0 * need, 1),
             "target": round(100.0 * max_output, 1), "ratio": round(ratio, 2)},
        ))  # fmt: skip
    return out


MOTOR_OUT = Analyzer(
    name="motor_out",
    version=1,
    run=_motor_out,
    metrics=("motor_out.margin", "motor_out.weight_ratio"),
    findings=("motor_out.short", "motor_out.no_balance"),
)
