"""The airframe's wind envelope, per payload band (spec: Weather and wind, "Measures the
wind envelope"; mock-up Weather). The wind analyzer works out, on each flight that
measured its own drag, the strongest steady wind it can hold at that weight and on those
settings (analyzers/wind.py). Within a payload band the airframe's envelope is the median
of its latest such flights (rules/wind.json, envelope: `flights`), its interval a typical
flight's (the medians of their interval ends), and its limit the one most of them hit.
Drag is an acceleration, so it changes with the weight: a band no flight measured its
drag in has no envelope. Nothing is worked out again here, and nothing reads a position.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any

from flight_insights.analyzers.registry import Registry
from flight_insights.store.db import Store


@dataclass(frozen=True)
class Held:
    """What one flight's analysis says it can hold."""

    flight: str
    hold: float
    low: float | None
    high: float | None
    lean: float | None  # at the reference wind
    values: Mapping[str, Any]  # the wind:envelope evidence


def _held(store: Store, flight_id: str, run_id: str) -> Held | None:
    row = store.conn.execute(
        "SELECT evidence_json FROM analysis_run WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    found = [e for e in json.loads(row[0]) if e.get("id") == "wind:envelope"]
    if not found:
        return None
    metrics: dict[str, tuple[float, dict[str, float]]] = {}
    for m, v, s in store.conn.execute(
        "SELECT metric, value, spread_json FROM metric_value WHERE run_id = ? "
        "AND metric IN ('wind.envelope_ms', 'wind.lean_at_reference_deg')",
        (run_id,),
    ).fetchall():
        spread: dict[str, Any] = json.loads(s) if s else {}
        metrics[str(m)] = (float(v), {k: float(x) for k, x in spread.items()})
    if "wind.envelope_ms" not in metrics:
        return None
    hold, spread = metrics["wind.envelope_ms"]
    lean = metrics.get("wind.lean_at_reference_deg")
    return Held(
        flight_id, hold, spread.get("low"), spread.get("high"),
        None if lean is None else lean[0], found[0].get("values") or {},
    )  # fmt: skip


def _mid(values: Sequence[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return float(median(known)) if known else None


def _band(items: Sequence[Held]) -> dict[str, Any]:
    hold = float(median(h.hold for h in items))
    by = [str(h.values.get("limit_by") or "lean") for h in items]
    limit = max(set(by), key=lambda k: (by.count(k), k == "thrust"))  # ties: the cautious
    flown = [float(h.values["airspeed_flown_ms"]) for h in items
             if isinstance(h.values.get("airspeed_flown_ms"), int | float)]  # fmt: skip

    def r(v: float | None, places: int = 1) -> float | None:
        return None if v is None else round(v, places)

    limits = [float(h.values["limit_deg"]) for h, k in zip(items, by, strict=True)
              if k == limit and isinstance(h.values.get("limit_deg"), int | float)]  # fmt: skip
    return {
        "flights": [h.flight for h in items],
        "hold_ms": round(hold, 1),
        "low_ms": r(_mid([h.low for h in items])),
        "high_ms": r(_mid([h.high for h in items])),
        "lean_deg": r(_mid([h.lean for h in items])),
        "limit_by": limit,
        "limit_deg": r(_mid(limits)),
        "airspeed_flown_ms": round(max(flown), 1) if flown else None,
        "extrapolated": any(bool(h.values.get("extrapolated")) for h in items),
        "thrust_unmeasured": any(bool(h.values.get("thrust_unmeasured")) for h in items),
        "current_limited": any(bool(h.values.get("current_limited")) for h in items),
    }


def envelope(store: Store, reg: Registry, airframe_id: str, flight_id: str | None = None
             ) -> dict[str, Any]:  # fmt: skip
    """The airframe's envelope in each payload band its flights measured their drag in,
    lightest first, then the flights with no band by their payload label; with
    `flight_id`, also which group that flight is in."""
    from flight_insights.trends import analysed_flights, describe_group, group_of

    rules: Mapping[str, Any] = reg.rules["wind"]["envelope"]
    flights, b = analysed_flights(store, reg, airframe_id)
    groups: dict[str, list[Held]] = {}
    mine = next((group_of(f, b) for f in flights if f.id == flight_id), None)
    for f in flights:
        if f.run_id is None:
            continue
        held = _held(store, f.id, f.run_id)
        if held is not None:
            groups.setdefault(group_of(f, b), []).append(held)

    def order(group: str) -> tuple[int, int, str]:
        kind, _, value = group.partition(":")
        return (0, int(value), "") if kind == "band" else (1, 0, value)

    latest = int(rules["flights"])
    bands = [
        {"group": g, **describe_group(g, b), **_band(groups[g][-latest:])}
        for g in sorted(groups, key=order)
    ]
    return {"reference_ms": float(rules["reference_ms"]), "bands": bands, "group": mine}
