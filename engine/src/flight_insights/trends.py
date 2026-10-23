"""Performance trends and ideal ranges (spec: Performance trends and ideal ranges).

Every metric is a series per airframe: one point per flight, from that flight's latest
analysis, read against the metric's fixed zones and against the airframe's own
baseline, with every change marked on the same time axis.

- **Like with like.** A series holds one segment (hover, flight) and, by default, one
  payload: the label the flight was imported with. A heavy hover is never plotted
  against a light one.
- **The baseline** is the spread of the airframe's good flights in the same series:
  the 10th to 90th percentile of the most recent ones, once there are enough. A flight
  is good when its analysis is complete and raised nothing out of range or unsafe.
- **Drift.** The latest flights outside the baseline on the worse side, while the
  baseline was made without them, are flagged before they become a fault, even
  inside every fixed zone.
- **Events.** Parameter change sets, build changes, firmware updates and applied
  recommendations, each placed at the flight it first applies to.

Nothing here leaves the laptop or reads positions: only metric values, findings and
the records the store already holds.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.analyzers.registry import Metric, Registry
from flight_insights.store.db import Store

BAD_SEVERITIES = ("out", "safety")


@dataclass(frozen=True)
class Flight:
    id: str
    utc_us: int | None  # take-off time, where the log has UTC
    created_hlc: str
    firmware: str | None
    payload: str | None
    run_id: str | None  # its latest analysis
    good: bool  # may count towards the baseline


def flight_order(row: Mapping[str, Any]) -> tuple[bool, int, str]:
    """When a flight flew: by UTC take-off where the log has it, else by import order."""
    utc = (
        int(row["boot_utc_us"]) + int(row["arm_us"])
        if row.get("boot_utc_us") is not None and row.get("arm_us") is not None
        else None
    )
    return (utc is None, utc or 0, str(row["created_hlc"]))


def _flights(store: Store, airframe_id: str) -> list[Flight]:
    cur = store.conn.execute(
        "SELECT id, boot_utc_us, arm_us, created_hlc, firmware, payload FROM flight "
        "WHERE airframe_id = ?",
        (airframe_id,),
    )
    rows = [{d[0]: v for d, v in zip(cur.description, r, strict=True)} for r in cur.fetchall()]
    rows.sort(key=flight_order)
    out: list[Flight] = []
    for r in rows:
        run = store.conn.execute(
            "SELECT id, status FROM analysis_run WHERE flight_id = ? "
            "ORDER BY created_hlc DESC LIMIT 1",
            (r["id"],),
        ).fetchone()
        bad = (
            run is not None
            and store.conn.execute(
                "SELECT 1 FROM finding WHERE run_id = ? AND severity IN (?, ?) LIMIT 1",
                (run[0], *BAD_SEVERITIES),
            ).fetchone()
        )
        utc = flight_order(r)
        out.append(
            Flight(
                id=r["id"],
                utc_us=None if utc[0] else utc[1],
                created_hlc=r["created_hlc"],
                firmware=r["firmware"],
                payload=r["payload"],
                run_id=run[0] if run else None,
                good=run is not None and run[1] == "complete" and not bad,
            )
        )
    return out


def _value(store: Store, run_id: str, metric: str, segment: str) -> tuple[float, Any] | None:
    row = store.conn.execute(
        "SELECT value, spread_json, zone FROM metric_value "
        "WHERE run_id = ? AND metric = ? AND segment = ?",
        (run_id, metric, segment),
    ).fetchone()
    return None if row is None else (float(row[0]), (json.loads(row[1]), row[2]))


def _worse(metric: Metric, value: float, low: float, high: float) -> bool:
    """Outside the baseline on the side that is worse for this metric."""
    if metric.direction == "lower":
        return value > high
    if metric.direction == "higher":
        return value < low
    return value < low or value > high


def baseline(values: Sequence[float], rules: Mapping[str, Any]) -> dict[str, Any] | None:
    """The spread of the most recent good values, once there are enough."""
    recent = list(values)[-int(rules["baseline_max_flights"]) :]
    if len(recent) < int(rules["baseline_min_flights"]):
        return None
    low_p, high_p = rules["baseline_percentiles"]
    a = np.asarray(recent, dtype=float)
    return {
        "low": float(np.percentile(a, low_p)),
        "high": float(np.percentile(a, high_p)),
        "median": float(np.median(a)),
        "flights": len(recent),
    }


def series(
    store: Store,
    reg: Registry,
    airframe_id: str,
    metric_id: str,
    segment: str,
    payload: str | None = None,
    any_payload: bool = False,
) -> dict[str, Any]:
    """One metric's series for one airframe, with its baseline, drift and zones.
    `payload` picks a payload label (None: flights with none); `any_payload` mixes them."""
    metric = reg.metrics[metric_id]
    rules: Mapping[str, Any] = reg.rules["trends"]
    points: list[dict[str, Any]] = []
    for f in _flights(store, airframe_id):
        if f.run_id is None or (not any_payload and f.payload != payload):
            continue
        found = _value(store, f.run_id, metric_id, segment)
        if found is None:
            continue
        value, (spread, zone) = found
        points.append(
            {"flight": f.id, "utc_us": f.utc_us, "value": value, "spread": spread,
             "zone": zone, "payload": f.payload, "good": f.good}
        )  # fmt: skip
    drift_n = int(rules["drift_flights"])
    tail = points[-drift_n:] if len(points) > drift_n else []
    earlier = points[: len(points) - len(tail)]
    base = baseline([p["value"] for p in earlier if p["good"]], rules)
    drift: dict[str, Any] | None = None
    if (
        base is not None
        and tail
        and all(_worse(metric, p["value"], base["low"], base["high"]) for p in tail)
    ):
        drift = {"flights": [p["flight"] for p in tail], "since": tail[0]["flight"]}
    # The band drawn is made from every good flight so far, the drift check's without the tail.
    shown = baseline([p["value"] for p in points if p["good"]], rules)
    return {
        "airframe": airframe_id,
        "metric": metric_id,
        "segment": segment,
        "payload": None if any_payload else payload,
        "unit": metric.unit,
        "direction": metric.direction,
        "zones": {z: [list(i) for i in b] for z, b in metric.zones.items()},
        "source": dict(metric.source),
        "points": points,
        "baseline": shown,
        "drift": drift,
    }


def overview(store: Store, reg: Registry, airframe_id: str) -> list[dict[str, Any]]:
    """The airframe dashboard: for each metric and segment its latest flight has, the
    latest value and zone, the change since the previous flight and since the
    baseline, drift, and a sparkline, all within the latest flight's payload."""
    flights = [f for f in _flights(store, airframe_id) if f.run_id is not None]
    if not flights:
        return []
    latest = flights[-1]
    assert latest.run_id is not None
    keys = store.conn.execute(
        "SELECT metric, segment FROM metric_value WHERE run_id = ? ORDER BY metric, segment",
        (latest.run_id,),
    ).fetchall()
    spark = int(reg.rules["trends"]["sparkline_flights"])
    cards: list[dict[str, Any]] = []
    for metric_id, segment in keys:
        if metric_id not in reg.metrics:
            continue
        s = series(store, reg, airframe_id, metric_id, segment, latest.payload)
        points = s["points"]
        if not points:
            continue
        last = points[-1]
        previous = points[-2]["value"] if len(points) > 1 else None
        base = s["baseline"]
        cards.append(
            {
                "metric": metric_id, "segment": segment, "payload": latest.payload,
                "unit": s["unit"], "value": last["value"], "zone": last["zone"],
                "since_previous": None if previous is None else last["value"] - previous,
                "since_baseline": None if base is None else last["value"] - base["median"],
                "baseline": base, "drift": s["drift"] is not None,
                "sparkline": [p["value"] for p in points[-spark:]],
            }
        )  # fmt: skip
    return cards


