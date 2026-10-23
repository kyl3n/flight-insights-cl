"""A plan's step as a parameter file for a person to review and load (spec:
Recommendation engine, "Outputs").

Changed values only, in the firmware's own names and units, each with the reason as
a comment. A step that ends in a reboot and parameter refresh says so in the header,
because the next step's parameters only appear after it. Nothing here talks to an
aircraft: the file is loaded by a person in Mission Planner or QGroundControl.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flight_insights.analyzers.registry import Registry
from flight_insights.params.files import Kind, write_mission_planner, write_qgroundcontrol
from flight_insights.recommend.plan import Plan, render, why


class ExportError(ValueError):
    """The plan has nothing to load at that step."""


def export_step(
    plan: Plan, reg: Registry, step: int, kind: Kind, lang: str = "en"
) -> dict[str, Any]:
    """{"filename", "text"} for one step of the plan."""
    recs = plan.offered(step)
    if not recs:
        raise ExportError(f"step {step} of the plan has nothing to load")
    rules: Mapping[str, Any] = reg.rules["recommend"]["texts"]
    verify: Mapping[str, Any] = reg.rules["recommend"]["verify"]
    after = next(s.after for s in plan.steps if s.number == step)

    def text(key: str, **params: Any) -> str:
        return render(rules[key][lang], params)

    values = {r.native or "": float(r.native_value or 0) for r in recs}
    comments = {r.native or "": why(reg, r.lead)[lang] for r in recs}
    header = [
        f"Flight Insights: flight {plan.flight}, {plan.vehicle} {plan.firmware}",
        text("file_header"),
        text("file_step", step=step, steps=len(plan.steps), after=text(f"after_{after}")),
        "",
        text("verify_header"),
        *dict.fromkeys(verify[r.stage][lang] for r in recs if r.stage in verify),
    ]
    stem = f"flight-insights-{plan.flight}-step{step}"
    if kind == "mission planner":
        return {
            "filename": f"{stem}.param",
            "text": write_mission_planner(values, comments, header),
        }
    return {
        "filename": f"{stem}.params",
        "text": write_qgroundcontrol(values, comments=comments, notes=header),
    }
