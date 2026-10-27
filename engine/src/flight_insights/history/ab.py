"""The in-flight A/B split (spec: Parameter history and change attribution, "Prefer
in-flight A/B"): a parameter changed while the aircraft flew splits the flight there,
and both sides are compared under the same conditions, the same aircraft, payload,
battery and air.

- **The moments.** The parameter changes the log recorded while armed (a ground
  station's writes, logged as PARM), in canonical names. Changes within `group_s` of
  each other are one moment, as a person setting several values at once.
- **The sides.** Before: from the last moment that could move the same metrics (or
  arming) to this one's first change. After: from `settle_s` past this one's last change
  to the next such moment (or disarming). A change that moves none of them, such as the
  tune check's own logging switched on, does not cut a side short, as between flights.
  A side shorter than `min_side_s` is not compared.
- **The measurements.** The analyzers that can work on part of a flight (`spans`) and
  emit a metric the map ties to one of the moment's changes measure each side, with the
  parameters as they stood just before and just after the moment. A moment none of them
  can measure is not split.
- **The test.** A metric measured with an interval on each side (the tune check's)
  compares the two as a pair, as a shift between two flights does. Any other is measured
  on blocks of `block_s` across each side and tested as a shift over flights is, with
  the blocks for flights.
- **The causes.** The moment's changes, ranked by the map as a shift's are. There is no
  build, firmware or weather between the sides to confound them.

Numbers are in rules/attribution.json "in_flight", beside the shifts' own.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import Analyzer, MetricValue
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.run import ANALYZERS
from flight_insights.history.attribution import (
    Candidate,
    Change,
    Point,
    compare_points,
    links_for,
    ranking_confidence,
    score_change,
    verdict_for,
)
from flight_insights.history.persist import load_snapshot
from flight_insights.knowledge import Pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Comparison:
    metric: str
    segment: str
    method: str  # "intervals": each side's own; "blocks": the sides cut into blocks
    before: float  # the side's value; with blocks, the median of its blocks
    after: float
    effect: float  # after minus before
    low: float  # its interval
    high: float
    verdict: str  # "worse", "better", "moved" (a band metric) or "inconclusive"
    candidates: tuple[Candidate, ...]  # the moment's changes, ranked
    confidence: str


@dataclass(frozen=True)
class Split:
    flight: str
    at_us: int  # the moment's first change, log time
    changes: tuple[Change, ...]  # the candidates: tuning and calibration parameters
    context: tuple[Change, ...]  # learned values and counters changed with them
    before: tuple[int, int] | None  # each side, log time; None when too short
    after: tuple[int, int] | None
    results: tuple[Comparison, ...]
    skipped: str | None = None  # why nothing was compared


@dataclass(frozen=True)
class _Moment:
    first_us: int  # its first and last change that moves something a side can show
    last_us: int
    changes: tuple[Change, ...]  # those, and any other change within group_s of them
    moves: frozenset[str]  # the metrics its changes move that part of a flight can show
    before: Mapping[str, float]  # the canonical set just before it
    after: Mapping[str, float]  # and just after it


def _moments(
    store: Store, pack: Pack, flight: FlightData, group_us: int,
    moves: Callable[[Change], frozenset[str]],
) -> list[_Moment]:  # fmt: skip
    """The flight's parameter changes while armed that move something part of a flight
    can show, grouped into moments. A change that moves none of it (the tune check's own
    logging switched on) neither makes a moment nor stretches one; within group_s of one
    it is listed with it."""
    row = store.get("flight", flight.id)
    if row is None or not row["param_snapshot_id"]:
        return []
    snapshot = load_snapshot(store, row["param_snapshot_id"])
    dialect = snapshot.dialect(pack)
    values = dict(snapshot.values)
    start = dict(flight.params)
    timed: list[tuple[int, Change, Mapping[str, float]]] = []  # each with the set after it
    for c in sorted(snapshot.changes, key=lambda c: c.time_us):
        if not flight.arm_us < c.time_us < flight.disarm_us:
            values[c.name] = c.new
            if c.time_us <= flight.arm_us:
                start = dialect.to_canonical(values).values
            continue
        name, before = dialect.canonical(c.name, c.old)
        _, after = dialect.canonical(c.name, c.new)
        cls = dialect.param_class(c.name, values)
        values[c.name] = c.new
        change = Change("parameter", name, before, after, flight.id, cls, "in flight")
        timed.append((c.time_us, change, dialect.to_canonical(values).values))
    groups: list[list[int]] = []  # indices into timed
    for k, (t, c, _) in enumerate(timed):
        if not moves(c):
            continue
        if groups and t - timed[groups[-1][-1]][0] <= group_us:
            groups[-1].append(k)
        else:
            groups.append([k])
    out: list[_Moment] = []
    for g in groups:
        first, last = timed[g[0]][0], timed[g[-1]][0]
        near = tuple(c for t, c, _ in timed if first - group_us <= t <= last + group_us)
        before = timed[g[0] - 1][2] if g[0] else start
        moved = frozenset[str]().union(*(moves(timed[k][1]) for k in g))
        out.append(_Moment(first, last, near, moved, before, timed[g[-1]][2]))
    return out


def _measure(
    analyzers: Sequence[Analyzer], flight: FlightData, reg: Registry, span: tuple[int, int],
    params: Mapping[str, float], metrics: set[str],
) -> dict[tuple[str, str], MetricValue]:  # fmt: skip
    """The linked metrics the span-aware analyzers measure on one part of the flight."""
    side = dataclasses.replace(flight, span=span, params=params)
    out: dict[tuple[str, str], MetricValue] = {}
    for a in analyzers:
        try:
            r = a.run(side, reg)
        except Exception:  # one analyzer failing leaves the others' measurements standing
            log.exception("analyzer %s failed on part of flight %s", a.name, flight.id)
            continue
        for m in r.metrics:
            if m.metric in metrics and math.isfinite(m.value):
                out[(m.metric, m.segment)] = m
    return out


def _blocks(span: tuple[int, int], block_us: int) -> list[tuple[int, int]]:
    n = (span[1] - span[0]) // block_us
    return [(span[0] + k * block_us, span[0] + (k + 1) * block_us) for k in range(n)]


def _point(index: int, flight: str, m: MetricValue) -> Point:
    low, high = m.spread.get("low"), m.spread.get("high")
    return Point(index, flight, float(m.value), None if low is None else float(low),
                  None if high is None else float(high))  # fmt: skip


def split(store: Store, cache: ColumnCache, pack: Pack, flight_id: str) -> list[Split]:
    """Each in-flight parameter change of the flight, with both sides compared on the
    metrics the map ties to it, in flying order."""
    reg = Registry.load(pack)
    rules: Mapping[str, Any] = reg.rules["attribution"]
    ab: Mapping[str, Any] = rules["in_flight"]
    shift_rules: Mapping[str, Any] = rules["shifts"]
    classes = set(rules["ranking"]["candidate_classes"])
    flight = FlightData.load(store, cache, pack, flight_id)
    if not flight.logged:
        return []
    measured = {name for a in ANALYZERS if a.spans for name in a.metrics}

    def moves(c: Change) -> frozenset[str]:
        if c.cls not in classes:
            return frozenset()
        return frozenset(k.metric for k in links_for(reg, c.name)) & measured

    moments = _moments(store, pack, flight, int(float(ab["group_s"]) * 1e6), moves)
    settle, least = int(float(ab["settle_s"]) * 1e6), int(float(ab["min_side_s"]) * 1e6)
    block = int(float(ab["block_s"]) * 1e6)
    out: list[Split] = []
    for k, m in enumerate(moments):
        linked = set(m.moves)
        # Each side runs to the next moment that moves the same metrics, as between
        # flights a change that cannot move a metric does not cut its series short.
        prev = max((j for j in range(k) if moments[j].moves & linked), default=None)
        nxt = min((j for j in range(k + 1, len(moments)) if moments[j].moves & linked),
                  default=None)  # fmt: skip
        start = flight.arm_us if prev is None else moments[prev].last_us + settle
        end = flight.disarm_us if nxt is None else moments[nxt].first_us
        before = (start, m.first_us) if m.first_us - start >= least else None
        after = (m.last_us + settle, end) if end - (m.last_us + settle) >= least else None
        candidates = tuple(c for c in m.changes if c.cls in classes)
        context = tuple(c for c in m.changes if c.cls not in classes)
        if before is None or after is None:
            short = "before" if before is None else "after"
            out.append(Split(flight_id, m.first_us, candidates, context, before, after, (),
                             f"the side {short} the change is shorter than "
                             f"{float(ab['min_side_s']):g} s"))  # fmt: skip
            continue
        used = [a for a in ANALYZERS if a.spans and linked & set(a.metrics)]
        params_before, params_after = m.before, m.after
        sides = (_measure(used, flight, reg, before, params_before, linked),
                 _measure(used, flight, reg, after, params_after, linked))  # fmt: skip
        results: list[Comparison] = []
        blocks: tuple[list[dict[tuple[str, str], MetricValue]], ...] | None = None
        for key in sorted(sides[0].keys() & sides[1].keys()):
            metric, segment = key
            if metric not in reg.metrics:
                continue
            p, q = _point(0, flight_id, sides[0][key]), _point(1, flight_id, sides[1][key])
            seed = int(shift_rules["seed"]) + zlib.crc32(
                f"{metric}|{segment}|{m.first_us}".encode()
            )
            if None not in (p.low, p.high, q.low, q.high):
                method, b_points, a_points = "intervals", [p], [q]
            else:
                if blocks is None:
                    blocks = (
                        [_measure(used, flight, reg, s, params_before, linked)
                         for s in _blocks(before, block)],
                        [_measure(used, flight, reg, s, params_after, linked)
                         for s in _blocks(after, block)],
                    )  # fmt: skip
                b_points = [_point(i, flight_id, got[key])
                            for i, got in enumerate(blocks[0]) if key in got]  # fmt: skip
                a_points = [_point(i, flight_id, got[key])
                            for i, got in enumerate(blocks[1]) if key in got]  # fmt: skip
                # Blocks are compared on their values alone, as flights are.
                b_points = [dataclasses.replace(x, low=None, high=None) for x in b_points]
                a_points = [dataclasses.replace(x, low=None, high=None) for x in a_points]
                method = "blocks"
            tested = compare_points(b_points, a_points, shift_rules, seed)
            if tested is None:
                continue
            kind, effect, low, high, _ = tested
            verdict = "inconclusive" if kind == "inconclusive" else verdict_for(
                reg.metrics[metric].direction, effect
            )  # fmt: skip
            ranked = tuple(sorted((score_change(reg, c, metric, effect, False) for c in candidates),
                                  key=lambda c: -c.score))  # fmt: skip
            value_b = sides[0][key].value if method == "intervals" else _median(b_points)
            value_a = sides[1][key].value if method == "intervals" else _median(a_points)
            results.append(Comparison(metric, segment, method, value_b, value_a, effect, low,
                                      high, verdict, ranked,
                                      ranking_confidence(ranked, rules["ranking"])))  # fmt: skip
        out.append(Split(flight_id, m.first_us, candidates, context, before, after,
                         tuple(results)))  # fmt: skip
    return out


def _median(points: Sequence[Point]) -> float:
    values = sorted(p.value for p in points)
    n = len(values)
    return values[n // 2] if n % 2 else 0.5 * (values[n // 2 - 1] + values[n // 2])


def split_json(s: Split, reg: Registry, arm_us: int) -> dict[str, Any]:
    """A split as the app shows it, with its times in seconds after arming."""
    by_group = {g.group: g for g in reg.attribution}

    def secs(t: int) -> float:
        return round((t - arm_us) / 1e6, 2)

    def change(c: Change) -> dict[str, Any]:
        return {"name": c.name, "before": c.before, "after": c.after, "class": c.cls}

    def side(span: tuple[int, int] | None) -> list[float] | None:
        return None if span is None else [secs(span[0]), secs(span[1])]

    return {
        "at_s": secs(s.at_us),
        "changes": [change(c) for c in s.changes],
        "context": [change(c) for c in s.context],
        "before_s": side(s.before),
        "after_s": side(s.after),
        "skipped": s.skipped,
        "results": [
            {
                "metric": r.metric,
                "name": dict(reg.metrics[r.metric].name),
                "unit": reg.metrics[r.metric].unit,
                "segment": r.segment,
                "method": r.method,
                "before": r.before,
                "after": r.after,
                "effect": r.effect,
                "low": r.low,
                "high": r.high,
                "verdict": r.verdict,
                "confidence": r.confidence,
                "candidates": [
                    change(c.change)
                    | {
                        "score": round(c.score, 3),
                        "link": c.group,
                        "why": dict(by_group[c.group].why) if c.group in by_group else None,
                        "factors": {k: round(v, 3) for k, v in c.factors.items()},
                    }
                    for c in r.candidates
                ],
            }
            for r in s.results
        ],
    }
