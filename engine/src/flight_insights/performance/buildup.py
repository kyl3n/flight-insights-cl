"""The speed build-up, when the setup's speed goal is "fastest safe" (spec: Power and
efficiency analysis, "The speed build-up"): the next step the envelope offers at the
flight's weight, as a raise of `WP_SPD` the plan may propose, or what holds it back.

- **No verified speed at this weight** (`speed.build_up_start`): fly the speed card at
  this weight; its fastest legs, held with every watched metric in target, verify the
  first speed.
- **The step offered** (every predicted margin holds; `speed.build_up_step`): `WP_SPD`
  set to the step speed, rounded down to 0.1 m/s (lowered to it when it is set faster
  still, so the next flight flies the step and no more), with its procedure: the card to fly
  it, the abort criteria, the logging the watched metrics need, and the checks on the
  next flight's fast segment (`speed.build_up_step` in the procedures). This one raise,
  one step past the verified speed, is the only one past it the plan's never-raise rule
  lets through.
- **A prediction the step needs is missing** (`speed.build_up_missing_<what>`): what to
  enter or fly for the lean, the thrust or the current.
- **A margin fails** (`speed.limited_by_<check>`): its predicted value and limit, and the
  ways to move it.
- While the goal is fastest, the cruise speed no longer follows the Priority
  (`advice.speeds`); the home speed and the battery failsafe's charge still do.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from flight_insights.analyzers.registry import Registry
from flight_insights.performance.advice import EVIDENCE, Advice, Context
from flight_insights.performance.envelope import Inputs as EnvelopeInputs
from flight_insights.performance.envelope import step as next_step
from flight_insights.performance.top_speed import lowest_volts

START = "speed.build_up_start"
STEP = "speed.build_up_step"
MISSING = "speed.build_up_missing_"  # + lean, thrust or current
LIMITED = "speed.limited_by_"  # + the check that binds


def fastest(ctx: Context) -> bool:
    return ctx.inputs.answers.get("speed_goal") == "fastest"


def _finding(ctx: Context, code: str, params: dict[str, Any],
             changes: list[dict[str, Any]]) -> dict[str, Any]:  # fmt: skip
    return {
        "id": f"speed:{code.split('.')[1]}:{ctx.flight_id}", "analyzer": "speed",
        "code": code, "severity": "info", "confidence": "low", "params": params,
        "evidence": [EVIDENCE], "changes": changes, "stage": "navigation",
        "origin": "measured",
    }  # fmt: skip


def buildup(ctx: Context, params: Mapping[str, float], reg: Registry) -> Advice:
    """The build-up's finding for this flight's plan, and the one raise it allows."""
    if not fastest(ctx):
        return Advice()
    i = ctx.inputs
    common: dict[str, Any] = {"mass_kg": round(ctx.mass_kg, 1)}
    if i.basis is None or i.pack is None:
        what = "lean" if i.basis is None else "current"
        return Advice([_finding(ctx, MISSING + what, common, [])])
    env = EnvelopeInputs(ctx.model, i.basis, params, lowest_volts(params, i.pack), i.current_a,
                         i.thermals, i.esc_limit_c, i.motors)  # fmt: skip
    s = next_step(i.fast, env, ctx.mass_kg, ctx.density, reg)
    if s.verified is None or s.next_ms is None:
        return Advice([_finding(ctx, START, common, [])])
    common |= {"verified_ms": round(s.verified.speed_ms, 1), "next_ms": round(s.next_ms, 1)}
    by = {c.name: c for c in s.checks}
    if s.offered:
        proposed = math.floor(s.next_ms * 10 + 1e-9) / 10
        current = params.get("WP_SPD")
        if current is not None and abs(current - proposed) < 0.05:
            return Advice()  # set already: the next flight's fast segment verifies it
        margins = {f"{n}_value": round(c.value, 1) for n, c in by.items()}
        margins |= {f"{n}_limit": round(c.limit, 1) for n, c in by.items()}
        change = {"param": "WP_SPD", "proposed": proposed, "stage": "navigation"}
        found = _finding(ctx, STEP, common | margins | {"proposed_ms": proposed}, [change])
        return Advice([found], {"WP_SPD": proposed})
    binding = s.binding
    if binding is None:
        return Advice()
    if binding not in by:  # a prediction the step needs is missing
        return Advice([_finding(ctx, MISSING + binding, common, [])])
    c = by[binding]
    return Advice([_finding(ctx, LIMITED + binding, common | {
        "value": round(c.value, 1), "limit": round(c.limit, 1)}, [])])  # fmt: skip
