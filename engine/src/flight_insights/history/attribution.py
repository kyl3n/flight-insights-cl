"""Change attribution (spec: Parameter history and change attribution): which changes
could have moved a metric, and how strongly the rule pack's map ties them to it.

The map (rules/attribution.json, checked by the registry) is a prior: each parameter
group lists the metrics it should move, the way it moves them and how firmly. Only a
change of a tuning or calibration parameter is a candidate; learned values, counters,
hardware IDs and ground-station settings are context (knowledge/params/classes.json).

A shift is found per airframe, metric, segment and payload group, never a loaded flight
against a light one, at each boundary where changes first apply. Its windows run between
the boundaries that could move that metric, so an unrelated change does not cut every
series short. Every change the shift brackets is ranked by the map's strength, how well
its direction fits, how large it was and how close in time; confounders (a build or a
firmware change with it) lower the confidence. Numbers are in rules/attribution.json.
"""

from __future__ import annotations

import json
import math
import zlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from flight_insights.analyzers.registry import AttributionLink, Registry
from flight_insights.store.db import Store

if TYPE_CHECKING:
    from flight_insights.trends import Flight


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


# --- shifts: when a metric moved, and which changes could have moved it ----------------


@dataclass(frozen=True)
class Change:
    """Something that changed on the airframe, from the first flight it applies to."""

    kind: str  # "parameter", "build" or "firmware"
    name: str  # the parameter (canonical), or what changed
    before: float | str | None
    after: float | str | None
    flight: str  # the first flight it applies to
    cls: str | None = None  # a parameter's class (params/classes.json)
    origin: str | None = None  # a parameter change set's origin


@dataclass(frozen=True)
class Candidate:
    change: Change
    score: float
    group: str | None  # the map's link that ties it to the metric
    factors: Mapping[str, float]  # strength, direction, size, timing


@dataclass(frozen=True)
class Shift:
    metric: str
    segment: str
    group: str  # the payload group it was measured in
    flight: str  # the first flight after
    before: tuple[str, ...]  # the flights it was measured against
    after: tuple[str, ...]
    effect: float  # after minus before, in the metric's unit
    low: float  # its interval
    high: float
    spread: float | None  # the flights' spread before; None for a pair of flights
    verdict: str  # "worse", "better", "moved" (a band metric) or "inconclusive"
    candidates: tuple[Candidate, ...]  # ranked, highest first
    context: tuple[Change, ...]  # changes that are no cause: learned values, counters
    confidence: str  # "high", "medium" or "low"


@dataclass(frozen=True)
class _Point:
    index: int  # in flying order
    flight: str
    value: float
    low: float | None
    high: float | None


def _changes(store: Store, airframe_id: str, order: Mapping[str, int]) -> dict[int, list[Change]]:
    """Every change, at the index of the first flight it applies to."""
    out: dict[int, list[Change]] = {}

    def add(c: Change) -> None:
        if c.flight in order:
            out.setdefault(order[c.flight], []).append(c)

    for after, origin, details in store.conn.execute(
        "SELECT after_flight_id, origin, details_json FROM change_set "
        "WHERE airframe_id = ? AND kind = 'parameter'",
        (airframe_id,),
    ):
        for c in json.loads(details).get("changes", []):
            add(Change("parameter", str(c["name"]), c.get("before"), c.get("after"), after,
                       str(c.get("class") or "tuning"), origin))  # fmt: skip
    for number, reason, first in store.conn.execute(
        "SELECT number, reason, first_flight_id FROM build_version "
        "WHERE airframe_id = ? AND number > 1",
        (airframe_id,),
    ):
        if first is not None:
            add(Change("build", f"build {number}", None, str(reason or ""), first))
    return out


def _firmware(flights: Sequence[tuple[Flight, str]]) -> dict[int, list[Change]]:
    out: dict[int, list[Change]] = {}
    previous: str | None = None
    for i, (f, _) in enumerate(flights):
        if f.firmware and previous and f.firmware != previous:
            out.setdefault(i, []).append(Change("firmware", "firmware", previous, f.firmware, f.id))
        previous = f.firmware or previous
    return out


def _series(
    store: Store, flights: Sequence[tuple[Flight, str]], metric: str | None
) -> dict[tuple[str, str, str], list[_Point]]:
    """(metric, segment, payload group) -> points in flying order."""
    runs = {f.run_id: (i, f.id, g) for i, (f, g) in enumerate(flights) if f.run_id}
    out: dict[tuple[str, str, str], list[_Point]] = {}
    marks = ", ".join("?" for _ in runs)
    query = (
        f"SELECT run_id, metric, segment, value, spread_json FROM metric_value "
        f"WHERE run_id IN ({marks})" + (" AND metric = ?" if metric else "")
    )
    args = [*runs, *([metric] if metric else [])]
    for run, m, seg, value, spread in store.conn.execute(query, args):
        if value is None or not math.isfinite(float(value)):
            continue
        i, fid, group = runs[run]
        s: dict[str, Any] = json.loads(spread) if spread else {}
        low, high = s.get("low"), s.get("high")
        out.setdefault((m, seg, group), []).append(
            _Point(i, fid, float(value), None if low is None else float(low),
                   None if high is None else float(high))
        )  # fmt: skip
    for points in out.values():
        points.sort(key=lambda p: p.index)
    return out


