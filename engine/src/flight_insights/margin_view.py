"""The flight's motor-out margin and payload for the Propulsion view (spec: Propulsion,
"Motor-out margin" and "Payload"; mock-up Propulsion), from its latest analysis: what
the motor_out and payload analyzers measured, the frame's motor layout from the
firmware's own mixer table, and the take-off weight the airframe's payload bands put on
this flight. Nothing is worked out again here, and nothing reads a position.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.motor_out import motor_frame
from flight_insights.analyzers.registry import Registry
from flight_insights.store.db import Store
from flight_insights.trends import analysed_flights, group_of


def _latest(store: Store, flight_id: str) -> tuple[str, dict[str, Any], list[Any]] | None:
    row = store.conn.execute(
        "SELECT id, analyzers_json, evidence_json FROM analysis_run WHERE flight_id = ? "
        "ORDER BY created_hlc DESC LIMIT 1",
        (flight_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), json.loads(row[1]), json.loads(row[2])


def _metric(store: Store, run_id: str, metric: str) -> float | None:
    row = store.conn.execute(
        "SELECT value FROM metric_value WHERE run_id = ? AND metric = ? LIMIT 1",
        (run_id, metric),
    ).fetchone()
    return None if row is None else float(row[0])


def _skipped(outcomes: Mapping[str, Any], name: str) -> str | None:
    outcome = outcomes.get(name)
    return None if outcome is None else outcome.get("skipped")


def _estimate(store: Store, reg: Registry, flight_id: str) -> tuple[str | None, float | None]:
    """The flight's payload group and the take-off weight its bands put on it, in kg
    (None until a flight of the airframe was weighed)."""
    row = store.get("flight", flight_id)
    if row is None or row["airframe_id"] is None:
        return None, None
    flights, bands = analysed_flights(store, reg, str(row["airframe_id"]))
    mine = next((f for f in flights if f.id == flight_id), None)
    if mine is None:
        return None, None
    return group_of(mine, bands), bands.kg(mine.thrust)


def margin(store: Store, flight: FlightData, reg: Registry) -> dict[str, Any]:
    """`flight.margin`: the flight's airframe (for its payload bands, `trend.payloads`), and
    the payload and the motor-out margin of its latest analysis, each None until it was
    analysed, or {"skipped": why} where its analyzer had nothing to go on."""
    row = store.get("flight", flight.id)
    airframe = None if row is None else row["airframe_id"]
    latest = _latest(store, flight.id)
    if latest is None:
        return {"airframe": airframe, "payload": None, "motor_out": None}
    run_id, outcomes, evidence = latest
    by_id = {str(e.get("id")): e for e in evidence}
    group, kg = _estimate(store, reg, flight.id)
    weighed = None if row is None else row["takeoff_kg"]

    payload: dict[str, Any] | None
    seen = by_id.get("payload:hover")
    if seen is None:
        payload = {"skipped": _skipped(outcomes, "payload")}
    else:
        v: Mapping[str, Any] = seen.get("values", {})
        thrust = _metric(store, run_id, "payload.hover_thrust")
        payload = {
            "hover_thrust_pct": None if thrust is None else round(thrust, 2),
            "seconds": v.get("seconds"), "tilt_deg": v.get("tilt_deg"),
            "density_ratio": v.get("density_ratio"), "group": group,
            "estimated_kg": None if kg is None else round(kg, 1), "takeoff_kg": weighed,
        }  # fmt: skip

    motor_out: dict[str, Any]
    seen = by_id.get("motor_out:hover")
    if seen is None:
        motor_out = {"skipped": _skipped(outcomes, "motor_out")}
    else:
        v = seen.get("values", {})
        found = motor_frame(flight, reg)
        layout: list[Mapping[str, Any]] = [] if isinstance(found, str) else found[0]["motors"]
        needed: list[float | None] = list(v.get("output_needed_pct") or [])

        def number(m: Mapping[str, Any]) -> int:
            return int(m["motor"])

        motors = [
            {"motor": number(m), "roll": float(m["roll"]), "pitch": float(m["pitch"]),
             "yaw": float(m["yaw"]),
             "needed_pct": needed[k] if k < len(needed) else None}
            for k, m in enumerate(sorted(layout, key=number))
        ]  # fmt: skip
        finding = store.conn.execute(
            "SELECT code, severity FROM finding WHERE run_id = ? AND analyzer = 'motor_out' "
            "LIMIT 1",
            (run_id,),
        ).fetchone()
        ratio = v.get("weight_ratio")
        base = weighed if weighed is not None else kg
        motor_out = {
            "frame": v.get("frame"), "hold_yaw": v.get("hold_yaw"),
            "target_pct": v.get("target_pct"), "hover_thrust_pct": v.get("hover_thrust_pct"),
            "worst_motor": v.get("worst_motor"), "busiest_motor": v.get("busiest_motor"),
            "worst_output_pct": v.get("worst_output_pct"),
            "best_split_output_pct": v.get("best_split_output_pct"),
            "margin_pts": _metric(store, run_id, "motor_out.margin"),
            "weight_ratio": ratio,
            "heaviest_kg": None if ratio is None or base is None else round(ratio * base, 1),
            "weight_from": None if base is None else ("weighed" if weighed is not None
                                                      else "estimated"),
            "motors": motors,
            "finding": None if finding is None else {"code": finding[0], "severity": finding[1]},
        }  # fmt: skip
    return {"airframe": airframe, "payload": payload, "motor_out": motor_out}
