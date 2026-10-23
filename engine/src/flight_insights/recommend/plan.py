"""The recommendation engine: a flight's findings become a staged plan (spec:
Recommendation engine).

Analyzers only suggest candidate changes. This module turns them into what a person
reviews before the next test flight:

1. Each candidate is checked against the firmware's own metadata, in its dialect
   (name, read-only, range, values, bitmask bits), and against the changes that are
   never proposed automatically (skipping arming checks, switching failsafes off,
   raising limits). A candidate that fails is kept, blocked, with its reason.
2. Candidates for the same parameter are merged: bitmask changes that touch
   different bits combine; otherwise safety beats stability beats performance, then
   higher confidence wins, and a tie between different values is disputed, never
   silently resolved. Measured evidence against an AI suggestion is disputed too.
3. Gain changes beyond the per-flight step limit are split across flights.
4. A parameter whose feature is switched off gets its enable parameter first; when the
   parameter only appears once the feature is on, it moves to a later step, after a
   reboot and parameter refresh.
5. Stages follow ArduPilot's tuning order: only the earliest stage with work left is
   offered, plus anything safety-related; later stages wait.

A finding at caution or worse with no change to offer (a loose mount, a hot ESC)
becomes an action for a person, with no parameter. Nothing here writes to an aircraft.
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from flight_insights.analyzers.registry import LANGUAGES, STAGES, Registry
from flight_insights.params.dialect import Dialect
from flight_insights.params.files import format_value

Kind = Literal["safety", "stability", "performance"]
Status = Literal["next", "waiting", "blocked", "disputed"]
After = Literal["refresh", "reboot", "fly"]

KINDS: tuple[Kind, ...] = ("safety", "stability", "performance")
CONFIDENCES = ("high", "medium", "low")
SEVERITIES = ("safety", "out", "caution", "info")
ACTION_SEVERITIES = ("safety", "out", "caution")  # these need a person even with no change
PLACEHOLDER = re.compile(r"\{(\w+)\}")
TOLERANCE = 1e-6


@dataclass(frozen=True)
class Source:
    """A finding that proposes a change, and what it rests on."""

    finding: str  # its id in the store
    analyzer: str
    code: str
    severity: str
    confidence: str
    params: Mapping[str, Any]
    evidence: tuple[str, ...]
    origin: Literal["measured", "ai"] = "measured"

    @property
    def kind(self) -> Kind:
        if self.severity == "safety":
            return "safety"
        if self.severity in ("out", "caution") and not self.code.startswith("option."):
            return "stability"
        return "performance"  # a better option, or something worth knowing

    def priority(self) -> tuple[int, int, int, int]:
        """Lower wins: kind, then measured over inferred, confidence and severity."""
        return (
            KINDS.index(self.kind),
            0 if self.origin == "measured" else 1,
            CONFIDENCES.index(self.confidence),
            SEVERITIES.index(self.severity),
        )


@dataclass(frozen=True)
class Note:
    """Why a recommendation is blocked, disputed, waiting or split: a code in the
    rules' texts and the values its wording fills in."""

    reason: str
    params: Mapping[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class Alternative:
    """A value another finding proposed for the same parameter, and lost or disputes."""

    proposed: float
    sources: tuple[Source, ...]


@dataclass
class Recommendation:
    param: str | None  # canonical name; None for an action with no parameter
    native: str | None  # the name this firmware uses
    current: float | None  # canonical units
    proposed: float | None  # this flight's step, canonical units
    stage: str
    sources: list[Source]
    status: Status = "next"
    notes: list[Note] = field(default_factory=list[Note])
    target: float | None = None  # the final value, when split across flights
    native_value: float | None = None  # proposed, current and target in this firmware's units
    native_current: float | None = None
    native_target: float | None = None
    reboot: bool = False
    step: int = 1
    requires: list[str] = field(default_factory=list[str])
    alternatives: list[Alternative] = field(default_factory=list[Alternative])

    @property
    def lead(self) -> Source:
        return min(self.sources, key=Source.priority)

    @property
    def kind(self) -> Kind:
        return self.lead.kind

    @property
    def confidence(self) -> str:
        return self.lead.confidence


@dataclass(frozen=True)
class Step:
    number: int
    params: tuple[str, ...]  # native names, in the order they are loaded
    after: After  # what to do once they are written


@dataclass
class Plan:
    flight: str
    vehicle: str
    firmware: str
    metadata_version: str  # the release whose metadata validated the plan
    exact: bool  # False: the firmware's own release is not in the pack
    stage: str | None  # the stage on offer; None when nothing is left to do
    recommendations: list[Recommendation]
    steps: list[Step]

    def offered(self, step: int | None = None) -> list[Recommendation]:
        return [
            r
            for r in self.recommendations
            if r.status == "next" and r.param is not None and (step is None or r.step == step)
        ]


# --- rules ----------------------------------------------------------------------------


def _matches(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _never(
    rules: Mapping[str, Any], param: str, current: float | None, value: float
) -> Note | None:
    never: Mapping[str, Any] = rules["never"]
    if _matches(param, never["add_bits"]["names"]) and int(value) & ~int(current or 0):
        return Note("never_add_bits", {"param": param})
    if _matches(param, never["disable"]["names"]) and value == 0 and current != 0:
        return Note("never_disable", {"param": param})
    raise_rule: Mapping[str, Any] = never["raise"]
    if _matches(param, raise_rule["names"]):
        unlimited = _matches(param, raise_rule["zero_means_unlimited"])

        def level(v: float) -> float:
            return math.inf if unlimited and v == 0 else v

        if current is None or level(value) > level(current):
            return Note("never_raise", {"param": param})
    return None


def _validate(
    native: str, value: float, meta: Mapping[str, Any] | None, firmware: str
) -> Note | None:
    """The value against this firmware's metadata for the parameter."""
    if meta is None:
        return Note("unknown_param", {"param": native, "firmware": firmware})
    if meta.get("read_only"):
        return Note("read_only", {"param": native})
    text = format_value(value)
    bounds = meta.get("range")
    if bounds is not None:
        low, high = float(bounds[0]), float(bounds[1])
        if not low - TOLERANCE <= value <= high + TOLERANCE:
            return Note(
                "out_of_range",
                {
                    "param": native,
                    "value": text,
                    "low": format_value(low),
                    "high": format_value(high),
                },
            )
    elif meta.get("values"):  # without a range the listed values are the only ones
        values = cast(Mapping[str, str], meta["values"])
        if not any(abs(float(k) - value) < TOLERANCE for k in values):
            return Note("not_a_value", {"param": native, "value": text})
    if meta.get("bitmask"):
        bits = {int(k) for k in cast(Mapping[str, str], meta["bitmask"])}
        defined = sum(1 << b for b in bits)
        if value < 0 or value != int(value) or int(value) & ~defined:
            return Note("undefined_bits", {"param": native, "value": text})
    return None


def _step_limit(rules: Mapping[str, Any], param: str) -> float | None:
    for limit in rules["step_limits"]:
        if _matches(param, limit["names"]):
            return float(limit["max_fraction"])
    return None


def _enable_for(param: str, names: Iterable[str]) -> str | None:
    """The parameter that switches `param`'s feature on: the longest `<group>_ENABLE`
    whose group prefixes it (INS_HNTCH_ENABLE for INS_HNTCH_FREQ)."""
    groups = [
        name
        for name in names
        if name != param and name.endswith("_ENABLE") and param.startswith(name[: -len("ENABLE")])
    ]
    return max(groups, key=len, default=None)


# --- building the plan ------------------------------------------------------------------


@dataclass
class _Candidate:
    param: str
    proposed: float
    stage: str
    source: Source


def _sources(findings: Sequence[Mapping[str, Any]]) -> list[tuple[Source, Mapping[str, Any]]]:
    return [
        (
            Source(
                finding=str(f["id"]),
                analyzer=str(f["analyzer"]),
                code=str(f["code"]),
                severity=str(f["severity"]),
                confidence=str(f["confidence"]),
                params=dict(f.get("params") or {}),
                evidence=tuple(f.get("evidence") or ()),
                origin=f.get("origin", "measured"),
            ),
            f,
        )
        for f in findings
    ]


def _is_bitmask(meta: Mapping[str, Any] | None) -> bool:
    return bool(meta and meta.get("bitmask"))


def _bits(current: float | None, value: float) -> tuple[int, int]:
    """Bits a bitmask value sets and clears against the current one."""
    now = int(current or 0)
    return int(value) & ~now, now & ~int(value)


def _resolve(
    param: str, current: float | None, group: list[_Candidate], bitmask: bool
) -> Recommendation:
    group = sorted(group, key=lambda c: c.source.priority())
    top = group[0]
    rec = Recommendation(param, None, current, top.proposed, top.stage, [top.source])
    sets, clears = _bits(current, top.proposed) if bitmask else (0, 0)
    rivals: dict[float, list[Source]] = {}
    for c in group[1:]:
        if c.proposed == rec.proposed and not bitmask:
            rec.sources.append(c.source)
            continue
        if bitmask:
            s, cl = _bits(current, c.proposed)
            if not (s & clears or cl & sets):  # touches other bits: both changes stand
                sets, clears = sets | s, clears | cl
                rec.sources.append(c.source)
                continue
        rivals.setdefault(c.proposed, []).append(c.source)
    if bitmask:
        rec.proposed = float((int(current or 0) | sets) & ~clears)
    lead = top.source.priority()
    for value, sources in rivals.items():
        rec.alternatives.append(Alternative(value, tuple(sources)))
        rival = min(sources, key=Source.priority)
        same_rank = rival.priority()[:3] == lead[:3]
        if same_rank or rival.origin != top.source.origin:
            rec.status = "disputed"
    if rec.status == "disputed":
        rec.notes.append(Note("disputed", {"param": param}))
    return rec


def build_plan(
    flight: str,
    findings: Sequence[Mapping[str, Any]],
    params: Mapping[str, float],
    dialect: Dialect,
    reg: Registry,
) -> Plan:
    """The staged plan for one flight's findings. `params` are the flight's own,
    canonical; `findings` as analysis results hold them (with "changes")."""
    rules: Mapping[str, Any] = reg.rules["recommend"]
    firmware = dialect.firmware
    canonical_names = dialect.pack.metadata(dialect.canonical_version, dialect.vehicle)

    def native(param: str, value: float | None) -> tuple[str, float | None]:
        return dialect.native(param, value)

    def check(param: str, value: float) -> Note | None:
        name, nvalue = native(param, value)
        return _never(rules, param, params.get(param), value) or _validate(
            name, value if nvalue is None else nvalue, dialect.meta.get(name), firmware
        )

    recs: list[Recommendation] = []
    candidates: dict[str, list[_Candidate]] = {}
    blocked: dict[tuple[str, float], Recommendation] = {}
    sources = _sources(findings)
    by_code: dict[str, list[Source]] = {}
    for source, _ in sources:
        by_code.setdefault(source.code, []).append(source)
    for source, f in sources:
        # A finding that replaces other findings' changes answers them too, and takes on
        # their weight: the filter search's whole set fixes the flags it explains.
        explained = [e for code in source.params.get("explains", ()) for e in by_code.get(code, ())]
        for ch in f.get("changes") or ():
            param, proposed = str(ch["param"]), ch.get("proposed")
            if proposed is None:
                continue
            proposed = float(proposed)
            current = params.get(param)
            if current is not None and abs(current - proposed) < TOLERANCE:
                continue  # already set
            note = check(param, proposed)
            if note is not None:
                rec = blocked.setdefault(
                    (param, proposed),
                    Recommendation(
                        param, None, current, proposed, str(ch["stage"]), [], "blocked", [note]
                    ),
                )
                rec.sources += [source, *explained]
                continue
            candidates.setdefault(param, []).extend(
                _Candidate(param, proposed, str(ch["stage"]), s) for s in (source, *explained)
            )

    by_param: dict[str, Recommendation] = {}
    for param, group in candidates.items():
        name, _ = native(param, None)
        rec = _resolve(param, params.get(param), group, _is_bitmask(dialect.meta.get(name)))
        by_param[param] = rec

    # Gain changes past the per-flight limit: this flight takes one step towards the target.
    for param, rec in by_param.items():
        limit = _step_limit(rules, param)
        current, target = rec.current, rec.proposed
        if limit is None or rec.status != "next" or not current or target is None:
            continue
        fraction = abs(target - current) / abs(current)
        if fraction <= limit + TOLERANCE:
            continue
        steps = math.ceil(fraction / limit - TOLERANCE)
        value = current + math.copysign(limit * abs(current), target - current)
        increment = (dialect.meta.get(native(param, None)[0]) or {}).get("increment")
        if increment:
            value = round(value / float(increment)) * float(increment)
        rec.target, rec.proposed = target, float(format_value(value))
        rec.notes.append(
            Note(
                "split", {"step": 1, "steps": steps, "target": format_value(target), "param": param}
            )
        )

    # Enable parameters first; parameters that only appear once enabled come a step later.
    for param in list(by_param):
        rec = by_param[param]
        enable = _enable_for(param, canonical_names)
        if enable is None or rec.status != "next":
            continue
        if not params.get(enable):  # off now: this plan switches it on first
            if enable not in by_param:
                note = check(enable, 1.0)
                if note is not None:
                    rec.status = "blocked"
                    rec.notes.append(note)
                    continue
                by_param[enable] = Recommendation(
                    enable, None, params.get(enable), 1.0, rec.stage, list(rec.sources)
                )
            rec.notes.append(
                Note("needs_enable", {"enable": native(enable, None)[0], "param": param})
            )
            if rec.current is None:
                rec.step = by_param[enable].step + 1
        if enable in by_param:
            value = format_value(by_param[enable].proposed or 1)
            rec.requires.append(f"{native(enable, None)[0]}={value}")

    recs = [*by_param.values(), *blocked.values()]
    for rec in recs:
        assert rec.param is not None and rec.proposed is not None
        rec.native, rec.native_value = native(rec.param, rec.proposed)
        if rec.native_value is None:
            rec.native_value = rec.proposed
        rec.native_current = _native_value(dialect, rec.param, rec.current)
        rec.native_target = _native_value(dialect, rec.param, rec.target)
        rec.reboot = bool((dialect.meta.get(rec.native) or {}).get("reboot_required"))

    # Findings that need a person and leave nothing to load become actions.
    answered = {s.finding for r in recs if r.status in ("next", "disputed") for s in r.sources}
    for source, f in sources:
        if source.severity in ACTION_SEVERITIES and source.finding not in answered:
            stage = str(f.get("stage") or "mechanical")
            recs.append(Recommendation(None, None, None, None, stage, [source]))

    # The earliest stage with a problem left is on offer, with safety at any stage. A
    # better option fixes no problem, so it never holds a stage back: it comes with the
    # stage on offer when it belongs to that stage or an earlier one.
    open_recs = [r for r in recs if r.status != "blocked" and r.stage in STAGES]
    pool = [r.stage for r in open_recs if r.kind != "performance"] or [r.stage for r in open_recs]
    gate = min(pool, key=STAGES.index) if pool else None
    for rec in recs:
        later = (
            gate is None or rec.stage not in STAGES or STAGES.index(rec.stage) > STAGES.index(gate)
        )
        if rec.status == "next" and later and rec.kind != "safety":
            rec.status = "waiting"
            rec.notes.append(Note("waiting", {"stage": gate}))

    # Changes from one finding are loaded together; a reboot joins them where needed.
    offered = [r for r in recs if r.status == "next" and r.param is not None]
    for rec in offered:
        mine = {s.finding for s in rec.sources}
        rec.requires += [
            f"{o.native}={format_value(o.native_value or 0)}"
            for o in offered
            if o is not rec
            and o.step == rec.step
            and mine & {s.finding for s in o.sources}
            and f"{o.native}={format_value(o.native_value or 0)}" not in rec.requires
        ]
        if rec.reboot:
            rec.requires.append("reboot")

    steps = _steps(offered)
    recs.sort(key=lambda r: (
        ("next", "disputed", "waiting", "blocked").index(r.status),
        KINDS.index(r.kind), STAGES.index(r.stage) if r.stage in STAGES else len(STAGES),
        r.step, r.native or "",
    ))  # fmt: skip
    return Plan(
        flight=flight,
        vehicle=dialect.vehicle,
        firmware=firmware,
        metadata_version=dialect.choice.version,
        exact=dialect.choice.exact,
        stage=gate,
        recommendations=recs,
        steps=steps,
    )


def _native_value(dialect: Dialect, param: str, value: float | None) -> float | None:
    if value is None:
        return None
    converted = dialect.native(param, value)[1]
    return value if converted is None else converted


def _steps(offered: list[Recommendation]) -> list[Step]:
    numbers = sorted({r.step for r in offered})
    out: list[Step] = []
    for i, n in enumerate(numbers, start=1):
        mine = sorted((r for r in offered if r.step == n), key=lambda r: r.native or "")
        for r in mine:
            r.step = i
        last = i == len(numbers)
        after: After = "fly" if last else "refresh"
        if last and any(r.reboot for r in mine):
            after = "reboot"
        out.append(Step(i, tuple(r.native or "" for r in mine), after))
    return out


# --- wording ----------------------------------------------------------------------------


def render(template: str, params: Mapping[str, Any]) -> str:
    """A text with its {placeholders} filled; a missing one is left as written."""

    def value(m: re.Match[str]) -> str:
        v = params.get(m.group(1))
        if v is None:
            return m.group(0)
        if isinstance(v, float):
            return format_value(v)
        if isinstance(v, list | tuple):
            return ", ".join(str(x) for x in cast(Sequence[Any], v))
        return str(v)

    return PLACEHOLDER.sub(value, template)


def texts(rules: Mapping[str, Any], key: str, params: Mapping[str, Any]) -> dict[str, str]:
    return {lang: render(rules["texts"][key][lang], params) for lang in LANGUAGES}


def why(reg: Registry, source: Source) -> dict[str, str]:
    text = reg.findings.get(source.code)
    if text is None:
        return {lang: source.code for lang in LANGUAGES}
    return {lang: render(text.title[lang], source.params) for lang in LANGUAGES}


def expected_effect(reg: Registry, rec: Recommendation) -> dict[str, Any]:
    effects: Mapping[str, Any] = reg.rules["recommend"]["effects"]
    lead = rec.lead
    simulated = lead.code in effects
    entry = effects.get(lead.code, effects["default"])
    return {"simulated": simulated} | {lang: render(entry[lang], lead.params) for lang in LANGUAGES}


def to_json(plan: Plan, reg: Registry) -> dict[str, Any]:
    rules: Mapping[str, Any] = reg.rules["recommend"]

    def source(s: Source) -> dict[str, Any]:
        return {
            "finding": s.finding, "analyzer": s.analyzer, "code": s.code,
            "severity": s.severity, "confidence": s.confidence, "params": dict(s.params),
            "evidence": list(s.evidence), "origin": s.origin, "why": why(reg, s),
        }  # fmt: skip

    def one(r: Recommendation) -> dict[str, Any]:
        lead = r.lead
        return {
            "param": r.native,
            "canonical": r.param,
            "finding": lead.finding,
            "current_canonical": r.current,
            "proposed_canonical": r.proposed,
            "current": r.native_current,
            "proposed": r.native_value,
            "target": r.native_target,
            "stage": r.stage,
            "kind": r.kind,
            "status": r.status,
            "step": r.step if r.status == "next" and r.param is not None else None,
            "why": why(reg, lead),
            "evidence": sorted({e for s in r.sources for e in s.evidence}),
            "expected_effect": expected_effect(reg, r),
            "confidence": r.confidence,
            "risk": rules["risk"].get(r.stage, "medium"),
            "requires": list(r.requires),
            "reboot": r.reboot,
            "verify": dict(rules["verify"][r.stage]) if r.stage in rules["verify"] else None,
            "source": f"finding:{lead.code}",
            "sources": [source(s) for s in sorted(r.sources, key=Source.priority)],
            "notes": [
                {
                    "reason": n.reason,
                    "params": dict(n.params),
                    "text": texts(rules, n.reason, n.params),
                }
                for n in r.notes
            ],
            "alternatives": [
                {"proposed": a.proposed, "sources": [source(s) for s in a.sources]}
                for a in r.alternatives
            ],
        }

    return {
        "flight": plan.flight,
        "vehicle": plan.vehicle,
        "firmware": plan.firmware,
        "metadata_version": plan.metadata_version,
        "exact": plan.exact,
        "stage": plan.stage,
        "recommendations": [one(r) for r in plan.recommendations],
        "steps": [
            {"number": s.number, "params": list(s.params), "after": s.after} for s in plan.steps
        ],
    }
