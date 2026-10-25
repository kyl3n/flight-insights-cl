"""Step-by-step procedures (spec: Recommendation engine, "Procedures").

A plan says what to change; a procedure says how, in order, so nobody has to guess:

1. work for a person (what an action's finding says to do), hardware and firmware work
   (new ESC firmware, balancing propellers);
2. each parameter file, with the refresh or reboot the firmware needs between them;
3. a reboot when any value needs one;
4. the settings that are for the test flight only;
5. checks on the bench (props off) before flying;
6. what to fly, and when to stop and land;
7. what the next log should show, with the checks the app runs on it;
8. the revert file that takes the test-flight settings out again, once confirmed;
9. the undo file that puts back everything the plan changed, if the test goes wrong.

The parameter steps come from the plan itself (its steps, gates, reboots and temporary
settings). The rest comes from the finding's own template (knowledge/procedures/*.json)
and, for what to fly, when to stop and what to check, from the stage's defaults
(rules/recommend.json) wherever the template gives none. Nothing here talks to an
aircraft: every value goes through a file a person reviews and loads.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from flight_insights.analyzers.registry import LANGUAGES, Registry
from flight_insights.params.files import format_value
from flight_insights.recommend.plan import Plan, Recommendation, render

StepKind = Literal[
    "task", "hardware", "firmware", "set", "refresh", "reboot_refresh", "reboot",
    "temporary", "ground_check", "fly", "abort", "verify", "revert", "undo",
]  # fmt: skip
TOLERANCE = 1e-6


@dataclass(frozen=True)
class ProcedureStep:
    kind: StepKind
    text: Mapping[str, str]  # by language
    params: tuple[str, ...] = ()  # the parameters it loads or names, this firmware's names
    step: int | None = None  # the plan's file, for "set"
    checks: tuple[Mapping[str, Any], ...] = ()  # what the app tests on the next flight


def procedure(plan: Plan, reg: Registry, rec: Recommendation) -> list[ProcedureStep]:
    """One recommendation's procedure: its own change, and the switches it waits on."""
    chain = [rec]
    by_param = {r.param: r for r in plan.recommendations if r.param is not None}
    while chain[-1].after_gate in by_param and len(chain) <= len(by_param):
        chain.append(by_param[chain[-1].after_gate])
    return _assemble(plan, reg, [r for r in chain if r is rec or r.status == "next"], rec.stage)


def session(plan: Plan, reg: Registry, only: Iterable[str] | None = None) -> list[ProcedureStep]:
    """The whole session for what the plan offers, merged into one procedure: one set of
    files and one test flight, for the stage on offer. With `only`, just those canonical
    parameters (the ones a person accepted), plus every action."""
    keep = None if only is None else set(only)
    recs = [
        r
        for r in plan.recommendations
        if r.status == "next" and (r.param is None or keep is None or r.param in keep)
    ]
    return _assemble(plan, reg, recs, plan.stage)


