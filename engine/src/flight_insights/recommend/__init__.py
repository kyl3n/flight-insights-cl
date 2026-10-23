"""Recommendations: a flight's findings as a staged, validated plan and a parameter
file to review (spec: Recommendation engine)."""

from __future__ import annotations

from flight_insights.analyzers.flight import FlightError
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.run import run_analysis
from flight_insights.history.persist import load_snapshot
from flight_insights.knowledge import Pack
from flight_insights.recommend.export import ExportError, export_step
from flight_insights.recommend.plan import Plan, Recommendation, Source, build_plan, to_json
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store


def flight_plan(store: Store, cache: ColumnCache, pack: Pack, flight_id: str) -> Plan:
    """The plan for a flight's current analysis (running it first if needed), in the
    dialect of the firmware the flight flew."""
    row = store.get("flight", flight_id)
    if row is None or not row["param_snapshot_id"]:
        raise FlightError(f"no flight {flight_id} with parameters")
    analysis = run_analysis(store, cache, pack, flight_id)
    snapshot = load_snapshot(store, row["param_snapshot_id"])
    dialect = snapshot.dialect(pack)
    params = dialect.to_canonical(snapshot.values).values
    return build_plan(flight_id, analysis["findings"], params, dialect, Registry.load(pack))


__all__ = [
    "ExportError",
    "Plan",
    "Recommendation",
    "Source",
    "build_plan",
    "export_step",
    "flight_plan",
    "to_json",
]
