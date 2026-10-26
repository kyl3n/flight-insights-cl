"""Change attribution (spec: Parameter history and change attribution): which changes
could have moved a metric, and how strongly the rule pack's map ties them to it.

The map (rules/attribution.json, checked by the registry) is a prior: each parameter
group lists the metrics it should move, the way it moves them and how firmly. Only a
change of a tuning parameter is a candidate; learned values, counters, hardware IDs and
ground-station settings are context (knowledge/params/classes.json).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from flight_insights.analyzers.registry import AttributionLink, Registry


@dataclass(frozen=True)
class Link:
    """One parameter tied to one metric: +1 when the metric moves with the parameter's
    value, -1 against it, 0 either way; `weight` is how firmly the map holds it."""

    param: str
    metric: str
    sign: int
    weight: float
    group: AttributionLink


def links_for(reg: Registry, param: str) -> Iterator[Link]:
    """The metrics a change of this (canonical) parameter should move."""
    axes: dict[str, str] = reg.rules["attribution"]["axes"]
    for group in reg.attribution:
        for pattern in group.params:
            m = pattern.fullmatch(param)
            if m is None:
                continue
            axis = axes.get(m.group("axis")) if "axis" in pattern.groupindex else None
            for name, sign, weight in group.metrics:
                metric = name.format(axis=axis) if axis is not None else name
                if metric in reg.metrics:
                    yield Link(param, metric, sign, weight, group)
            break


def links_to(reg: Registry, param: str, metric: str) -> Link | None:
    """The strongest link from this parameter to this metric, if the map has one."""
    found = [k for k in links_for(reg, param) if k.metric == metric]
    return max(found, key=lambda k: k.weight) if found else None
