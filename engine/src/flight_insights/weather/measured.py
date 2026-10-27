"""The wind each flight's aircraft measured, beside the modelled wind (spec: Weather and
wind, "The aircraft"; rules in `knowledge/rules/wind.json`, "calibration").

A flight that changed speed and heading measured its own wind (analyzers/wind.py). A
flight that only hovered, or held one speed, cannot tell its drag from the wind, so it
borrows the drag its airframe measured on other flights in the same payload band and on
the same build: the median of their fitted drag (k1, k2) and lean in still air, from the
latest of them. Each of its steady windows then gives the wind from its own lean, as the
analyzer's do. The analyzers read one flight each, so this lives here, beside the
weather record, and works on demand from the flight's cached tables.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData, FlightError
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.wind import Fit, blowing_from, ekf, window_winds, windows
from flight_insights.knowledge import Pack
from flight_insights.store.cache import CacheError, ColumnCache
from flight_insights.store.db import Store


@dataclass(frozen=True)
class Own:
    """What the analyzer measured on a flight, from its latest analysis."""

    flight: str
    fit: Fit
    speed: float
    peak: float
    from_deg: float
    windows: int
    height_m: float


def _own(store: Store, flight_id: str) -> Own | None:
    row = store.conn.execute(
        "SELECT id, evidence_json FROM analysis_run WHERE flight_id = ? "
        "ORDER BY created_hlc DESC LIMIT 1",
        (flight_id,),
    ).fetchone()
    if row is None:
        return None
    run_id, evidence_json = row
    found = [e for e in json.loads(evidence_json) if e.get("id") == "wind:flight"]
    if not found:
        return None
    v: Mapping[str, Any] = found[0].get("values", {})
    if not {"k1", "k2", "bias_x", "bias_y"} <= set(v):
        return None
    metrics = dict(store.conn.execute(
        "SELECT metric, value FROM metric_value WHERE run_id = ? AND metric IN (?, ?)",
        (run_id, "wind.speed_ms", "wind.peak_ms"),
    ).fetchall())  # fmt: skip
    if "wind.speed_ms" not in metrics:
        return None
    fit = Fit(float(v["k1"]), float(v["k2"]), np.zeros(2),
              np.array([float(v["bias_x"]), float(v["bias_y"])]),
              v.get("model") == "quadratic", float(v.get("residual_rms") or 0.0))  # fmt: skip
    return Own(flight_id, fit, float(metrics["wind.speed_ms"]),
               float(metrics.get("wind.peak_ms") or metrics["wind.speed_ms"]),
               float(v.get("from_deg") or 0.0), int(v.get("windows") or 0),
               float(v.get("height_m") or 0.0))  # fmt: skip


def borrowed(store: Store, reg: Registry, flight_id: str) -> tuple[Fit, tuple[str, ...]] | None:
    """The drag the flight's airframe measured on its other flights in the same payload
    band and on the same build, or None when it has none."""
    from flight_insights.trends import grouped_flights  # trends imports recommend

    row = store.get("flight", flight_id)
    if row is None or row["airframe_id"] is None:
        return None
    rules: Mapping[str, Any] = reg.rules["wind"]["calibration"]
    flights = grouped_flights(store, reg, str(row["airframe_id"]))
    mine = next(((f, g) for f, g in flights if f.id == flight_id), None)
    if mine is None:
        return None

    def alike(f: Any, g: str) -> bool:
        # the same payload band; a flight with no hover thrust to place it in one (a hover
        # leaning hard into the wind) goes by its payload label
        return g == mine[1] if mine[1].startswith("band:") else f.payload == mine[0].payload

    build = row["build_version_id"]
    same: list[Own] = []
    for f, g in flights:
        if f.id == flight_id or not alike(f, g):
            continue
        other = store.get("flight", f.id)
        if other is None or other["build_version_id"] != build:
            continue
        own = _own(store, f.id)
        if own is not None:
            same.append(own)
    same = same[-int(rules["flights"]) :]
    if not same:
        return None
    quadratic = sum(o.fit.quadratic for o in same) * 2 >= len(same)
    k1 = float(np.median([o.fit.k1 for o in same]))
    k2 = float(np.median([o.fit.k2 for o in same])) if quadratic else 0.0
    bias = np.median(np.array([o.fit.bias for o in same]), axis=0)
    return Fit(k1, k2, np.zeros(2), bias, quadratic, 0.0), tuple(o.flight for o in same)


def drag(store: Store, reg: Registry, flight_id: str) -> tuple[Fit, tuple[str, ...]] | None:
    """The drag the flight measured itself, else the drag its airframe measured in the
    same payload band and on the same build, with the flights it came from."""
    own = _own(store, flight_id)
    if own is not None:
        return own.fit, (flight_id,)
    return borrowed(store, reg, flight_id)


def measured(store: Store, cache: ColumnCache, pack: Pack, flight_id: str
             ) -> dict[str, Any] | None:  # fmt: skip
    """The flight's measured wind: its own when it measured one, else from its airframe's
    drag; None when neither can be had."""
    own = _own(store, flight_id)
    if own is not None:
        return {
            "source": "flight", "speed_ms": round(own.speed, 2), "peak_ms": round(own.peak, 2),
            "from_deg": round(own.from_deg), "windows": own.windows,
            "height_m": round(own.height_m, 1), "drag_from": [],
        }  # fmt: skip
    reg = Registry.load(pack)
    got = borrowed(store, reg, flight_id)
    if got is None:
        return None
    fit, sources = got
    rules: Mapping[str, Any] = reg.rules["wind"]
    try:
        flight = FlightData.load(store, cache, pack, flight_id)
        e = ekf(flight)
    except (FlightError, CacheError):
        return None
    if e is None:
        return None
    w = windows(e, flight.arm_us, flight.disarm_us, rules)
    if len(w) < int(rules["calibration"]["min_windows"]):
        return None
    winds = window_winds(w, fit)
    speeds = np.hypot(winds[:, 0], winds[:, 1])
    return {
        "source": "airframe",
        "speed_ms": round(float(np.mean(speeds)), 2),
        "peak_ms": round(float(np.percentile(speeds, float(rules["peak_percentile"]))), 2),
        "from_deg": round(blowing_from(np.mean(winds, axis=0))),
        "windows": len(w),
        "height_m": round(float(np.median(w.height)), 1),
        "drag_from": list(sources),
    }


def difference(measured: Mapping[str, Any] | None, record: Mapping[str, Any] | None
               ) -> dict[str, Any] | None:  # fmt: skip
    """How far the modelled wind at the flight's height was from the measured: speed
    (model less measured) and direction (the model's, turned from the measured, in
    -180 to 180 degrees)."""
    if not measured or not record:
        return None
    model, model_from = record.get("wind_ms"), record.get("wind_from_deg")
    if not isinstance(model, int | float) or not math.isfinite(model):
        return None
    out: dict[str, Any] = {"speed_ms": round(float(model) - float(measured["speed_ms"]), 2)}
    if isinstance(model_from, int | float) and float(measured["speed_ms"]) > 0.5:
        turned = (float(model_from) - float(measured["from_deg"]) + 180.0) % 360.0 - 180.0
        out["from_deg"] = round(turned)
    return out