def _relevant(reg: Registry, metric: str, changes: Sequence[Change]) -> bool:
    """Whether a boundary's changes could move this metric: a linked candidate, or a
    build or firmware change."""
    classes = set(reg.rules["attribution"]["ranking"]["candidate_classes"])
    return any(
        c.kind in ("build", "firmware")
        or (c.cls in classes and links_to(reg, c.name, metric) is not None)
        for c in changes
    )


def _verdict(direction: str, effect: float) -> str:
    if direction == "lower":
        return "worse" if effect > 0 else "better"
    if direction == "higher":
        return "worse" if effect < 0 else "better"
    return "moved"


def _test(
    before: Sequence[_Point], after: Sequence[_Point], rules: Mapping[str, Any], seed: int
) -> tuple[str, float, float, float, float | None] | None:
    """("shift" | "inconclusive", effect, low, high, spread), or None for no shift."""
    if len(before) >= int(rules["min_before"]) and len(after) >= int(rules["min_after"]):
        b = np.array([p.value for p in before])
        a = np.array([p.value for p in after])
        effect = float(np.median(a) - np.median(b))
        mad = 1.4826 * float(np.median(np.abs(b - np.median(b))))
        spread = max(mad, float(rules["spread_floor"]) * abs(float(np.median(b))), 1e-12)
        if abs(effect) < float(rules["spread_factor"]) * spread:
            return None
        rng = np.random.default_rng(seed)
        n = int(rules["resamples"])
        diffs = np.median(rng.choice(a, (n, len(a))), axis=1) - np.median(
            rng.choice(b, (n, len(b))), axis=1
        )
        tail = (1.0 - float(rules["level"])) / 2.0 * 100.0
        low, high = (float(x) for x in np.percentile(diffs, [tail, 100.0 - tail]))
        return ("shift" if low > 0 or high < 0 else "inconclusive"), effect, low, high, spread
    if before and after:
        p, q = before[-1], after[0]
        if None in (p.low, p.high, q.low, q.high) or p.value == 0:
            return None
        assert p.low is not None and p.high is not None
        assert q.low is not None and q.high is not None
        effect = q.value - p.value
        if abs(effect) / abs(p.value) < float(rules["min_relative_pair"]):
            return None
        apart = q.low > p.high or q.high < p.low
        return ("shift" if apart else "inconclusive"), effect, q.low - p.high, q.high - p.low, None
    return None


def _number(x: float | str | None) -> float | None:
    return float(x) if isinstance(x, int | float) and math.isfinite(float(x)) else None


def _score(reg: Registry, c: Change, metric: str, effect: float, nearby: bool) -> Candidate:
    rules: Mapping[str, Any] = reg.rules["attribution"]["ranking"]
    timing = float(rules["nearby"]) if nearby else 1.0
    if c.kind != "parameter":
        weight = float(rules[c.kind])
        return Candidate(c, weight * timing, None, {"strength": weight, "timing": timing})
    link = links_to(reg, c.name, metric)
    if link is None:
        weight = float(rules["unlinked"])
        return Candidate(c, weight * timing, None, {"strength": weight, "timing": timing})
    before, after = _number(c.before), _number(c.after)
    if link.sign == 0 or before is None or after is None or after == before:
        direction = float(rules["either"])
    else:
        expected = link.sign * (1 if after > before else -1)
        direction = float(rules["match"] if expected * effect > 0 else rules["opposite"])
    if before is None or after is None:
        size = 1.0  # a parameter that appeared or went: as large as a change gets
    else:
        rel = abs(after - before) / max(abs(before), abs(after), 1e-12)
        size = min(1.0, rel / float(rules["size_saturates"]))
    factors = {"strength": link.weight, "direction": direction, "size": size, "timing": timing}
    return Candidate(c, link.weight * direction * size * timing, link.group.group, factors)


def _confidence(candidates: Sequence[Candidate], rules: Mapping[str, Any]) -> str:
    if not candidates or candidates[0].group is None:
        return "low"
    top = candidates[0]
    others = list(candidates[1:])
    hardware = any(c.change.kind in ("build", "firmware") for c in others)
    second = others[0].score if others else 0.0
    if not hardware and top.score >= float(rules["dominates"]) * second:
        return "high"
    return "medium"


