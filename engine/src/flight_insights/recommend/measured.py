"""Checks a recommendation's procedure runs across flights (spec: Recommendation engine,
"Closing the loop"): a metric measured before the change and again with it, compared
within each payload band, so a loaded flight is never read against a light one.

- **metric_improves**: every payload band flown before the change must be flown again
  with it, and there the metric must be better by at least `by` (a share of its value
  before), with the two 95% intervals apart where the metric has them. A band still
  waiting for its flight leaves the check unanswered; a band clearly worse marks it worse.
- **metric_not_worse**: the same bands, flown again with it, none worse by `by` with
  the intervals apart: a guard on what the change is not meant to improve.
- **metric_vs_lightest**: with the change, the heaviest band's value against the
  lightest's: at most `max_ratio` times it, or at most `max_above` over it. ArduPilot
  tunes light and expects the same gains to fly heavier aircraft slower, not unstable.

"Before" is each band's latest flight without the change; "with" its latest flight with
it, up to the flight being graded. A parameter change is in a flight that flew with the
proposed value; an action is in the flights after the one that showed it done (or after
the flight that proposed it, when a person marked it done).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from flight_insights.analyzers.registry import Registry
from flight_insights.history.persist import load_snapshot
from flight_insights.knowledge import Pack
from flight_insights.store.db import Store

TOLERANCE = 1e-6
CHECKS = ("metric_improves", "metric_not_worse", "metric_vs_lightest")  # judged here


@dataclass(frozen=True)
class Measured:
    flight: str
    group: str
    with_change: bool
    value: float
    low: float | None
    high: float | None


def _with_change(
    store: Store, pack: Pack, rec: Mapping[str, Any], order: Mapping[str, int]
) -> Callable[[str], bool]:
    if rec["param"] is not None:
        proposed = float(rec["proposed_value"])

        def flew_with(flight_id: str) -> bool:
            row = store.get("flight", flight_id)
            if row is None or not row["param_snapshot_id"]:
                return False
            snapshot = load_snapshot(store, row["param_snapshot_id"])
            value = snapshot.dialect(pack).to_canonical(snapshot.values).values.get(rec["param"])
            return value is not None and abs(value - proposed) < TOLERANCE

        return flew_with
    start = rec["detail"].get("applied_flight") or rec["flight_id"]
    at = order.get(start, -1)
    return lambda flight_id: order.get(flight_id, -1) > at


def measurements(
    store: Store, pack: Pack, reg: Registry, rec: Mapping[str, Any], flight_id: str,
    metric: str, segment: str | None,
) -> list[Measured]:  # fmt: skip
    """The airframe's flights up to `flight_id` that measured the metric, in flying order."""
    from flight_insights.trends import grouped_flights, metric_of  # trends imports recommend

    flights = grouped_flights(store, reg, str(rec["airframe_id"]))
    order = {f.id: i for i, (f, _) in enumerate(flights)}
    if flight_id not in order:
        return []
    changed = _with_change(store, pack, rec, order)
    out: list[Measured] = []
    for f, group in flights[: order[flight_id] + 1]:
        assert f.run_id is not None
        found = metric_of(store, f.run_id, metric, segment)
        if found is None:
            continue
        value, spread = found
        low, high = spread.get("low"), spread.get("high")
        out.append(
            Measured(f.id, group, changed(f.id), value,
                     None if low is None else float(low), None if high is None else float(high))
        )  # fmt: skip
    return out


def _latest(ms: list[Measured], with_change: bool) -> dict[str, Measured]:
    out: dict[str, Measured] = {}
    for m in ms:
        if m.with_change == with_change:
            out[m.group] = m  # in flying order: the last one stays
    return out


def _band(group: str) -> int | None:
    kind, _, value = group.partition(":")
    return int(value) if kind == "band" else None


def improves(
    check: Mapping[str, Any], ms: list[Measured], direction: str
) -> tuple[bool | None, dict[str, Any]]:
    before, after = _latest(ms, False), _latest(ms, True)
    by = float(check["by"])
    sign = -1.0 if direction == "lower" else 1.0  # which way is better
    bands: list[dict[str, Any]] = []
    waiting = False
    for group, b in sorted(before.items()):
        a = after.get(group)
        row: dict[str, Any] = {"group": group, "before_flight": b.flight, "before": b.value}
        if a is None:
            waiting = True
            bands.append(row | {"verdict": "waiting"})
            continue
        change = sign * (a.value - b.value) / abs(b.value) if b.value else 0.0
        intervals = None not in (a.low, a.high, b.low, b.high)
        if intervals:
            assert a.low is not None and a.high is not None
            assert b.low is not None and b.high is not None
            apart_better = a.high < b.low if sign < 0 else a.low > b.high
            apart_worse = a.low > b.high if sign < 0 else a.high < b.low
        else:
            apart_better = apart_worse = True
        verdict = (
            "better" if change >= by and apart_better
            else "worse" if change <= -by and apart_worse
            else "unchanged"
        )  # fmt: skip
        bands.append(row | {"after_flight": a.flight, "after": a.value,
                            "change": round(change, 4), "verdict": verdict})  # fmt: skip
    info: dict[str, Any] = {"bands": bands, "worse": any(r["verdict"] == "worse" for r in bands)}
    if not before or waiting:
        return None, info
    return all(r["verdict"] == "better" for r in bands), info


def versus_lightest(
    check: Mapping[str, Any], ms: list[Measured]
) -> tuple[bool | None, dict[str, Any]]:
    after = {g: m for g, m in _latest(ms, True).items() if _band(g) is not None}
    if len(after) < 2:
        return None, {"bands": sorted(after)}
    light = after[min(after, key=lambda g: _band(g) or 0)]
    heavy = after[max(after, key=lambda g: _band(g) or 0)]
    info: dict[str, Any] = {"light_flight": light.flight, "light": light.value,
                            "heavy_flight": heavy.flight, "heavy": heavy.value}  # fmt: skip
    if "max_ratio" in check:
        ok = light.value > 0 and heavy.value <= light.value * float(check["max_ratio"])
    else:
        ok = heavy.value <= light.value + float(check["max_above"])
    return ok, info


def judge(
    store: Store, pack: Pack, reg: Registry, rec: Mapping[str, Any], flight_id: str,
    check: Mapping[str, Any],
) -> tuple[bool | None, dict[str, Any]]:  # fmt: skip
    metric = reg.metrics[str(check["metric"])]
    ms = measurements(store, pack, reg, rec, flight_id, metric.id, check.get("segment"))
    if check["check"] == "metric_improves":
        return improves(check, ms, metric.direction)
    if check["check"] == "metric_not_worse":
        return not_worse(check, ms, metric.direction)
    return versus_lightest(check, ms)


def not_worse(
    check: Mapping[str, Any], ms: list[Measured], direction: str
) -> tuple[bool | None, dict[str, Any]]:
    """Passes once every band flown before is flown again and none is clearly worse;
    one clearly worse fails it at once (and marks it worse)."""
    passed, info = improves(check, ms, direction)
    if info["worse"]:
        return False, info
    return (None if passed is None else True), info
