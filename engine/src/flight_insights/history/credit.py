"""Credit for improvements (spec: "When performance changes: pointing at the cause",
step 7, "Credit improvements the same way, so a change that helped is confirmed and
kept, not just remembered").

A shift that made a metric better, at medium or high confidence, credits its
top-ranked parameter change when the map, or the platform's fleet, ties it to the
metric. The credit then follows the change: it is kept while every later flight still
has its value after, and the first later flight with any other value undoes it. That
flight is named, because putting back a change that helped is where a regression
starts.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from flight_insights.analyzers.registry import Registry
from flight_insights.history.attribution import Candidate, Shift, attribute
from flight_insights.history.persist import load_snapshot
from flight_insights.history.snapshot import same_value
from flight_insights.knowledge import Pack
from flight_insights.store.db import Store
from flight_insights.trends import grouped_flights


@dataclass(frozen=True)
class Credit:
    shift: Shift
    candidate: Candidate
    undone_on: str | None  # the first later flight without the change; None: kept
    value_then: float | None  # what that flight had instead


def _params(store: Store, pack: Pack, flight_id: str) -> Mapping[str, float] | None:
    row = store.get("flight", flight_id)
    if row is None or not row["param_snapshot_id"]:
        return None
    snapshot = load_snapshot(store, row["param_snapshot_id"])
    return snapshot.dialect(pack).to_canonical(snapshot.values).values


def credits(store: Store, pack: Pack, reg: Registry, airframe_id: str) -> list[Credit]:
    """The changes the airframe's better shifts credit, in flying order, each with
    whether it is still in place."""
    classes = set(reg.rules["attribution"]["ranking"]["candidate_classes"])
    order = {f.id: i for i, (f, _) in enumerate(grouped_flights(store, reg, airframe_id))}
    later_params: dict[str, Mapping[str, float] | None] = {}
    out: list[Credit] = []
    for s in attribute(store, reg, airframe_id):
        if s.verdict != "better" or s.confidence not in ("high", "medium") or not s.candidates:
            continue
        top = s.candidates[0]
        c = top.change
        after = float(c.after) if isinstance(c.after, int | float) else math.nan
        if c.kind != "parameter" or c.cls not in classes or top.group is None:
            continue
        if not math.isfinite(after):
            continue
        undone: tuple[str, float | None] | None = None
        for fid in sorted((f for f in order if order[f] > order[s.flight]), key=order.__getitem__):
            if fid not in later_params:
                later_params[fid] = _params(store, pack, fid)
            p = later_params[fid]
            if p is None:
                continue
            now = p.get(c.name)
            if now is None or not same_value(now, after):
                undone = (fid, now)
                break
        out.append(Credit(s, top, *(undone or (None, None))))
    return out


def credit_json(c: Credit, reg: Registry) -> dict[str, Any]:
    """A credit as the app shows it."""
    metric = reg.metrics[c.shift.metric]
    change = c.candidate.change
    return {
        "metric": c.shift.metric, "name": dict(metric.name), "unit": metric.unit,
        "segment": c.shift.segment, "group": c.shift.group, "flight": c.shift.flight,
        "effect": c.shift.effect, "confidence": c.shift.confidence,
        "param": change.name, "before": change.before, "after": change.after,
        "link": c.candidate.group, "kept": c.undone_on is None,
        "undone_on": c.undone_on, "value_then": c.value_then,
    }  # fmt: skip
