"""The propeller the setup's Priority favours (spec: Power and efficiency analysis,
"Propeller and motor choice", "Advice"; rules in `knowledge/rules/props.json` "search"
and "advice").

- **What is searched:** propellers of the flown one's family and blade count, at the
  flown diameter and the rules' steps either side, at each pitch over the rules'
  pitch-to-diameter range, each predicted against the propeller flown
  (`performance/candidate.py`). One that fails any check (thrust-to-weight, pack current,
  tip Mach, clearance, ESC temperature, the notch, the motors' response) is not a choice.
- **What it is searched for,** the setup's Priority: endurance and efficiency, the least
  electrical hover power at the weight (the shaft power's without a motor model); agility,
  the most thrust-to-weight at full throttle (needs a motor model). Stability asks for
  neither: nothing is searched.
- **The finding** `performance.prop_choice` (a hardware recommendation, never applied) is
  raised only when the advice is switched on (`advice.enabled`, once the local validation
  has met its targets and set `advice.validated_error`), the best is calibrated and
  anchored on the aircraft, and its predicted gain is more than `advice.gain_over_error`
  times the validated error. Until then the search is shown, labelled theory, and nothing
  is advised.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.performance.candidate import Basis, assess
from flight_insights.performance.propfamily import Prop

PROP_CHOICE = "performance.prop_choice"
OBJECTIVE = {"endurance": "hover_power", "efficiency": "hover_power", "agility": "thrust"}


@dataclass(frozen=True)
class Option:
    prop: Prop
    value: float  # the objective: hover W, or thrust-to-weight
    gain: float  # the share better than the propeller flown (positive: better)
    spread: float | None  # the gain's standard deviation, of its logarithm
    ok: bool  # every check passed
    failed: tuple[str, ...]
    calibrated: bool
    anchored: bool


def candidates(flown: Prop, rules: Mapping[str, Any]) -> list[Prop]:
    """The propellers the search tries: the flown family and blades, each diameter step
    either side of the flown one, each pitch over the pitch-to-diameter range."""
    spec: Mapping[str, Any] = rules["search"]
    lo, hi = (float(x) for x in spec["pitch_ratio"])
    out: list[Prop] = []
    for step in spec["diameter_steps_in"]:
        d = flown.diameter_in + float(step)
        if d <= 0:
            continue
        for ratio in np.linspace(lo, hi, int(spec["pitch_steps"])):
            pitch = round(float(ratio) * d, 1)
            out.append(Prop(flown.family, d, pitch, flown.blades))
    if all(p.key != flown.key for p in out):
        out.append(flown)
    return out


def _value(got: Mapping[str, Any], objective: str) -> tuple[float | None, float | None]:
    """An assessed propeller's objective and its spread."""
    at = got["point"]
    if objective == "hover_power":
        key = "electric_w" if "electric_w" in at else "shaft_w"
        return at.get(key), at.get("power_spread")
    most = got.get("most")
    return (most["thrust_to_weight"], 2 * at["rpm_spread"]) if most else (None, None)


def search(basis: Basis, rules: Mapping[str, Any], priority: str | None, mass_kg: float,
           density: float, duty: float) -> dict[str, Any]:  # fmt: skip
    """Every propeller tried, best first, and whether the best is advised, or why not (a
    reason code and its sentence)."""
    objective = OBJECTIVE.get(priority or "")
    out: dict[str, Any] = {"priority": priority, "objective": objective, "options": [],
                           "best": None, "advised": False, "why": None,
                           "reason": None}  # fmt: skip

    def because(reason: str, why: str) -> dict[str, Any]:
        out["reason"], out["why"] = reason, why
        return out

    if objective is None:
        return because("no_priority", "the setup's Priority asks for no propeller change")
    flown = assess(basis, rules, basis.flown, basis.motor, mass_kg, density, duty)
    base, _ = _value(flown, objective)
    if base is None:
        return because("needs_motor", "a motor model is needed for the most thrust")
    options: list[Option] = []
    for prop in candidates(basis.flown, rules):
        got = assess(basis, rules, prop, basis.motor, mass_kg, density, duty, None,
                     flown["point"])  # fmt: skip
        value, spread = _value(got, objective)
        if value is None:
            continue
        gain = (base - value) / base if objective == "hover_power" else (value - base) / base
        failed = tuple(c["check"] for c in got["checks"] if not c["ok"])
        at = got["point"]
        options.append(Option(prop, value, gain, spread, not failed, failed,
                              bool(at["calibrated"]), bool(at["anchored"])))  # fmt: skip
    options.sort(key=lambda o: (not o.ok, -o.gain))
    out["options"] = [_view(o) for o in options]
    best = next((o for o in options if o.ok and o.prop.key != basis.flown.key), None)
    if best is None:
        return because("none_pass", "no other propeller passes every check")
    out["best"] = _view(best)
    advice: Mapping[str, Any] = rules["advice"]
    error = advice.get("validated_error")
    if not advice["enabled"] or error is None:
        because(
            "validation", "the advice waits for the local validation (labelled theory until then)"
        )
    elif not (best.calibrated and best.anchored):
        because(
            "not_calibrated", "the best is not calibrated on tables and anchored on this aircraft"
        )
    elif best.gain <= float(advice["gain_over_error"]) * float(error):
        because("within_error", "the gain is within twice the validated error")
    else:
        out["advised"] = True
    return out


def _view(o: Option) -> dict[str, Any]:
    return {"diameter_in": o.prop.diameter_in, "pitch_in": o.prop.pitch_in,
            "blades": o.prop.blades, "family": o.prop.family, "value": o.value,
            "gain": o.gain, "spread": o.spread, "ok": o.ok, "failed": list(o.failed),
            "calibrated": o.calibrated, "anchored": o.anchored}  # fmt: skip


def finding(flight_id: str, found: Mapping[str, Any], flown: Prop, mass_kg: float,
            evidence: Sequence[str]) -> dict[str, Any] | None:  # fmt: skip
    """The `performance.prop_choice` finding for an advised search, with what it rests on;
    None when nothing is advised."""
    best = found.get("best")
    if not found.get("advised") or best is None:
        return None
    return {
        "id": f"performance:prop_choice:{flight_id}", "analyzer": "performance",
        "code": PROP_CHOICE, "severity": "info", "confidence": "low", "stage": "mechanical",
        "origin": "measured", "evidence": list(evidence), "changes": [],
        "params": {"flown": f"{flown.diameter_in:g}x{flown.pitch_in:g}",
                   "best": f"{best['diameter_in']:g}x{best['pitch_in']:g}",
                   "gain_pct": round(100 * float(best["gain"]), 1), "mass_kg": round(mass_kg, 1),
                   "objective": found["objective"]},
    }  # fmt: skip


def with_rules(rules: Mapping[str, Any], **advice: Any) -> dict[str, Any]:
    """The rules with the advice's settings changed (for checks and the validation)."""
    return dict(rules) | {"advice": dict(rules["advice"]) | advice}