def attribute(
    store: Store, reg: Registry, airframe_id: str, metric: str | None = None
) -> list[Shift]:
    """The airframe's shifts, each with the changes that could have caused it ranked
    (spec: "When performance changes: pointing at the cause"), in flying order."""
    from flight_insights.trends import grouped_flights  # trends imports recommend

    rules: Mapping[str, Any] = reg.rules["attribution"]
    shift_rules: Mapping[str, Any] = rules["shifts"]
    classes = set(rules["ranking"]["candidate_classes"])
    window = int(shift_rules["window"])
    flights = grouped_flights(store, reg, airframe_id)
    order = {f.id: i for i, (f, _) in enumerate(flights)}
    at: dict[int, list[Change]] = _changes(store, airframe_id, order)
    for i, cs in _firmware(flights).items():
        at.setdefault(i, []).extend(cs)
    # A boundary of changes that are all context (learned values, counters) is none.
    bounds = sorted(i for i, cs in at.items() if any(c.kind != "parameter" or c.cls in classes
                                                     for c in cs))  # fmt: skip
    out: list[Shift] = []
    for (m, seg, group), points in sorted(_series(store, flights, metric).items()):
        if m not in reg.metrics:
            continue
        relevant = [i for i in bounds if _relevant(reg, m, at[i])]
        found: list[tuple[bool, float, Shift]] = []
        for b in bounds:
            lo = max((i for i in relevant if i < b), default=-1)
            hi = min((i for i in relevant if i > b), default=len(flights))
            before = [p for p in points if lo <= p.index < b][-window:]
            after = [p for p in points if b <= p.index < hi][:window]
            seed = int(shift_rules["seed"]) + zlib.crc32(f"{m}|{seg}|{group}|{b}".encode())
            tested = _test(before, after, shift_rules, seed)
            if tested is None:
                continue
            kind, effect, low, high, spread = tested
            first, last = after[0].index, before[-1].index if before else -1
            bracketed = [(i, c) for i in bounds if last < i <= first for c in at[i]]
            cands = sorted(
                (_score(reg, c, m, effect, i != b) for i, c in bracketed
                 if c.kind != "parameter" or c.cls in classes),
                key=lambda k: -k.score,
            )  # fmt: skip
            context = tuple(
                c for _, c in bracketed if c.kind == "parameter" and c.cls not in classes
            )
            verdict = "inconclusive" if kind == "inconclusive" else _verdict(
                reg.metrics[m].direction, effect
            )  # fmt: skip
            shift = Shift(
                m, seg, group, after[0].flight, tuple(p.flight for p in before),
                tuple(p.flight for p in after), effect, low, high, spread, verdict,
                tuple(cands), context, _confidence(cands, rules["ranking"]),
            )  # fmt: skip
            z = abs(effect) / spread if spread else abs(effect) / max(high - low, 1e-12)
            found.append((b in relevant, z, shift))
        # Windows that overlap measure the same move once: the better-linked, larger one.
        kept: list[Shift] = []
        spans: list[tuple[int, int]] = []
        for _, _, s in sorted(found, key=lambda x: (not x[0], -x[1])):
            span = (order[s.before[0]] if s.before else order[s.flight], order[s.after[-1]])
            if any(span[0] <= e and a <= span[1] for a, e in spans):
                continue
            kept.append(s)
            spans.append(span)
        out.extend(kept)
    out.sort(key=lambda s: (order[s.flight], s.metric, s.segment, s.group))
    return out


def shift_json(s: Shift, reg: Registry) -> dict[str, Any]:
    """A shift as the app shows it: the move, its interval, and the ranked changes with
    the map's reason for each."""
    by_group = {g.group: g for g in reg.attribution}

    def change(c: Change) -> dict[str, Any]:
        return {"kind": c.kind, "name": c.name, "before": c.before, "after": c.after,
                "flight": c.flight, "class": c.cls, "origin": c.origin}  # fmt: skip

    metric = reg.metrics[s.metric]
    return {
        "metric": s.metric,
        "name": dict(metric.name),
        "unit": metric.unit,
        "segment": s.segment,
        "group": s.group,
        "flight": s.flight,
        "before": list(s.before),
        "after": list(s.after),
        "effect": s.effect,
        "low": s.low,
        "high": s.high,
        "spread": s.spread,
        "verdict": s.verdict,
        "confidence": s.confidence,
        "candidates": [
            change(c.change)
            | {
                "score": round(c.score, 3),
                "link": c.group,
                "why": dict(by_group[c.group].why) if c.group in by_group else None,
                "factors": {k: round(v, 3) for k, v in c.factors.items()},
            }
            for c in s.candidates
        ],
        "context": [change(c) for c in s.context],
    }
