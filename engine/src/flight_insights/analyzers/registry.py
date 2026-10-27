"""The metric registry (spec: Explanations for every metric and data point).

Every metric the app computes, and every finding it can raise, is described in
knowledge/metrics/*.json: what it is, how it is measured, its unit and direction,
its zones and where each zone comes from, in English and Ukrainian. Engineers write
it; the engine refuses to load an entry with any of that missing, and a test fails
if an analyzer emits a metric or finding the registry does not describe, so nothing
the app shows goes unexplained.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from flight_insights.analyzers.model import Zone
from flight_insights.knowledge import KnowledgeError, Pack

LANGUAGES = ("en", "uk")
DIRECTIONS = ("lower", "higher", "band")
ZONES: tuple[Zone, ...] = ("target", "caution", "out")
# "app": a rule of the app's own, stated with its reason, where no published range exists
SOURCE_KINDS = ("ardupilot", "datasheet", "research", "community", "platform", "baseline", "app")
STAGES = (
    "mechanical", "evidence", "setup", "filters", "rate tune", "autotune", "feel",
    "navigation", "operations",
)  # fmt: skip
PLACEHOLDER = re.compile(r"\{(\w+)")


@dataclass(frozen=True)
class Metric:
    id: str
    name: Mapping[str, str]
    unit: str
    direction: str
    definition: Mapping[str, str]
    method: Mapping[str, str]
    messages: tuple[str, ...]
    # Each zone is one or more [low, high) intervals, None unbounded. A metric judged
    # against the airframe's own history (source kind "baseline") has none: its zone
    # comes from the trends, not from a fixed range.
    zones: Mapping[Zone, tuple[tuple[float | None, float | None], ...]]
    source: Mapping[str, str]

    def zone(self, value: float) -> Zone | None:
        """The zone `value` falls in; None for a value no zone covers (NaN), or for a
        metric with no fixed zones."""
        if math.isnan(value):
            return None
        for z in ZONES:
            for low, high in self.zones.get(z, ()):
                if (low is None or value >= low) and (high is None or value < high):
                    return z
        return None


@dataclass(frozen=True)
class FindingText:
    code: str
    title: Mapping[str, str]  # with {placeholders} the finding's params fill in
    action: Mapping[str, str]
    stage: str


@dataclass(frozen=True)
class Option:
    """A Better options catalog entry (knowledge/options/*.json)."""

    id: str
    area: str
    subsystem: str
    title: Mapping[str, str]
    hardware: Mapping[str, str]
    benefit: Mapping[str, str]
    how: Mapping[str, str]
    applies: tuple[Mapping[str, Any], ...]
    when: tuple[Mapping[str, Any], ...]
    outcomes: tuple[Mapping[str, Any], ...]
    benefit_by_priority: Mapping[str, int]
    cost: str
    sources: tuple[Mapping[str, str], ...]


OPTION_STAGES = {
    "filters": "filters",
    "logging": "evidence",
    "motors": "setup",
    "control": "setup",
    "power": "setup",
    "feel": "feel",
}
OPTION_COSTS = ("parameter", "reboot", "firmware", "hardware")
# Steps a procedure template may give; the plan adds its own parameter files, refreshes,
# reboots, temporary settings, revert and undo (spec: Recommendation engine, "Procedures").
PROCEDURE_KINDS = ("hardware", "firmware", "ground_check", "fly", "abort", "verify")
# What the app can test on the next flight, and the fields each test needs.
CHECKS: Mapping[str, tuple[str, ...]] = {
    "finding_clears": (),
    "has_message": ("message",),
    "metric": ("metric",),
    "has_metric": ("metric",),
    "param_is": ("param", "value"),
    # in every payload band flown before the change, flown again with it, the metric
    # better by at least `by` (a share) with 95% intervals apart
    "metric_improves": ("metric", "by"),
    # the same bands, flown again with it: no band worse by `by` with intervals apart
    "metric_not_worse": ("metric", "by"),
    # no metric shifted worse after the change, beyond the airframe's spread (attribution)
    "no_shift_worse": (),
    # with the change, the heaviest band's value within max_ratio times, or max_above
    # over, the lightest band's
    "metric_vs_lightest": ("metric",),
}
METRIC_CHECKS = (
    "metric", "has_metric", "metric_improves", "metric_not_worse", "metric_vs_lightest",
)  # fmt: skip
PARAM_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,15}")


@dataclass(frozen=True)
class AttributionLink:
    """Parameters that should move metrics (rules/attribution.json): each metric with
    the sign it moves by (+1 with the value, -1 against it, 0 either way) and a weight."""

    group: str
    params: tuple[re.Pattern[str], ...]
    metrics: tuple[tuple[str, int, float], ...]  # metric name, may hold {axis}
    why: Mapping[str, str]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Registry:
    """What the knowledge pack tells the analyzers: the metric and finding registry,
    the curated rules (knowledge/rules/*.json, by file stem), and each release's names
    for the numbers logs record."""

    metrics: Mapping[str, Metric]
    findings: Mapping[str, FindingText]
    version: str  # changes whenever any registry, rule, option or name file changes
    rules: Mapping[str, Any]
    pack: Pack
    options: Mapping[str, Option] = dataclasses.field(default_factory=dict[str, Option])
    questions: Mapping[str, Mapping[str, Any]] = dataclasses.field(default_factory=dict[str, Any])
    coverage: Mapping[str, Mapping[str, Any]] = dataclasses.field(default_factory=dict[str, Any])
    # finding code -> {"temporary", "add_temporary", "steps"}, shared parts merged in
    procedures: Mapping[str, Mapping[str, Any]] = dataclasses.field(default_factory=dict[str, Any])
    procedure_texts: Mapping[str, Mapping[str, str]] = dataclasses.field(
        default_factory=dict[str, Any]
    )
    attribution: tuple[AttributionLink, ...] = ()

    def names(self, firmware: str | None) -> Mapping[str, Any]:
        """Mode, event and error names of the flight's release, or the nearest earlier
        one the pack has; the oldest release's when the firmware is unknown."""
        return self.pack.enums(firmware or "0")

    @staticmethod
    def load(pack: Pack) -> Registry:
        metrics: dict[str, Metric] = {}
        findings: dict[str, FindingText] = {}
        for path in sorted((pack.root / "metrics").glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            for mid, m in doc.get("metrics", {}).items():
                if mid in metrics:
                    raise KnowledgeError(f"{path.name}: metric {mid} is defined twice")
                metrics[mid] = _metric(path.name, mid, m)
            for code, f in doc.get("findings", {}).items():
                if code in findings:
                    raise KnowledgeError(f"{path.name}: finding {code} is defined twice")
                findings[code] = _finding(path.name, code, f)
        rules = {
            path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((pack.root / "rules").glob("*.json"))
        }
        options, questions, coverage = _options(pack, findings)
        procedures, procedure_texts = _procedures(pack, findings, metrics)
        _attribution_weather(rules, metrics)
        return Registry(
            metrics, findings, knowledge_version(pack), rules, pack, options, questions,
            coverage, procedures, procedure_texts, _attribution(rules, metrics),
        )  # fmt: skip


def _attribution(
    rules: Mapping[str, Any], metrics: Mapping[str, Metric]
) -> tuple[AttributionLink, ...]:
    """The parameter-to-metric map, checked: patterns that compile, metrics the registry
    knows (for some axis, where one is named), signs and strengths, and its texts."""
    doc: Mapping[str, Any] | None = rules.get("attribution")
    if doc is None:
        return ()
    strengths: Mapping[str, float] = doc["strengths"]
    axes = set(cast(Mapping[str, str], doc["axes"]).values())
    out: list[AttributionLink] = []
    for i, raw in enumerate(cast(list[Mapping[str, Any]], doc["links"])):
        where = f"attribution.json: link {raw.get('group', i)}"
        try:
            params = tuple(re.compile(str(p)) for p in raw["params"])
        except re.error as e:
            raise KnowledgeError(f"{where}: a parameter pattern does not compile: {e}") from None
        captures = any("axis" in p.groupindex for p in params)
        links: list[tuple[str, int, float]] = []
        for m in cast(list[Mapping[str, Any]], raw["metrics"]):
            name, sign, strength = str(m["metric"]), m.get("sign"), m.get("strength")
            if sign not in (-1, 0, 1) or strength not in strengths:
                raise KnowledgeError(f"{where}: {name} needs a sign of -1, 0 or 1 and a strength")
            if "{axis}" in name and not captures:
                raise KnowledgeError(f"{where}: {name} names an axis its parameters do not give")
            names = {name.format(axis=a) for a in axes} if "{axis}" in name else {name}
            if not names & set(metrics):
                raise KnowledgeError(f"{where}: no metric {name}")
            links.append((name, int(sign), float(strengths[str(strength)])))
        sources = raw.get("sources")
        if not (isinstance(sources, list) and sources):
            raise KnowledgeError(f"{where}: needs its sources")
        out.append(
            AttributionLink(
                str(raw["group"]), params, tuple(links), _text(where, raw, "why"),
                tuple(str(x) for x in cast(list[Any], sources)),
            )
        )  # fmt: skip
    return tuple(out)


def _attribution_weather(rules: Mapping[str, Any], metrics: Mapping[str, Metric]) -> None:
    """The weather confounder's rules, checked: each variable's threshold, and metric
    prefixes that each name some metric the registry knows."""
    doc: Mapping[str, Any] | None = rules.get("attribution", {}).get("weather")
    if doc is None:
        return
    where = "attribution.json: weather"
    if not 0.0 <= float(doc["min_coverage"]) <= 1.0:
        raise KnowledgeError(f"{where}: min_coverage must be a share")
    for name, v in cast(Mapping[str, Mapping[str, Any]], doc["variables"]).items():
        if not float(v["differ"]) > 0.0:
            raise KnowledgeError(f"{where}: {name} needs a threshold above 0")
        for prefix in cast(list[str], v["metrics"]):
            if not any(m.startswith(prefix) for m in metrics):
                raise KnowledgeError(f"{where}: {name}: no metric starts with {prefix}")
    sources = doc.get("sources")
    if not (isinstance(sources, list) and sources):
        raise KnowledgeError(f"{where}: needs its sources")


def knowledge_version(pack: Pack) -> str:
    """A short hash of every file an analysis reads from the pack: the registry, the
    rules, the names and the motor frames. A result made with another version is
    recomputed, not reused."""
    h = hashlib.sha256()
    for folder in ("metrics", "rules", "enums", "options", "procedures", "frames"):
        for path in sorted((pack.root / folder).glob("*.json")):
            h.update(f"{folder}/{path.name}\0".encode())
            h.update(path.read_bytes())
    return "kp-" + h.hexdigest()[:16]


def _text(where: str, entry: Mapping[str, Any], key: str) -> Mapping[str, str]:
    raw = entry.get(key)
    text = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    if not all(isinstance(text.get(lang), str) and str(text[lang]).strip() for lang in LANGUAGES):
        raise KnowledgeError(f"{where}: {key} needs text in {', '.join(LANGUAGES)}")
    return cast(dict[str, str], text)


def _metric(file: str, mid: str, m: Mapping[str, Any]) -> Metric:
    where = f"{file}: metric {mid}"
    if not isinstance(m.get("unit"), str):
        raise KnowledgeError(f"{where}: needs a unit")
    if m.get("direction") not in DIRECTIONS:
        raise KnowledgeError(f"{where}: direction must be one of {', '.join(DIRECTIONS)}")
    source = m.get("source")
    if not isinstance(source, dict) or cast(dict[str, Any], source).get("kind") not in SOURCE_KINDS:
        raise KnowledgeError(f"{where}: needs a source of kind {', '.join(SOURCE_KINDS)}")
    raw_zones = m.get("zones")
    baseline = cast(dict[str, Any], source).get("kind") == "baseline"
    if baseline and raw_zones is None:
        raw_zones = {}
    elif not isinstance(raw_zones, dict) or "target" not in raw_zones:
        raise KnowledgeError(f"{where}: needs zones, with a target zone at least")
    zones: dict[Zone, tuple[tuple[float | None, float | None], ...]] = {}
    for z, bounds in cast(dict[str, Any], raw_zones).items():
        if z not in ZONES or not isinstance(bounds, list):
            raise KnowledgeError(f"{where}: zone {z} must be one of {ZONES} as [low, high]")
        intervals = cast(list[Any], bounds)
        if intervals and not isinstance(intervals[0], list):
            intervals = [intervals]  # one interval, written plainly
        if not intervals or any(
            not isinstance(i, list) or len(cast(list[Any], i)) != 2 for i in intervals
        ):
            raise KnowledgeError(f"{where}: zone {z} must be [low, high] or a list of them")
        zones[z] = tuple(
            (cast(list[float | None], i)[0], cast(list[float | None], i)[1]) for i in intervals
        )
    if not cast(dict[str, Any], source).get("title"):
        raise KnowledgeError(f"{where}: its source needs a title")
    return Metric(
        id=mid,
        name=_text(where, m, "name"),
        unit=m["unit"],
        direction=m["direction"],
        definition=_text(where, m, "definition"),
        method=_text(where, m, "method"),
        messages=tuple(m.get("messages", ())),
        zones=zones,
        source=cast(dict[str, str], source),
    )


def _finding(file: str, code: str, f: Mapping[str, Any]) -> FindingText:
    where = f"{file}: finding {code}"
    if f.get("stage") not in STAGES:
        raise KnowledgeError(f"{where}: stage must be one of {', '.join(STAGES)}")
    title, action = _text(where, f, "title"), _text(where, f, "action")
    names = {lang: set(PLACEHOLDER.findall(title[lang])) for lang in LANGUAGES}
    if len({frozenset(n) for n in names.values()}) != 1:
        raise KnowledgeError(f"{where}: every language must use the same placeholders")
    return FindingText(code, title, action, f["stage"])


def _options(
    pack: Pack, findings: dict[str, FindingText]
) -> tuple[dict[str, Option], dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    """The Better options catalog, its questions and coverage table. Each option's own
    text becomes the wording of its findings: option.<id>.available and
    option.<id>.hardware, and option.question.<fact> for each question."""
    folder = pack.root / "options"
    options: dict[str, Option] = {}
    questions: dict[str, Mapping[str, Any]] = {}
    coverage: dict[str, Mapping[str, Any]] = {}
    if not folder.is_dir():
        return options, questions, coverage
    shared = json.loads((folder / "coverage.json").read_text(encoding="utf-8"))
    coverage = dict(shared["subsystems"])
    question_action = _text("coverage.json", shared["texts"], "question_action")
    needs = _text("coverage.json", shared["texts"], "needs")
    for path in sorted(folder.glob("*.json")):
        if path.stem == "coverage":
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        for fact, q in doc.get("questions", {}).items():
            where = f"{path.name}: question {fact}"
            text = _text(where, q, "text")
            questions[fact] = q
            findings[f"option.question.{fact}"] = FindingText(
                f"option.question.{fact}", text, question_action, "setup"
            )
        for oid, o in doc.get("options", {}).items():
            where = f"{path.name}: option {oid}"
            if oid in options:
                raise KnowledgeError(f"{where}: defined twice")
            if o.get("cost") not in OPTION_COSTS or o.get("area") not in OPTION_STAGES:
                raise KnowledgeError(
                    f"{where}: needs an area of {sorted(OPTION_STAGES)} "
                    f"and a cost of {OPTION_COSTS}"
                )
            if not o.get("sources"):
                raise KnowledgeError(f"{where}: needs its sources")
            title, hardware = _text(where, o, "title"), _text(where, o, "hardware")
            benefit, how = _text(where, o, "benefit"), _text(where, o, "how")
            options[oid] = Option(
                oid, o["area"], o["subsystem"], title, hardware, benefit, how,
                tuple(o.get("applies", [])), tuple(o.get("when", [])), tuple(o["outcomes"]),
                dict(o["benefit_by_priority"]), o["cost"], tuple(o["sources"]),
            )  # fmt: skip
            stage = OPTION_STAGES[o["area"]]
            findings[f"option.{oid}.available"] = FindingText(
                f"option.{oid}.available",
                {lang: f"{title[lang]}" for lang in LANGUAGES},
                {lang: f"{benefit[lang]} {how[lang]}" for lang in LANGUAGES},
                stage,
            )
            findings[f"option.{oid}.hardware"] = FindingText(
                f"option.{oid}.hardware",
                {lang: f"{title[lang]}: {needs[lang]} {hardware[lang]}" for lang in LANGUAGES},
                {lang: benefit[lang] for lang in LANGUAGES},
                stage,
            )
    for oid, option in options.items():
        if option.subsystem not in coverage or oid not in coverage[option.subsystem]["options"]:
            raise KnowledgeError(
                f"option {oid}: its subsystem {option.subsystem} must list it in coverage.json"
            )
    for sid, sub in coverage.items():
        unknown = [code for code in sub.get("also", ()) if code not in findings]
        if unknown:
            raise KnowledgeError(f"coverage.json: {sid} lists unknown findings {unknown}")
    return options, questions, coverage


def _procedures(
    pack: Pack, findings: Mapping[str, FindingText], metrics: Mapping[str, Metric]
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, str]]]:
    """The procedure templates (knowledge/procedures/*.json), each checked: a finding the
    registry knows, steps of the kinds a template may give with text in every language,
    checks the app can run, and test-flight settings that name a value or bits."""
    folder = pack.root / "procedures"
    procedures: dict[str, Mapping[str, Any]] = {}
    texts: dict[str, Mapping[str, str]] = {}
    if not folder.is_dir():
        return procedures, texts
    for path in sorted(folder.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for key in doc.get("texts", {}):
            texts[key] = _text(f"{path.name}: text {key}", doc["texts"], key)
        shared: Mapping[str, Any] = doc.get("shared", {})
        for code, raw in doc.get("procedures", {}).items():
            where = f"{path.name}: procedure {code}"
            if code not in findings:
                raise KnowledgeError(f"{where}: no such finding")
            if code in procedures:
                raise KnowledgeError(f"{where}: defined twice")
            parts: list[Mapping[str, Any]] = []
            for name in raw.get("use", ()):
                if name not in shared:
                    raise KnowledgeError(f"{where}: no shared part {name}")
                parts.append(shared[name])
            parts.append(raw)
            temporary = [str(n) for part in parts for n in part.get("temporary", ())]
            add: list[dict[str, Any]] = [
                dict(a) for part in parts for a in part.get("add_temporary", ())
            ]
            steps = [
                _procedure_step(where, s, metrics) for part in parts for s in part.get("steps", ())
            ]
            for a in add:
                bits = "set_bits" in a or "clear_bits" in a
                if not isinstance(a.get("param"), str) or bits == ("value" in a):
                    raise KnowledgeError(
                        f"{where}: add_temporary needs a param and set_bits, clear_bits or a value"
                    )
            applied_when = raw.get("applied_when")
            if applied_when is not None and applied_when.get("finding") not in findings:
                raise KnowledgeError(f"{where}: applied_when names no finding the registry knows")
            if raw.get("graded_by", "findings") not in ("findings", "checks"):
                raise KnowledgeError(f"{where}: graded_by is 'findings' or 'checks'")
            # What the work itself may change (AutoTune saves gains): the undo file keeps
            # the flight's values of these too.
            undo_also: object = raw.get("undo_also", [])
            if not isinstance(undo_also, list) or not all(
                isinstance(n, str) and PARAM_NAME.fullmatch(n)
                for n in cast(list[object], undo_also)
            ):
                raise KnowledgeError(f"{where}: undo_also lists parameter names")
            undo_also = [str(n) for n in cast(list[str], undo_also)]
            procedures[code] = {
                "temporary": temporary, "add_temporary": add, "steps": steps,
                "applied_when": applied_when, "graded_by": raw.get("graded_by", "findings"),
                "undo_also": tuple(undo_also),
            }  # fmt: skip
    return procedures, texts


def _procedure_step(
    where: str, s: Mapping[str, Any], metrics: Mapping[str, Metric]
) -> Mapping[str, Any]:
    if s.get("kind") not in PROCEDURE_KINDS:
        raise KnowledgeError(f"{where}: a step's kind must be one of {', '.join(PROCEDURE_KINDS)}")
    text = _text(where, s, "text")
    if len({frozenset(PLACEHOLDER.findall(text[lang])) for lang in LANGUAGES}) != 1:
        raise KnowledgeError(f"{where}: every language must use the same placeholders")
    for check in s.get("checks", ()):
        needs = CHECKS.get(check.get("check"))
        if needs is None or any(k not in check for k in needs):
            raise KnowledgeError(f"{where}: unknown or incomplete check {check}")
        if check["check"] in METRIC_CHECKS and check["metric"] not in metrics:
            raise KnowledgeError(f"{where}: no metric {check['metric']}")
        if check["check"] == "metric_vs_lightest" and ("max_ratio" in check) == (
            "max_above" in check
        ):
            raise KnowledgeError(f"{where}: metric_vs_lightest needs max_ratio or max_above")
        if not isinstance(check.get("required", False), bool):
            raise KnowledgeError(f"{where}: 'required' is true or false")
    when = s.get("when")
    if when is not None and not (isinstance(when.get("param"), str) and when.get("in")):
        raise KnowledgeError(f"{where}: 'when' needs a param and the values it keeps the step for")
    return dict(s)
