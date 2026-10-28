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
from flight_insights.recommend import followup
from flight_insights.recommend.export import (
    ExportError,
    export_card,
    export_revert,
    export_step,
    export_undo,
)
from flight_insights.recommend.plan import Plan, Recommendation, Source, build_plan, to_json
from flight_insights.recommend.records import (
    RecommendationConflict,
    RecommendationError,
    flew_before,
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
    reg = Registry.load(pack)
    findings = [*analysis["findings"], *_ai(store, dialect, flight_id, analysis)]
    # A filter diagnostic is not proposed again while nothing it checks has changed.
    held: list[dict[str, Any]] = []
    checked = followup.filters_checked_on(store, pack, reg, flight_id, params)
    if checked is not None:
        diagnostics = set(reg.rules["logging"]["filter_settings"]["diagnostics"])
        held = [
            {"code": f["code"], "flight": checked} for f in findings if f["code"] in diagnostics
        ]
        findings = [f for f in findings if f["code"] not in diagnostics]
    # An offer is not made again once a later finding showed its work done on this build.
    once: Mapping[str, str] = reg.rules["recommend"]["held_once_done"]["findings"]
    for code in sorted({f["code"] for f in findings} & set(once)):
        done = _done_on_build(store, row, once[code])
        if done is not None:
            held.append({"code": code, "flight": done, "reason": "held_once_done"})
            findings = [f for f in findings if f["code"] != code]
    findings += followup.revert_findings(store, flight_id, dialect, params)
    findings += followup.drill_findings(store, reg, flight_id, analysis)
    findings += followup.sweep_findings(store, reg, flight_id, analysis)
    from flight_insights.performance import advice, buildup, charge  # trends plans too

    ctx = (
        advice.context(store, cache, pack, reg, flight_id, analysis) if cache is not None else None
    )
    limits: dict[str, float] = {}
    if ctx is not None:
        for got in (advice.speeds(ctx, params), buildup.buildup(ctx, params, reg)):
            findings += got.findings
            for name, value in got.raise_limits.items():
                limits[name] = max(value, limits.get(name, value))
        findings += charge.charge_findings(ctx, store, reg, params, analysis)
    plan = build_plan(flight_id, findings, params, dialect, reg, limits)
    plan.held_back = held
    return plan


def _done_on_build(store: Store, flight: Mapping[str, Any], code: str) -> str | None:
    """The latest flight of this flight's airframe and build version, flown no later than
    it, that raised `code`; None when none did."""
    rows = store.conn.execute(
        "SELECT DISTINCT fl.* FROM finding f JOIN flight fl ON fl.id = f.flight_id "
        "WHERE fl.airframe_id IS ? AND fl.build_version_id IS ? AND f.code = ?",
        (flight["airframe_id"], flight["build_version_id"], code),
    )
    names = [d[0] for d in rows.description]
    found = [dict(zip(names, r, strict=True)) for r in rows.fetchall()]
    latest: dict[str, Any] | None = None
    for f in found:
        if (f["id"] == flight["id"] or flew_before(f, flight)) and (
            latest is None or flew_before(latest, f)
        ):
            latest = f
    return None if latest is None else str(latest["id"])


def _ai(
    store: Store, dialect: Dialect, flight_id: str, analysis: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The changes the flight's accepted AI reply proposes, for this same analysis, as
    findings in canonical names: validated and merged like every other."""
    exchange = results.latest(store, flight_id)
    if exchange is None or not results.same_run(exchange, analysis):
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
    row = store.get("flight", flight_id)
    messages = cache.names(row["flight_key"]) if row and row.get("flight_key") else None
    grade_flight(store, pack, flight_id, analysis, messages)
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
    "export_card",
    "export_revert",
    "export_step",
    "export_undo",
    "flight_plan",
    "grade_flight",
    "history",
    "plan_and_record",
    "record_plan",
    "set_status",
    "to_json",
]
