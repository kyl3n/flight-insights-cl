"""The analysis methods (spec: Analyzers; Flight report).

| Method | Does |
| --- | --- |
| analysis.run | Analyses a flight, or returns its stored analysis while that is current |
| analysis.get | A flight's latest analysis, current or not, or null if it was never analysed |
| analysis.registry | Every metric and finding the analyzers can produce, with its wording |
| recommend.plan | A flight's findings as a staged, validated plan of changes and actions |
| recommend.export | One step of that plan as .param or .params text, for a person to load |
| recommend.set_status | A person accepts, rejects or marks a recommendation applied |
| recommend.history | An airframe's recommendations, with what happened to each |
| trend.overview | The airframe dashboard: each metric's latest value, zone, change and drift |
| trend.series | One metric's series for an airframe, with its zones, baseline and drift |
| trend.events | What changed on an airframe, placed at the flight it first applies to |
| trend.platform | One metric for every airframe of a platform, on shared axes |

Analysing a flight also grades its airframe's earlier recommendations against it.

Results hold metric values, findings with the evidence they rest on, and no
positions: the analyzers read cached columns, where positions stay encrypted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from flight_insights import analyzers, recommend, trends
from flight_insights.analyzers.registry import LANGUAGES
from flight_insights.rpc.server import (
    BAD_INPUT,
    CONFLICT,
    INVALID_PARAMS,
    Context,
    RpcError,
    Server,
)

if TYPE_CHECKING:
    from flight_insights.rpc.methods import EngineState


def _flight(params: dict[str, Any]) -> str:
    fid = params.get("flight_id")
    if not isinstance(fid, str) or not fid:
        raise RpcError(INVALID_PARAMS, "flight_id must be a flight's id")
    return fid


def add_analysis_methods(server: Server, state: EngineState, guarded: Callable[[Any], Any]) -> None:
    @server.method("analysis.run")
    @guarded
    def analysis_run(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        store, cache = state.require_store(), state.cache
        assert cache is not None
        force = params.get("force", False)
        if not isinstance(force, bool):
            raise RpcError(INVALID_PARAMS, "force must be true or false")
        ctx.progress(0.0, "analysing")
        flight = _flight(params)
        try:
            out = analyzers.run_analysis(store, cache, state.pack, flight, force=force)
        except analyzers.FlightError as e:
            raise RpcError(BAD_INPUT, str(e)) from None
        recommend.grade_flight(store, state.pack, flight, out)
        return out

    @server.method("analysis.get")
    @guarded
    def analysis_get(_ctx: Context, params: dict[str, Any]) -> dict[str, Any] | None:
        reg = analyzers.Registry.load(state.pack)
        return analyzers.analysis_results(state.require_store(), _flight(params), reg)

    @server.method("recommend.plan")
    @guarded
    def recommend_plan(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        store, cache = state.require_store(), state.cache
        assert cache is not None
        ctx.progress(0.0, "planning")
        try:
            return recommend.plan_and_record(store, cache, state.pack, _flight(params))
        except analyzers.FlightError as e:
            raise RpcError(BAD_INPUT, str(e)) from None

    @server.method("recommend.set_status")
    @guarded
    def recommend_set_status(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        rid, status, version = params.get("id"), params.get("status"), params.get("version")
        if not isinstance(rid, str) or not isinstance(status, str):
            raise RpcError(INVALID_PARAMS, "id and status must be strings")
        if not isinstance(version, int) or isinstance(version, bool):
            raise RpcError(INVALID_PARAMS, "version must be the version that was read")
        try:
            return recommend.set_status(state.require_store(), rid, status, version)
        except recommend.RecommendationConflict as e:
            raise RpcError(CONFLICT, str(e)) from None
        except recommend.RecommendationError as e:
            raise RpcError(BAD_INPUT, str(e)) from None

    @server.method("recommend.history")
    @guarded
    def recommend_history(_ctx: Context, params: dict[str, Any]) -> list[dict[str, Any]]:
        airframe = params.get("airframe_id")
        if not isinstance(airframe, str) or not airframe:
            raise RpcError(INVALID_PARAMS, "airframe_id must be an airframe's id")
        return recommend.history(state.require_store(), airframe)

    @server.method("recommend.export")
    @guarded
    def recommend_export(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        store, cache = state.require_store(), state.cache
        assert cache is not None
        step, kind, lang = params.get("step", 1), params.get("kind"), params.get("lang", "en")
        if not isinstance(step, int) or isinstance(step, bool) or step < 1:
            raise RpcError(INVALID_PARAMS, "step must be a step number, from 1")
        if kind not in ("mission planner", "qgroundcontrol"):
            raise RpcError(INVALID_PARAMS, "kind must be 'mission planner' or 'qgroundcontrol'")
        if lang not in LANGUAGES:
            raise RpcError(INVALID_PARAMS, f"lang must be one of {LANGUAGES}")
        try:
            plan = recommend.flight_plan(store, cache, state.pack, _flight(params))
            return recommend.export_step(
                plan, analyzers.Registry.load(state.pack), step, kind, lang
            )
        except (analyzers.FlightError, recommend.ExportError) as e:
            raise RpcError(BAD_INPUT, str(e)) from None

    def _id(params: dict[str, Any], key: str) -> str:
        value = params.get(key)
        if not isinstance(value, str) or not value:
            raise RpcError(INVALID_PARAMS, f"{key} must be an id")
        return value

    def _metric(params: dict[str, Any], reg: analyzers.Registry) -> tuple[str, str]:
        metric, segment = params.get("metric"), params.get("segment", "flight")
        if not isinstance(metric, str) or metric not in reg.metrics:
            raise RpcError(INVALID_PARAMS, "metric must be a metric in the registry")
        if not isinstance(segment, str):
            raise RpcError(INVALID_PARAMS, "segment must be a segment name")
        return metric, segment

    @server.method("trend.overview")
    @guarded
    def trend_overview(_ctx: Context, params: dict[str, Any]) -> list[dict[str, Any]]:
        reg = analyzers.Registry.load(state.pack)
        return trends.overview(state.require_store(), reg, _id(params, "airframe_id"))

    @server.method("trend.series")
    @guarded
    def trend_series(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        reg = analyzers.Registry.load(state.pack)
        metric, segment = _metric(params, reg)
        payload, any_payload = params.get("payload"), params.get("any_payload", False)
        if payload is not None and not isinstance(payload, str):
            raise RpcError(INVALID_PARAMS, "payload must be a payload label or null")
        if not isinstance(any_payload, bool):
            raise RpcError(INVALID_PARAMS, "any_payload must be true or false")
        return trends.series(
            state.require_store(), reg, _id(params, "airframe_id"), metric, segment,
            payload, any_payload,
        )  # fmt: skip

    @server.method("trend.events")
    @guarded
    def trend_events(_ctx: Context, params: dict[str, Any]) -> list[dict[str, Any]]:
        return trends.events(state.require_store(), _id(params, "airframe_id"))

    @server.method("trend.platform")
    @guarded
    def trend_platform(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        reg = analyzers.Registry.load(state.pack)
        metric, segment = _metric(params, reg)
        return trends.platform(
            state.require_store(), reg, _id(params, "platform_id"), metric, segment
        )

    @server.method("analysis.registry")
    def analysis_registry(_ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
        reg = analyzers.Registry.load(state.pack)
        return {
            "version": reg.version,
            "metrics": {
                m.id: {
                    "name": dict(m.name),
                    "unit": m.unit,
                    "direction": m.direction,
                    "definition": dict(m.definition),
                    "method": dict(m.method),
                    "messages": list(m.messages),
                    "zones": {z: [list(i) for i in b] for z, b in m.zones.items()},
                    "source": dict(m.source),
                }
                for m in reg.metrics.values()
            },
            "findings": {
                f.code: {"title": dict(f.title), "action": dict(f.action), "stage": f.stage}
                for f in reg.findings.values()
            },
            "options": {
                o.id: {
                    "area": o.area,
                    "subsystem": o.subsystem,
                    "title": dict(o.title),
                    "benefit": dict(o.benefit),
                    "how": dict(o.how),
                    "hardware": dict(o.hardware),
                    "cost": o.cost,
                    "sources": [dict(x) for x in o.sources],
                }
                for o in reg.options.values()
            },
            "coverage": {
                sid: {
                    "title": dict(sub["title"]),
                    "options": list(sub["options"]),
                    "covered": bool(sub["options"]),
                }
                for sid, sub in reg.coverage.items()
            },
        }
