"""A plan's files for a person to review and load (spec: Recommendation engine,
"Outputs" and "Procedures").

- One parameter file per step: changed values only, in the firmware's own names and
  units, each with the reason as a comment. Its header says what to do once it is
  loaded (refresh, reboot and refresh, reboot, or fly), and the last step's file also
  carries the test card: the checks before flying, what to fly, when to stop and what
  the next log should show.
- The revert file: the settings that were only for the test flight, back as they were.
- The undo file: every value the plan changed, back as it was, and the values an
  action's own work may change (the gains AutoTune saves).
- The test card on its own, as text to print.

Nothing here talks to an aircraft: the files are loaded by a person in Mission Planner
or QGroundControl.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from flight_insights.analyzers.registry import Registry
from flight_insights.params.files import Kind, write_mission_planner, write_qgroundcontrol
from flight_insights.recommend import procedure
from flight_insights.recommend.plan import Plan, Recommendation, render, why


class ExportError(ValueError):
    """The plan has nothing to load at that step."""


def _chosen(
    plan: Plan, only: Collection[str] | None, step: int | None = None
) -> list[Recommendation]:
    return [r for r in plan.offered(step) if only is None or r.param in only]


def _texts(reg: Registry, lang: str) -> Any:
    rules: Mapping[str, Any] = reg.rules["recommend"]["texts"]

    def text(key: str, **params: Any) -> str:
        return render(rules[key][lang], params)

    return text


def _file(
    plan: Plan,
    reg: Registry,
    recs: list[Recommendation],
    values: Mapping[str, float],
    header: list[str],
    stem: str,
    kind: Kind,
    lang: str,
    mark_temporary: bool = False,
    comments: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    text = _texts(reg, lang)
    comments = dict(comments or {}) | {
        r.native or "": why(reg, r.lead)[lang]
        + (f" ({text('temporary_note')})" if mark_temporary and r.native_revert is not None else "")
        for r in recs
    }
    header = [f"Flight Insights: flight {plan.flight}, {plan.vehicle} {plan.firmware}", *header]
    if kind == "mission planner":
        return {
            "filename": f"{stem}.param",
            "text": write_mission_planner(values, comments, header),
        }
    return {
        "filename": f"{stem}.params",
        "text": write_qgroundcontrol(values, comments=comments, notes=header),
    }


def export_step(
    plan: Plan,
    reg: Registry,
    step: int,
    kind: Kind,
    lang: str = "en",
    only: Collection[str] | None = None,
) -> dict[str, Any]:
    """{"filename", "text"} for one step of the plan; with `only`, just those canonical
    parameters (the ones a person accepted)."""
    recs = _chosen(plan, only, step)
    if not recs:
        raise ExportError(
            f"step {step} of the plan has nothing {'accepted ' if only is not None else ''}to load"
        )
    text = _texts(reg, lang)
    after = next(s.after for s in plan.steps if s.number == step)
    header = [
        text("file_header"),
        text("file_step", step=step, steps=len(plan.steps), after=text(f"after_{after}")),
    ]
    last = max(r.step for r in _chosen(plan, only))
    if step == last:  # the file loaded last carries the test card
        card = procedure.session(plan, reg, only)
        for kind_, key in (
            ("ground_check", "before_header"),
            ("fly", "fly_header"),
            ("abort", "abort_header"),
            ("verify", "verify_header"),
        ):
            lines = [s.text[lang] for s in card if s.kind == kind_]
            if lines:
                header += ["", text(key), *lines]
    values = {r.native or "": float(r.native_value or 0) for r in recs}
    stem = f"flight-insights-{plan.flight}-step{step}"
    return _file(plan, reg, recs, values, header, stem, kind, lang, mark_temporary=True)


def export_revert(
    plan: Plan, reg: Registry, kind: Kind, lang: str = "en", only: Collection[str] | None = None
) -> dict[str, Any]:
    """The settings that were for the test flight only, back to what they were."""
    recs = [r for r in _chosen(plan, only) if r.native_revert is not None]
    if not recs:
        raise ExportError("the plan has no settings for the test flight only to take out")
    values = {r.native or "": float(r.native_revert or 0) for r in recs}
    header = [_texts(reg, lang)("revert_header")]
    return _file(
        plan, reg, recs, values, header, f"flight-insights-{plan.flight}-revert", kind, lang
    )


def export_undo(
    plan: Plan, reg: Registry, kind: Kind, lang: str = "en", only: Collection[str] | None = None
) -> dict[str, Any]:
    """Every value the plan changes, back to what the flight had, and what the work of
    an action on offer may change by itself (the gains AutoTune saves). A parameter the
    plan makes appear has no earlier value; it hides again once its switch is back off."""
    recs = [r for r in _chosen(plan, only) if r.native_current is not None]
    actions = [r for r in plan.recommendations if r.param is None and r.status == "next"]
    values = {n: v for a in actions for n, v in a.undo.items()}
    values |= {r.native or "": float(r.native_current or 0) for r in recs}
    if not values:
        raise ExportError("the plan changes nothing that the flight already had")
    header = [_texts(reg, lang)("undo_header")]
    extra = {n: why(reg, a.lead)[lang] for a in actions for n in a.undo}
    return _file(
        plan, reg, recs, values, header, f"flight-insights-{plan.flight}-undo", kind, lang,
        comments=extra,
    )  # fmt: skip


def export_card(
    plan: Plan, reg: Registry, lang: str = "en", only: Collection[str] | None = None
) -> dict[str, Any]:
    """The session's test card as text, to print or keep beside the aircraft."""
    if not procedure.session(plan, reg, only):
        raise ExportError("the plan has nothing on offer")
    return {
        "filename": f"flight-insights-{plan.flight}-card.txt",
        "text": procedure.card_text(plan, reg, lang, only),
    }
