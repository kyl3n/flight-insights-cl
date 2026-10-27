"""The test that settles a shift (spec: "When performance changes: pointing at the
cause", steps 5 to 7): put the top candidate back on its own, fly the same profile, and
see whether the metric comes back.

- **The steps.** A shift that made a metric worse has its parameter candidates ranked
  (history/attribution.py). Each becomes one single-change test flight, most likely
  first: a file that puts that one parameter back as it was, with every other change
  left in place. A build or firmware change cannot be put back by a file; it is listed
  for a person, beside the steps.
- **Which flight tested which change.** No state is kept: the airframe's later flights
  say it. A flight in the shift's payload group, with the candidate back at its value
  before and every other candidate still at its value after, is that candidate's test.
- **Reading the test.** Against the shift's own flights, with its spread before (twice
  it, as the shifts' test): back within it of the flights before and outside it of the
  flights after is *explained*; still within it of the after and outside it of the
  before is *not this one*; anything else is *unclear*. A metric with an interval on
  each flight (the tune check's) reads the test flight's interval against the last
  flight before and the first after instead: apart from one and touching the other.
- **Bisection.** Once a step explains the shift, the steps after it are not needed. A
  step that is not the cause sends the next file; its undo file puts the parameter back
  as it is now before the next test.

Nothing here talks to an aircraft: the files are loaded by a person.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.analyzers.registry import Registry
from flight_insights.history.attribution import Candidate, Change, Shift
from flight_insights.history.persist import load_snapshot
from flight_insights.history.snapshot import same_value
from flight_insights.knowledge import Pack
from flight_insights.params.files import (
    Kind,
    format_value,
    write_mission_planner,
    write_qgroundcontrol,
)
from flight_insights.recommend.plan import render
from flight_insights.store.db import Store
from flight_insights.trends import grouped_flights, metric_of


@dataclass(frozen=True)
class Step:
    order: int  # 1 is the most likely
    candidate: Candidate
    state: str  # "to fly", "explained", "not this one", "unclear", "not needed"
    flight: str | None = None  # the flight that tested it
    value: float | None = None  # the metric on that flight


@dataclass(frozen=True)
class RevertTest:
    shift: Shift
    steps: tuple[Step, ...]
    by_hand: tuple[Change, ...]  # build and firmware changes: no file puts them back
    before: float  # the metric's median over the flights before the shift, and after
    after: float
    band: float | None  # what counts as back: twice the spread before; None: intervals
    explained_by: str | None  # the parameter whose test explained the shift
    next_step: int | None  # the step to fly next, if any


class RevertError(ValueError):
    """No revert test can be made for that shift."""


def _params(store: Store, pack: Pack, flight_id: str) -> Mapping[str, float] | None:
    row = store.get("flight", flight_id)
    if row is None or not row["param_snapshot_id"]:
        return None
    snapshot = load_snapshot(store, row["param_snapshot_id"])
    return snapshot.dialect(pack).to_canonical(snapshot.values).values


def _number(x: float | str | None) -> float | None:
    return float(x) if isinstance(x, int | float) and math.isfinite(float(x)) else None


def _judge(
    value: float, interval: tuple[float, float] | None, before: float, after: float,
    band: float | None, first: tuple[float, float] | None, last: tuple[float, float] | None,
) -> str:  # fmt: skip
    """Explained, not this one, or unclear: back with the flights before, still with the
    flights after, or neither."""
    if band is not None:
        back = abs(value - before) <= band
        still = abs(value - after) <= band
    else:  # intervals: the test flight's against the last flight before, the first after
        if interval is None or first is None or last is None:
            return "unclear"

        def touches(a: tuple[float, float], b: tuple[float, float]) -> bool:
            return a[0] <= b[1] and b[0] <= a[1]

        back, still = touches(interval, last), touches(interval, first)
    if back and not still:
        return "explained"
    if still and not back:
        return "not this one"
    return "unclear"


def revert_test(
    store: Store, pack: Pack, reg: Registry, airframe_id: str, shift: Shift
) -> RevertTest:
    """The single-change test flights that settle a shift, most likely first, each with
    what the airframe's later flights already showed."""
    if shift.verdict != "worse":
        raise RevertError(
            "only a shift that made a metric worse is tested by putting a change back"
        )
    classes = set(reg.rules["attribution"]["ranking"]["candidate_classes"])
    params = [c for c in shift.candidates if c.change.kind == "parameter"
              and c.change.cls in classes and _number(c.change.before) is not None
              and _number(c.change.after) is not None]  # fmt: skip
    by_hand = tuple(c.change for c in shift.candidates if c.change.kind in ("build", "firmware"))
    if not params:
        raise RevertError("the shift has no parameter change a file can put back")
    flights = grouped_flights(store, reg, airframe_id)
    order = {f.id: i for i, (f, _) in enumerate(flights)}
    runs = {f.id: f.run_id for f, _ in flights}

    def measured(fid: str) -> tuple[float, tuple[float, float] | None] | None:
        run = runs.get(fid)
        got = metric_of(store, run, shift.metric, shift.segment) if run else None
        if got is None:
            return None
        low, high = got[1].get("low"), got[1].get("high")
        return got[0], (None if low is None or high is None else (float(low), float(high)))

    sides = [[m for f in side if (m := measured(f)) is not None]
             for side in (shift.before, shift.after)]  # fmt: skip
    if not sides[0] or not sides[1]:
        raise RevertError("the shift's flights no longer carry the metric")
    before = float(np.median([v for v, _ in sides[0]]))
    after = float(np.median([v for v, _ in sides[1]]))
    factor = float(reg.rules["attribution"]["shifts"]["spread_factor"])
    band = None if shift.spread is None else factor * shift.spread
    last, first = sides[0][-1][1], sides[1][0][1]
    # The later flights in the shift's group, in flying order.
    later = [(f, g) for f, g in flights if order[f.id] > order[shift.flight] and g == shift.group]
    flown: dict[str, tuple[str, float, tuple[float, float] | None]] = {}
    for f, _ in later:
        p = _params(store, pack, f.id)
        if p is None:
            continue
        back = [c for c in params if _same(p.get(c.change.name), _number(c.change.before))]
        if len(back) != 1 or back[0].change.name in flown:
            continue
        kept = all(_same(p.get(c.change.name), _number(c.change.after))
                   for c in params if c is not back[0])  # fmt: skip
        got = measured(f.id) if kept else None
        if got is not None:
            flown[back[0].change.name] = (f.id, got[0], got[1])
    steps: list[Step] = []
    explained: str | None = None
    next_step: int | None = None
    for k, c in enumerate(params, start=1):
        name = c.change.name
        if explained is not None:
            steps.append(Step(k, c, "not needed"))
            continue
        if name not in flown:
            steps.append(Step(k, c, "to fly"))
            next_step = k if next_step is None else next_step
            continue
        fid, value, interval = flown[name]
        state = _judge(value, interval, before, after, band, first, last)
        steps.append(Step(k, c, state, fid, value))
        if state == "explained":
            explained = name
            next_step = None
    return RevertTest(shift, tuple(steps), by_hand, before, after, band, explained, next_step)


