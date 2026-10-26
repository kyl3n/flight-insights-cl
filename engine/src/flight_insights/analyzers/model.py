"""What analyzers produce (spec: Analyzers common to all vehicles).

An analyzer is a pure function from a flight's decoded data, parameters and
fingerprint to findings and metric values. It never writes parameters: a finding
may carry candidate changes, which the recommendation engine merges and orders.

Every finding carries the evidence it rests on, and its wording lives in the
knowledge pack's registry, so the app shows the same words in every language and
nothing goes unexplained.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

if TYPE_CHECKING:
    from flight_insights.analyzers.flight import FlightData
    from flight_insights.analyzers.registry import Registry

Severity = Literal["info", "caution", "out", "safety"]
Confidence = Literal["low", "medium", "high"]
Zone = Literal["target", "caution", "out"]

SEVERITY_OF_ZONE: Mapping[Zone, Severity] = {"target": "info", "caution": "caution", "out": "out"}


@dataclass(frozen=True)
class Segment:
    """A stretch of the flight a value was measured over, in log time."""

    kind: str  # 'flight', 'hover' ...
    start_us: int
    end_us: int


@dataclass(frozen=True)
class Evidence:
    """One thing a finding rests on: a measured value, and where it was measured."""

    id: str  # unique within its run, e.g. 'vibe:hover:1'
    kind: Literal["metric", "series", "parameter", "fact"]
    summary: str  # one line, plain language, English; the UI shows the registry's wording
    messages: tuple[str, ...] = ()  # log messages it was read from
    fields: tuple[str, ...] = ()
    segment: Segment | None = None
    values: Mapping[str, Any] = field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        # Values are scalars, or short lists of them: never an array or a local track,
        # which could carry where the aircraft flew into a result.
        for key, value in self.values.items():
            kind = str(type(value).__name__)  # pyright: ignore[reportUnknownArgumentType]
            if isinstance(value, np.ndarray) or kind == "LocalTrack":
                raise TypeError(f"evidence {self.id}: {key} must be a scalar, not {kind}")


@dataclass(frozen=True)
class MetricValue:
    metric: str  # id in the metric registry
    segment: str
    value: float
    zone: Zone | None
    evidence_id: str
    spread: Mapping[str, float] = field(default_factory=dict[str, float])


@dataclass(frozen=True)
class CandidateChange:
    """A parameter change a finding suggests. Validated and staged later, never applied."""

    param: str  # canonical name
    current: float | None
    proposed: float | None
    stage: str


@dataclass(frozen=True)
class Finding:
    code: str  # id in the registry's findings: its wording and what it means
    severity: Severity
    confidence: Confidence
    evidence: tuple[Evidence, ...]
    params: Mapping[str, Any] = field(default_factory=dict[str, Any])
    changes: tuple[CandidateChange, ...] = ()

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ValueError(f"{self.code}: a finding needs the evidence it rests on")


@dataclass
class Result:
    """One analyzer's output for one flight. `skipped` says why it could not judge,
    for example a message type the log does not hold."""

    findings: list[Finding] = field(default_factory=list[Finding])
    metrics: list[MetricValue] = field(default_factory=list[MetricValue])
    evidence: list[Evidence] = field(default_factory=list[Evidence])  # what metrics cite
    skipped: str | None = None


@dataclass(frozen=True)
class Analyzer:
    name: str
    version: int  # raised whenever its output for the same input can change
    run: Callable[[FlightData, Registry], Result]
    metrics: tuple[str, ...]  # every metric and finding it can emit, all in the registry
    findings: tuple[str, ...]
