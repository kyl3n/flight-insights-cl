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
- **The lean limit, when it alone holds the step back** (`speed.build_up_lean`):
  `PSC_ANGLE_MAX`, the automatic modes' limit, raised by at most the rules' step within
  the whole degrees the thrust allows at the step, offered with the step when every margin
  then holds. The pilot's `ATC_ANGLE_MAX` stays.
- **A prediction the step needs is missing** (`speed.build_up_missing_<what>`): what to
  enter or fly for the lean, the thrust or the current.
- **A margin fails** (`speed.limited_by_<check>`): its predicted value and limit, and the
  ways to move it. For the limits the models predict (lean, thrust, current, ESC
  temperature), also the airspeed it allows now and how much faster each of the rules'
  solutions would let it fly (`envelope.gains`). For the notch: the motors' frequency at
  the step, past the reach of a notch set that does not follow them.
- **Ready first** (`gate`, rules "readiness"): the step is held back, with what holds it,
  while the flight has a finding open that matters at speed and that staging would not hold
  it behind (GNSS, the motor-out margin, the packs and their failsafe), or one of the
  earlier stages' that fails first at higher power (ESCs, motors, vibration).
- While the goal is fastest, the cruise speed no longer follows the Priority
  (`advice.speeds`); the home speed and the battery failsafe's charge still do.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.wind import configured_lean
from flight_insights.performance.advice import EVIDENCE, Advice, Context
from flight_insights.performance.envelope import Check, Step, gains
from flight_insights.performance.envelope import Inputs as EnvelopeInputs
from flight_insights.performance.envelope import step as next_step
from flight_insights.performance.top_speed import lowest_volts

START = "speed.build_up_start"
STEP = "speed.build_up_step"
MISSING = "speed.build_up_missing_"  # + lean, thrust or current
LIMITED = "speed.limited_by_"  # + the check that binds
NOT_READY = "speed.build_up_not_ready"
LEAN = "speed.build_up_lean"  # the lean limit raised for the step
LEAN_PARAM = "PSC_ANGLE_MAX"
MODELLED = ("lean", "thrust", "current", "esc_temp")  # the models can say what moves them


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
                         i.thermals, i.esc_limit_c, i.motors, i.motor_hz)  # fmt: skip
    s = next_step(i.fast, env, ctx.mass_kg, ctx.density, reg)
    if s.verified is None or s.next_ms is None:
        return Advice([_finding(ctx, START, common, [])])
    common |= {"verified_ms": round(s.verified.speed_ms, 1), "next_ms": round(s.next_ms, 1)}
    by = {c.name: c for c in s.checks}
    if s.offered:
        return _step(ctx, params, s, common)
    binding = s.binding
    if binding is None:
        return Advice()
    if binding == "lean":
        raised = _lean(ctx, params, env, by, common, reg)
        if raised is not None:
            return raised
    if binding not in by:  # a prediction the step needs is missing
        return Advice([_finding(ctx, MISSING + binding, common, [])])
    c = by[binding]
    limited = common | {"value": round(c.value, 1), "limit": round(c.limit, 1)}
    if binding in MODELLED:  # what it allows now, and what each solution would add
        now, more = gains(binding, env, ctx.mass_kg, ctx.density, reg)
        sizes = {str(x["name"]): x for x in reg.rules["performance"]["envelope"]["solutions"][
            binding]}  # fmt: skip
        limited["allows_ms"] = round(now, 1)
        for name, gain in more.items():
            share = next(float(v) for k, v in sizes[name].items() if k.endswith("_share"))
            limited[f"{name}_gain_ms"] = round(gain, 1)
            limited[f"{name}_pct"] = round(100 * abs(share - 1.0))
    return Advice([_finding(ctx, LIMITED + binding, limited, [])])