def _assemble(
    plan: Plan, reg: Registry, recs: Sequence[Recommendation], stage: str | None
) -> list[ProcedureStep]:
    """`stage` is the one the test flight is for: what to fly and when to stop come
    from it. Anything else on offer (a safety fix, a better option) brings its own work
    and checks, but no second flight."""
    if not recs:
        return []
    texts = reg.procedure_texts
    changes = sorted(
        (r for r in recs if r.param is not None and r.status == "next"),
        key=lambda r: (r.step, r.native or ""),
    )
    out = _tasks(plan, reg, recs)
    out += _from_templates(plan, reg, recs, ("firmware", "hardware"))
    numbers = sorted({r.step for r in changes})
    after = {s.number: s.after for s in plan.steps}
    for i, n in enumerate(numbers):
        mine = [r for r in changes if r.step == n]
        out.append(
            ProcedureStep(
                "set",
                _fill(texts["set"], {"step": n, "params": _values(plan, reg, mine)}),
                tuple(r.native or "" for r in mine),
                step=n,
            )
        )
        between = after.get(n)
        if i < len(numbers) - 1 and between in ("refresh", "reboot_refresh"):
            out.append(ProcedureStep(between, dict(texts[between])))
    if any(r.reboot for r in changes):
        out.append(ProcedureStep("reboot", dict(texts["reboot"])))
    temporary = [r for r in changes if r.temporary or r.native_revert is not None]
    if temporary:
        out.append(
            ProcedureStep(
                "temporary",
                _fill(texts["temporary"], {"params": _values(plan, reg, temporary)}),
                tuple(r.native or "" for r in temporary),
            )
        )
    out += _from_templates(plan, reg, recs, ("ground_check",))
    main = [r for r in recs if r.stage == stage]
    out += _flight(plan, reg, main, stage)
    out += _abort(plan, reg, recs, stage)
    out += _verify(plan, reg, recs, stage)
    reverted = [r for r in temporary if r.native_revert is not None]
    if reverted:
        out.append(
            ProcedureStep(
                "revert",
                _fill(texts["revert"], {"params": ", ".join(r.native or "" for r in reverted)}),
                tuple(r.native or "" for r in reverted),
            )
        )
    undone = [r for r in changes if r.native_current is not None]
    if undone:
        out.append(
            ProcedureStep(
                "undo",
                _fill(texts["undo"], {"params": ", ".join(r.native or "" for r in undone)}),
                tuple(r.native or "" for r in undone),
            )
        )
    return out


def _values(plan: Plan, reg: Registry, recs: Sequence[Recommendation]) -> str:
    """NAME = value for each, naming the bits a bitmask gains or loses."""
    meta = reg.pack.metadata(plan.metadata_version, plan.vehicle)
    out: list[str] = []
    for r in recs:
        item = f"{r.native} = {format_value(r.native_value or 0)}"
        bits: Mapping[str, str] = (meta.get(r.native or "") or {}).get("bitmask") or {}
        if bits and r.native_value is not None:
            before, after = int(r.native_current or 0), int(r.native_value)
            gained = [
                bits.get(str(b), str(b))
                for b in range(32)
                if after >> b & 1 and not before >> b & 1
            ]
            lost = [
                bits.get(str(b), str(b))
                for b in range(32)
                if before >> b & 1 and not after >> b & 1
            ]
            notes = [f"+ {', '.join(gained)}"] if gained else []
            notes += [f"- {', '.join(lost)}"] if lost else []
            if notes:
                item += f" ({'; '.join(notes)})"
        out.append(item)
    return ", ".join(out)


def _templates(
    reg: Registry, rec: Recommendation, lead_only: bool = False
) -> list[Mapping[str, Any]]:
    """The templates of every finding behind a recommendation, the lead's first."""
    codes = dict.fromkeys([rec.lead.code, *(() if lead_only else (s.code for s in rec.sources))])
    return [reg.procedures[c] for c in codes if c in reg.procedures]


def _value_after(plan: Plan, param: str) -> float | None:
    """A parameter's value once the plan is loaded (canonical)."""
    for r in plan.recommendations:
        if r.param == param and r.status == "next" and r.proposed is not None:
            return r.proposed
    return plan.params.get(param)


def _applies(plan: Plan, step: Mapping[str, Any]) -> bool:
    when = step.get("when")
    if when is None:
        return True
    value = _value_after(plan, str(when["param"]))
    return value is not None and any(abs(value - float(v)) < TOLERANCE for v in when["in"])


def _from_templates(
    plan: Plan,
    reg: Registry,
    recs: Sequence[Recommendation],
    kinds: tuple[StepKind, ...],
    lead_only: bool = False,
) -> list[ProcedureStep]:
    out: list[ProcedureStep] = []
    seen: set[str] = set()
    for rec in recs:
        for template in _templates(reg, rec, lead_only):
            for s in template["steps"]:
                if s["kind"] not in kinds or not _applies(plan, s):
                    continue
                text = _fill(s["text"], rec.lead.params)
                if text["en"] in seen:
                    continue
                seen.add(text["en"])
                out.append(ProcedureStep(s["kind"], text, checks=tuple(s.get("checks", ()))))
    return out


