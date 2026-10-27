"""Better options (spec: Configuration review, "Better options").

Walks the catalog (knowledge/options/*.json) against the facts the flight shows. For
each option that applies and whose "when" says the aircraft is set up the weaker way:

- every need holds: the better option is available now, as parameter changes;
- a hardware need fails: it needs hardware, shown with its benefit, never automatic;
- a need is unknown: one question per unknown fact, stored with the platform's answers.

Options are ranked by their benefit for the platform's priority answer (the highest
of any priority when it is unanswered), then by cost.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from flight_insights.analyzers.facts import Fact, facts
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import Analyzer, CandidateChange, Evidence, Finding, Result
from flight_insights.analyzers.motors import picture
from flight_insights.analyzers.registry import OPTION_COSTS, OPTION_STAGES, Option, Registry

HIGH_BENEFIT = 3  # a benefit score this high for the aircraft's priority is a caution
TOLERANCE = 1e-6


def _same(value: float, wanted: Any) -> bool:
    """Equal as the log stores it: parameters are 32-bit floats (0.15 is 0.15000000596)."""
    return abs(float(value) - float(wanted)) <= TOLERANCE * max(1.0, abs(float(wanted)))


def _condition(
    c: Mapping[str, Any],
    params: Mapping[str, float],
    known: Mapping[str, Fact],
    defaults: Mapping[str, float] | None = None,
) -> bool | None:
    """True, False, or None when it cannot be judged (an unknown fact, a missing
    parameter, a default the log does not record)."""
    if "fact" in c:
        value = known.get(c["fact"])
        if value is None:
            return None
        if "is" in c:
            return bool(value) == bool(c["is"]) if isinstance(value, bool) else None
        if "gt" in c:
            return float(value) > float(c["gt"])
        if "eq" in c:
            return float(value) == float(c["eq"])
        raise ValueError(f"unknown fact condition {c}")
    if "default" in c:  # the firmware's own default, as the log records it
        default = (defaults or {}).get(c["default"])
        if default is None:
            return None
        if "eq" in c:
            return _same(default, c["eq"])
        raise ValueError(f"unknown default condition {c}")
    value = params.get(c["param"])
    if value is None:
        return None
    if "eq" in c:
        return _same(value, c["eq"])
    if "in" in c:
        return any(_same(value, v) for v in c["in"])
    if "bit_clear" in c:
        return not (int(value) >> int(c["bit_clear"]) & 1)
    if "bit_set" in c:
        return bool(int(value) >> int(c["bit_set"]) & 1)
    if "gt" in c:
        return float(value) > float(c["gt"])
    if "lt" in c:
        return float(value) < float(c["lt"])
    raise ValueError(f"unknown parameter condition {c}")


def _changes(
    o: Option, outcome: Mapping[str, Any], params: Mapping[str, float], known: Mapping[str, Fact]
) -> tuple[CandidateChange, ...] | None:
    """The parameter changes of an available outcome; None if a value it needs is unknown."""
    stage = OPTION_STAGES[o.area]
    out: list[CandidateChange] = []
    for ch in outcome.get("changes", []):
        name = str(ch["param"])
        current = params.get(name)
        spec: Any = ch["value"]
        if isinstance(spec, dict) and "fact" in spec:
            fact = known.get(str(cast(dict[str, Any], spec)["fact"]))
            if fact is None:
                return None
            value = float(fact)
        elif isinstance(spec, dict) and "or_bits" in spec:
            value = float(int(current or 0) | int(cast(dict[str, Any], spec)["or_bits"]))
        else:
            value = float(cast(float, spec))
        out.append(CandidateChange(name, current, value, stage))
    return tuple(out)


def _benefit(o: Option, priority: str | None) -> int:
    if priority and priority in o.benefit_by_priority:
        return int(o.benefit_by_priority[priority])
    return max(o.benefit_by_priority.values(), default=0)


def _options(flight: FlightData, reg: Registry) -> Result:
    if not reg.options:
        return Result(skipped="the knowledge pack has no Better options catalog")
    known = facts(flight, picture(flight))
    params, defaults = flight.params, flight.defaults
    priority = flight.answers.get("priority")
    ev = Evidence(
        id="options:facts",
        kind="fact",
        summary="what the flight, its parameters and the setup answers show",
        messages=(),
        values={k: v for k, v in known.items()},
    )
    out = Result(evidence=[ev])
    asked: dict[str, list[str]] = {}
    for oid, o in reg.options.items():
        if not all(_condition(c, params, known, defaults) is True for c in o.applies):
            continue
        if not all(_condition(c, params, known, defaults) is True for c in o.when):
            continue
        decided: Mapping[str, Any] | None = None
        unknown: str | None = None
        for outcome in o.outcomes:
            results = [(c, _condition(c, params, known, defaults)) for c in outcome["requires"]]
            if all(r is True for _, r in results):
                decided = outcome
                break
            if unknown is None and not any(r is False for _, r in results):
                unknown = next(c["fact"] for c, r in results if r is None and "fact" in c)
        benefit = _benefit(o, priority)
        rank = benefit * len(OPTION_COSTS) - OPTION_COSTS.index(o.cost)
        common = {"option": oid, "benefit": benefit, "cost": o.cost, "rank": rank}
        if decided is not None and decided["outcome"] == "available":
            changes = _changes(o, decided, params, known)
            if changes is None:
                continue
            out.findings.append(
                Finding(
                    code=f"option.{oid}.available",
                    severity="caution" if benefit >= HIGH_BENEFIT else "info",
                    confidence="high",
                    evidence=(ev,),
                    params=common,
                    changes=changes,
                )
            )
        elif decided is not None and decided["outcome"] == "hardware":
            out.findings.append(
                Finding(
                    code=f"option.{oid}.hardware",
                    severity="info",
                    confidence="high",
                    evidence=(ev,),
                    params=common,
                )
            )
        elif unknown is not None and unknown in reg.questions:
            field = str(reg.questions[unknown]["field"])
            if flight.answers.get(field) in (None, ""):  # answered but still unknown: no use asking
                asked.setdefault(unknown, []).append(oid)
    for fact, oids in asked.items():
        out.findings.append(
            Finding(
                code=f"option.question.{fact}",
                severity="info",
                confidence="high",
                evidence=(ev,),
                params={"field": str(reg.questions[fact]["field"]), "options": oids},
            )
        )
    out.findings.sort(key=lambda f: -int(f.params.get("rank", 0)))
    return out


OPTIONS = Analyzer(
    name="options",
    version=2,
    run=_options,
    metrics=(),
    findings=("option.*",),  # one code per catalog entry and question, from the pack
)