def _step(ctx: Context, params: Mapping[str, float], s: Step, common: Mapping[str, Any],
          lean: dict[str, Any] | None = None) -> Advice:  # fmt: skip
    """The step offered: WP_SPD to the step speed, with the lean limit's raise when it
    needs one."""
    assert s.next_ms is not None
    proposed = math.floor(s.next_ms * 10 + 1e-9) / 10
    by = {c.name: c for c in s.checks}
    found: list[dict[str, Any]] = []
    limits: dict[str, float] = {}
    current = params.get("WP_SPD")
    if current is None or abs(current - proposed) >= 0.05:  # else set already
        margins = {f"{n}_value": round(c.value, 1) for n, c in by.items()}
        margins |= {f"{n}_limit": round(c.limit, 1) for n, c in by.items()}
        change = {"param": "WP_SPD", "proposed": proposed, "stage": "navigation"}
        found.append(_finding(ctx, STEP, dict(common) | margins | {"proposed_ms": proposed},
                              [change]))  # fmt: skip
        limits["WP_SPD"] = proposed
    if lean is not None:
        change = {"param": LEAN_PARAM, "proposed": lean["to_deg"], "stage": "navigation"}
        found.append(_finding(ctx, LEAN, dict(common) | lean | {"proposed_ms": proposed},
                              [change]))  # fmt: skip
        limits[LEAN_PARAM] = lean["to_deg"]
    return Advice(found, limits)


def _lean(ctx: Context, params: Mapping[str, float], env: EnvelopeInputs,
          by: Mapping[str, Check], common: Mapping[str, Any],
          reg: Registry) -> Advice | None:  # fmt: skip
    """When the lean alone holds the step back and the thrust leaves room: the step with
    PSC_ANGLE_MAX raised by at most the rules' step, within the whole degrees the thrust
    allows, if every margin then holds; None otherwise."""
    thrust = by.get("thrust")
    if thrust is None or not thrust.holds:
        return None
    wind: Mapping[str, Any] = reg.rules["wind"]["envelope"]
    spec: Mapping[str, Any] = reg.rules["performance"]["envelope"]
    now = configured_lean(params, wind)
    to = min(now + float(spec["lean_step_deg"]), float(math.floor(thrust.limit)),
             float(wind["angle_max_deg"]))  # fmt: skip
    to = math.floor(to * 10 + 1e-9) / 10
    if to <= now:
        return None
    raised = {**params, LEAN_PARAM: to}
    s = next_step(ctx.inputs.fast, replace(env, params=raised), ctx.mass_kg, ctx.density, reg)
    if not s.offered or s.next_ms is None:
        return None
    lean = {"param": LEAN_PARAM, "from_deg": round(now, 1), "to_deg": to,
            "lean_deg": round(thrust.value, 1), "thrust_deg": round(thrust.limit, 1),
            "share_pct": round(by["lean"].limit)}  # fmt: skip
    return _step(ctx, raised, s, common, lean)


def blocking(findings: Sequence[Mapping[str, Any]], reg: Registry) -> list[str]:
    """The codes of the open findings the step waits for (rules "readiness")."""
    spec: Mapping[str, Any] = reg.rules["performance"]["readiness"]
    names: tuple[str, ...] = tuple(spec["codes"])
    severities = set(spec["severities"])

    def named(code: str) -> bool:
        return any(code.startswith(n) if n.endswith(".") else code == n for n in names)

    return sorted({str(f["code"]) for f in findings
                   if f.get("severity") in severities and named(str(f["code"]))})  # fmt: skip


def gate(findings: Sequence[Mapping[str, Any]], limits: Mapping[str, float], reg: Registry
         ) -> tuple[list[dict[str, Any]], dict[str, float]]:  # fmt: skip
    """The plan's findings and raise limits with the build-up's step (and its lean raise)
    held back, and what holds it said, while a finding that matters at speed is open."""
    out = [dict(f) for f in findings]
    if not any(f["code"] in (STEP, LEAN) for f in out):
        return out, dict(limits)
    held = blocking(out, reg)
    if not held:
        return out, dict(limits)
    first = next(k for k, f in enumerate(out) if f["code"] in (STEP, LEAN))
    f, p = out[first], out[first]["params"]
    out[first] = f | {
        "id": str(f["id"]).replace(str(f["code"]).split(".")[1], NOT_READY.split(".")[1]),
        "code": NOT_READY, "severity": "caution", "changes": [],
        "params": {key: p[key] for key in ("mass_kg", "verified_ms", "next_ms", "proposed_ms")}
        | {"blocking": ", ".join(held), "count": len(held)},
    }  # fmt: skip
    out = [f for f in out if f["code"] not in (STEP, LEAN)]
    return out, {name: v for name, v in limits.items() if name not in ("WP_SPD", LEAN_PARAM)}
