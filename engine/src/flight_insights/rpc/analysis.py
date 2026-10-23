"""The analysis methods (spec: Analyzers; Flight report).

| Method | Does |
| --- | --- |
| analysis.run | Analyses a flight, or returns its stored analysis while that is current |
| analysis.get | A flight's latest analysis, current or not, or null if it was never analysed |
| analysis.registry | Every metric and finding the analyzers can produce, with its wording |

Results hold metric values, findings with the evidence they rest on, and no
positions: the analyzers read cached columns, where positions stay encrypted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from flight_insights import analyzers
from flight_insights.rpc.server import BAD_INPUT, INVALID_PARAMS, Context, RpcError, Server

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
        try:
            return analyzers.run_analysis(store, cache, state.pack, _flight(params), force=force)
        except analyzers.FlightError as e:
            raise RpcError(BAD_INPUT, str(e)) from None

    @server.method("analysis.get")
    @guarded
    def analysis_get(_ctx: Context, params: dict[str, Any]) -> dict[str, Any] | None:
        reg = analyzers.Registry.load(state.pack)
        return analyzers.analysis_results(state.require_store(), _flight(params), reg)

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