def events(store: Store, airframe_id: str) -> list[dict[str, Any]]:
    """What changed on the airframe, placed at the flight each change first applies to:
    parameter change sets, build changes, firmware updates and applied recommendations."""
    flights = _flights(store, airframe_id)
    order = {f.id: i for i, f in enumerate(flights)}
    out: list[dict[str, Any]] = []
    for cid, kind, origin, after, details in store.conn.execute(
        "SELECT id, kind, origin, after_flight_id, details_json FROM change_set "
        "WHERE airframe_id = ?",
        (airframe_id,),
    ):
        detail: dict[str, Any] = json.loads(details)
        names = [str(c["name"]) for c in detail.get("changes", [])]
        out.append(
            {"kind": f"{kind} change", "id": cid, "flight": after, "origin": origin,
             "params": names}
        )  # fmt: skip
    for bid, number, reason, first in store.conn.execute(
        "SELECT id, number, reason, first_flight_id FROM build_version "
        "WHERE airframe_id = ? AND number > 1",  # the first build is where it started
        (airframe_id,),
    ):
        out.append({"kind": "build", "id": bid, "flight": first, "number": number,
                    "reason": reason})  # fmt: skip
    previous: str | None = None
    for f in flights:
        if f.firmware and previous and f.firmware != previous:
            out.append({"kind": "firmware", "flight": f.id, "from": previous, "to": f.firmware})
        previous = f.firmware or previous
    for rid, param, status, graded, proposed in store.conn.execute(
        "SELECT id, param, status, graded_flight_id, proposed_value FROM recommendation "
        "WHERE airframe_id = ? AND graded_flight_id IS NOT NULL",
        (airframe_id,),
    ):
        out.append({"kind": "recommendation", "id": rid, "flight": graded, "param": param,
                    "proposed": proposed, "grade": status})  # fmt: skip
    out.sort(key=lambda e: order.get(e["flight"], len(order)))
    return out


