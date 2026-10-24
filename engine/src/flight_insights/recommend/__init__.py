"""Recommendations: a flight's findings as a staged, validated plan and a parameter
file to review (spec: Recommendation engine)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flight_insights.ai import results
from flight_insights.analyzers.flight import FlightError
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.run import run_analysis
from flight_insights.history.persist import load_snapshot
from flight_insights.knowledge import Pack
from flight_insights.params.dialect import Dialect
from flight_insights.recommend.export import ExportError, export_step
from flight_insights.recommend.plan import Plan, Recommendation, Source, build_plan, to_json
from flight_insights.recommend.records import (
    RecommendationConflict,
    RecommendationError,
    grade_flight,
    history,
    record_plan,
    set_status,
)
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store


def flight_plan(
    store: Store,
    cache: ColumnCache | None,
    pack: Pack,
    flight_id: str,
    analysis: Mapping[str, Any] | None = None,
) -> Plan:
    """The plan for a flight's current analysis (running it first if needed, which
    needs the cache), in the dialect of the firmware the flight flew."""
    row = store.get("flight", flight_id)
    if row is None or not row["param_snapshot_id"]:
        raise FlightError(f"no flight {flight_id} with parameters")
    if analysis is None:
        if cache is None:
            raise FlightError(f"flight {flight_id} has no analysis to plan from")
        analysis = run_analysis(store, cache, pack, flight_id)
    snapshot = load_snapshot(store, row["param_snapshot_id"])
    dialect = snapshot.dialect(pack)
    params = dialect.to_canonical(snapshot.values).values
    findings = [*analysis["findings"], *_ai(store, dialect, flight_id, analysis)]
    return build_plan(flight_id, findings, params, dialect, Registry.load(pack))


def _ai(
    store: Store, dialect: Dialect, flight_id: str, analysis: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The changes the flight's accepted AI reply proposes, for this same analysis, as
    findings in canonical names: validated and merged like every other."""
    exchange = results.latest(store, flight_id)
    if exchange is None or exchange["run_id"] != analysis.get("run"):
        return []
    out: list[dict[str, Any]] = []
    for f in results.ai_findings(exchange, analysis["findings"]):
        changes: list[dict[str, Any]] = []
        for native, value in f.pop("native_changes"):
            name, converted = dialect.canonical(native, value)
            proposed = value if converted is None else converted
            changes.append({"param": name, "proposed": proposed, "stage": f["stage"]})
        out.append(f | {"changes": changes})
    return out


def plan_and_record(store: Store, cache: ColumnCache, pack: Pack, flight_id: str) -> dict[str, Any]:
    """What the app shows for a flight: earlier recommendations graded against it first,
    then its own plan, kept for its airframe with each recommendation's stored status."""
    analysis = run_analysis(store, cache, pack, flight_id)
    grade_flight(store, pack, flight_id, analysis)
    plan = flight_plan(store, cache, pack, flight_id, analysis)
    return record_plan(store, to_json(plan, Registry.load(pack)))


__all__ = [
    "ExportError",
    "Plan",
    "Recommendation",
    "RecommendationConflict",
    "RecommendationError",
    "Source",
    "build_plan",
    "export_step",
    "flight_plan",
    "grade_flight",
    "history",
    "plan_and_record",
    "record_plan",
    "set_status",
    "to_json",
]