def _tasks(plan: Plan, reg: Registry, recs: Sequence[Recommendation]) -> list[ProcedureStep]:
    """An action that no template spells out is done as its finding says."""
    out: list[ProcedureStep] = []
    for rec in recs:
        if rec.param is not None:
            continue
        work = ("task", "hardware", "firmware", "ground_check")
        if any(s["kind"] in work for t in _templates(reg, rec) for s in t["steps"]):
            continue
        text = reg.findings.get(rec.lead.code)
        if text is not None:
            title, action = _fill(text.title, rec.lead.params), _fill(text.action, rec.lead.params)
            joined = {lang: f"{title[lang].rstrip('.')}. {action[lang]}" for lang in LANGUAGES}
            out.append(ProcedureStep("task", joined))
    return out


def _stage_text(reg: Registry, kind: str, stage: str | None) -> dict[str, str] | None:
    defaults: Mapping[str, Any] = reg.rules["recommend"][kind]
    return dict(defaults[stage]) if stage is not None and stage in defaults else None


def _flight(
    plan: Plan, reg: Registry, main: Sequence[Recommendation], stage: str | None
) -> list[ProcedureStep]:
    """One test flight: what the leading findings' templates ask for, else the stage's."""
    own = _from_templates(plan, reg, main, ("fly",), lead_only=True)
    default = _stage_text(reg, "fly", stage)
    return own or ([ProcedureStep("fly", default)] if default and main else [])


def _abort(
    plan: Plan, reg: Registry, recs: Sequence[Recommendation], stage: str | None
) -> list[ProcedureStep]:
    """When to stop: the stage's criteria, and any a template adds."""
    default = _stage_text(reg, "abort", stage)
    out = [ProcedureStep("abort", default)] if default else []
    seen = {s.text["en"] for s in out}
    return out + [
        s for s in _from_templates(plan, reg, recs, ("abort",)) if s.text["en"] not in seen
    ]


def _verify(
    plan: Plan, reg: Registry, recs: Sequence[Recommendation], stage: str | None
) -> list[ProcedureStep]:
    """What the next log should show: the stage's check, then each recommendation's own
    (from its templates; a recommendation of the flight's stage with none adds nothing
    beyond the stage's, and one of another stage is checked by its own procedure)."""
    default = _stage_text(reg, "verify", stage)
    out = [ProcedureStep("verify", default)] if default else []
    seen = {s.text["en"] for s in out}
    for rec in recs:
        own = _from_templates(plan, reg, [rec], ("verify",))
        for s in own:
            if s.text["en"] not in seen:
                seen.add(s.text["en"])
                out.append(s)
    return out


def _fill(text: Mapping[str, str], params: Mapping[str, Any]) -> dict[str, str]:
    return {lang: render(text[lang], params) for lang in LANGUAGES}


def to_json(steps: Sequence[ProcedureStep]) -> list[dict[str, Any]]:
    return [
        {
            "kind": s.kind,
            "text": dict(s.text),
            "params": list(s.params),
            "step": s.step,
            "checks": [dict(c) for c in s.checks],
        }
        for s in steps
    ]


def card_text(
    plan: Plan, reg: Registry, lang: str = "en", only: Iterable[str] | None = None
) -> str:
    """The session as a numbered test card to print or keep beside the aircraft."""
    steps = session(plan, reg, only)
    texts = reg.procedure_texts
    lines = [
        f"Flight Insights: flight {plan.flight}, {plan.vehicle} {plan.firmware}",
        texts["card_header"][lang],
        "",
    ]
    lines += [f"{i}. {s.text[lang]}" for i, s in enumerate(steps, start=1)]
    return "\n".join(lines) + "\n"