def platform(
    store: Store, reg: Registry, platform_id: str, metric_id: str, segment: str
) -> dict[str, Any]:
    """The same metric for every airframe of a platform, as small multiples on shared
    axes; an airframe whose latest value sits outside the spread of its siblings'
    latest values is marked as drifting away from them."""
    airframes = [
        a for (a,) in store.conn.execute(
            "SELECT id FROM airframe WHERE platform_id = ? ORDER BY created_hlc", (platform_id,)
        )
    ]  # fmt: skip
    rows = [series(store, reg, a, metric_id, segment, any_payload=True) for a in airframes]
    latest = {r["airframe"]: r["points"][-1]["value"] for r in rows if r["points"]}
    metric = reg.metrics[metric_id]
    for r in rows:
        mine = latest.get(r["airframe"])
        others = [v for a, v in latest.items() if a != r["airframe"]]
        r["apart"] = bool(
            mine is not None and len(others) >= 2 and _worse(metric, mine, min(others), max(others))
        )
    return {"platform": platform_id, "metric": metric_id, "segment": segment, "airframes": rows}


def flight_list(
    store: Store,
    airframe_id: str | None = None,
    platform_id: str | None = None,
    limit: int = 200,
    flight_id: str | None = None,
) -> list[dict[str, Any]]:
    """Flights, newest first, each with its airframe, take-off, duration, modes and a
    summary of its latest analysis (worst severity and counts), for the Flights view;
    or the one flight asked for."""
    where, args = "WHERE f.airframe_id IS NOT NULL", []
    if flight_id is not None:
        where, args = "WHERE f.id = ?", [flight_id]
    elif airframe_id is not None:
        where, args = "WHERE f.airframe_id = ?", [airframe_id]
    elif platform_id is not None:
        where, args = "WHERE a.platform_id = ?", [platform_id]
    cur = store.conn.execute(
        "SELECT f.id, f.airframe_id, a.name AS airframe, f.boot_utc_us, f.arm_us, f.disarm_us, "
        "f.created_hlc, f.firmware, f.payload, f.fingerprint_json "
        f"FROM flight f JOIN airframe a ON a.id = f.airframe_id {where}",
        args,
    )
    rows = [{d[0]: v for d, v in zip(cur.description, r, strict=True)} for r in cur.fetchall()]
    rows.sort(key=flight_order, reverse=True)
    out: list[dict[str, Any]] = []
    for r in rows[:limit]:
        fp: dict[str, Any] = json.loads(r["fingerprint_json"]) if r["fingerprint_json"] else {}
        run = store.conn.execute(
            "SELECT id, status FROM analysis_run WHERE flight_id = ? "
            "ORDER BY created_hlc DESC LIMIT 1",
            (r["id"],),
        ).fetchone()
        analysis: dict[str, Any] | None = None
        if run is not None:
            counts = dict(
                store.conn.execute(
                    "SELECT severity, count(*) FROM finding WHERE run_id = ? GROUP BY severity",
                    (run[0],),
                ).fetchall()
            )
            worst = next((s for s in ("safety", "out", "caution", "info") if counts.get(s)), None)
            analysis = {"status": run[1], "counts": counts, "worst": worst}
        order = flight_order(r)
        duration = (
            (int(r["disarm_us"]) - int(r["arm_us"])) / 1e6
            if r["arm_us"] is not None and r["disarm_us"] is not None
            else None
        )
        out.append(
            {"id": r["id"], "airframe_id": r["airframe_id"], "airframe": r["airframe"],
             "utc_us": None if order[0] else order[1], "duration_s": duration,
             "firmware": r["firmware"], "payload": r["payload"],
             "modes": sorted(fp.get("mode_s", {})), "analysis": analysis}
        )  # fmt: skip
    return out