def _same(value: float | None, target: float | None) -> bool:
    return value is not None and target is not None and same_value(value, target)


def files(
    test: RevertTest, step: int, store: Store, pack: Pack, reg: Registry, kind: Kind,
    lang: str = "en",
) -> list[dict[str, str]]:  # fmt: skip
    """The step's revert file and its undo file, in the firmware the airframe flew last,
    each with its card in the header."""
    s = next((x for x in test.steps if x.order == step), None)
    if s is None:
        raise RevertError(f"the revert test has no step {step}")
    change = s.candidate.change
    shift = test.shift
    last = shift.after[-1]
    row = store.get("flight", last)
    assert row is not None and row["param_snapshot_id"]
    dialect = load_snapshot(store, row["param_snapshot_id"]).dialect(pack)
    before, after = _number(change.before), _number(change.after)
    assert before is not None and after is not None
    native, back = dialect.native(change.name, before)
    _, now = dialect.native(change.name, after)
    back, now = before if back is None else back, after if now is None else now
    texts: Mapping[str, Mapping[str, str]] = reg.rules["attribution"]["revert_test"]["texts"]
    metric = reg.metrics[shift.metric]
    values = {
        "param": native, "before": format_value(back), "after": format_value(now),
        "metric": metric.name[lang], "flight": shift.flight, "segment": shift.segment,
        "was": f"{test.before:.3g}", "is": f"{test.after:.3g}", "unit": metric.unit,
        "step": step, "steps": len(test.steps),
    }  # fmt: skip

    def text(key: str) -> str:
        return render(texts[key][lang], values)

    out: list[dict[str, str]] = []
    for suffix, value, keys in (
        ("revert", back, ("header", "fly", "verify")),
        ("undo", now, ("undo_header",)),
    ):
        header = [f"Flight Insights: revert test {step} of {len(test.steps)}",
                  *(text(k) for k in keys)]  # fmt: skip
        stem = f"flight-insights-revert-test-{shift.flight}-{step}-{suffix}"
        comments = {native: text("comment")}
        if kind == "mission planner":
            body = write_mission_planner({native: value}, comments, header)
            out.append({"filename": f"{stem}.param", "text": body})
        else:
            body = write_qgroundcontrol({native: value}, comments=comments, notes=header)
            out.append({"filename": f"{stem}.params", "text": body})
    return out


def revert_json(test: RevertTest, reg: Registry) -> dict[str, Any]:
    """A revert test as the app shows it."""

    def change(c: Change) -> dict[str, Any]:
        return {"kind": c.kind, "name": c.name, "before": c.before, "after": c.after,
                "class": c.cls}  # fmt: skip

    return {
        "metric": test.shift.metric,
        "name": dict(reg.metrics[test.shift.metric].name),
        "unit": reg.metrics[test.shift.metric].unit,
        "segment": test.shift.segment,
        "group": test.shift.group,
        "flight": test.shift.flight,
        "before": test.before,
        "after": test.after,
        "band": test.band,
        "explained_by": test.explained_by,
        "next_step": test.next_step,
        "steps": [
            change(s.candidate.change)
            | {
                "order": s.order,
                "state": s.state,
                "score": round(s.candidate.score, 3),
                "tested_on": s.flight,
                "value": s.value,
            }
            for s in test.steps
        ],
        "by_hand": [change(c) for c in test.by_hand],
    }


def find_shift(shifts: Sequence[Shift], metric: str, segment: str, group: str, flight: str
               ) -> Shift | None:  # fmt: skip
    return next((s for s in shifts if (s.metric, s.segment, s.group, s.flight)
                 == (metric, segment, group, flight)), None)  # fmt: skip
