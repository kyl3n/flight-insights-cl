"""The metric registry (spec: Explanations for every metric and data point).

Every metric the app computes, and every finding it can raise, is described in
knowledge/metrics/*.json: what it is, how it is measured, its unit and direction,
its zones and where each zone comes from, in English and Ukrainian. Engineers write
it; the engine refuses to load an entry with any of that missing, and a test fails
if an analyzer emits a metric or finding the registry does not describe, so nothing
the app shows goes unexplained.
"""

from __future__ import annotations

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
    zones: Mapping[Zone, tuple[float | None, float | None]]  # [low, high), None unbounded
    source: Mapping[str, str]

    def zone(self, value: float) -> Zone | None:
        """The zone `value` falls in; None for a value no zone covers (NaN)."""
        if math.isnan(value):
            return None
        for z in ZONES:
            if z not in self.zones:
                continue
            low, high = self.zones[z]
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
class Registry:
    """What the knowledge pack tells the analyzers: the metric and finding registry,
    the curated rules (knowledge/rules/*.json, by file stem), and each release's names
    for the numbers logs record."""

    metrics: Mapping[str, Metric]
    findings: Mapping[str, FindingText]
    version: str  # changes whenever any registry, rule or name file changes
    rules: Mapping[str, Any]
    pack: Pack

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
        return Registry(metrics, findings, knowledge_version(pack), rules, pack)


def knowledge_version(pack: Pack) -> str:
    """A short hash of every file an analysis reads from the pack: the registry, the
    rules and the names. A result made with another version is recomputed, not reused."""
    h = hashlib.sha256()
    for folder in ("metrics", "rules", "enums"):
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
    raw_zones = m.get("zones")
    if not isinstance(raw_zones, dict) or "target" not in raw_zones:
        raise KnowledgeError(f"{where}: needs zones, with a target zone at least")
    zones: dict[Zone, tuple[float | None, float | None]] = {}
    for z, bounds in cast(dict[str, Any], raw_zones).items():
        if z not in ZONES or not isinstance(bounds, list) or len(cast(list[Any], bounds)) != 2:
            raise KnowledgeError(f"{where}: zone {z} must be one of {ZONES} as [low, high]")
        low, high = cast(list[float | None], bounds)
        zones[z] = (low, high)
    source = m.get("source")
    if not isinstance(source, dict) or cast(dict[str, Any], source).get("kind") not in SOURCE_KINDS:
        raise KnowledgeError(f"{where}: needs a source of kind {', '.join(SOURCE_KINDS)}")
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
