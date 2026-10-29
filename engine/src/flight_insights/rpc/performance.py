"""RPC for the airframe's power model (spec: Power and efficiency analysis, "Range,
endurance and speed"; Engine protocol): its fit, and its points for the power against
airspeed chart as an Arrow IPC file; and the makers' bench tables a person enters for a
platform (`bench.list`, `bench.save`). The points carry airspeed, climb, power, air density,
weight and phase only: no time, no heading, nothing about where."""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import pyarrow as pa

from flight_insights.performance import airframe as perf
from flight_insights.performance import bench
from flight_insights.performance import report as perf_report
from flight_insights.performance.fit import NAMES
from flight_insights.rpc.server import BAD_INPUT, INVALID_PARAMS, Context, RpcError, Server

if TYPE_CHECKING:
    from flight_insights.rpc.methods import EngineState


def _airframe(params: dict[str, Any]) -> str:
    value = params.get("airframe_id")
    if not isinstance(value, str) or not value:
        raise RpcError(INVALID_PARAMS, "airframe_id must be an airframe's id")
    return value


def _platform(params: dict[str, Any]) -> str:
    value = params.get("platform_id")
    if not isinstance(value, str) or not value:
        raise RpcError(INVALID_PARAMS, "platform_id must be a platform's id")
    return value


def _model(state: EngineState, params: dict[str, Any]) -> perf.AirframeModel:
    store, cache = state.require_store(), state.cache
    assert cache is not None
    try:
        return perf.load(store, cache, state.pack, _airframe(params))
    except perf.PerformanceError as e:
        raise RpcError(BAD_INPUT, str(e)) from None


def summary(m: perf.AirframeModel) -> dict[str, Any]:
    """The model as the UI shows it: the fit and its intervals, how well each flight's
    energy is predicted without it, and what each flight gave."""
    flights = [
        {"flight_id": f.flight_id, "points": len(f.points), "mass_kg": f.mass_kg,
         "mass_from": f.mass_from, "wind_from": f.wind_from, "density_from": f.density_from,
         "seconds": round(sum(p.seconds for p in f.points), 1),
         "left_out_s": {k: round(v, 1) for k, v in f.left_out.items()}}
        for f in m.flights
    ]  # fmt: skip
    out: dict[str, Any] = {
        "airframe_id": m.airframe_id, "disc_area_m2": m.disc_area, "coaxial": m.coaxial,
        "flights": flights, "skipped": dict(m.skipped), "why": m.why, "fit": None,
    }  # fmt: skip
    f = m.fitted
    if f is not None:
        coefficients = {n: getattr(f.model, n) for n in (*NAMES, "drag_area")}
        out["fit"] = {
            "level": f.level, "rows": f.rows, "flights": f.flights, "left_out": f.left_out,
            "rms": f.model.rms, "coefficients": coefficients,
            "intervals": {k: list(v) for k, v in f.bootstrap.items()},
            "lofo": dict(f.lofo), "factors": dict(f.model.factors),
        }  # fmt: skip
    return out


def add_performance_methods(server: Server, state: EngineState,
                            guarded: Callable[[Any], Any]) -> None:  # fmt: skip
    @server.method("airframe.performance_model")
    @guarded
    def performance_model(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        ctx.progress(0.0, "fitting the power model")
        m = _model(state, params)
        ctx.progress(1.0, "done")
        return summary(m)

    @server.method("airframe.performance_points")
    @guarded
    def performance_points(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        m = _model(state, params)
        rows = [(i, f.mass_kg, p) for i, f in enumerate(m.flights) for p in f.points]
        columns: dict[str, pa.Array[Any]] = {
            "flight": pa.array([r[0] for r in rows], pa.int32()),
            "phase": pa.array([r[2].phase for r in rows], pa.string()),
            "airspeed_ms": pa.array([r[2].airspeed for r in rows], pa.float64()),
            "climb_ms": pa.array([r[2].climb for r in rows], pa.float64()),
            "power_w": pa.array([r[2].power for r in rows], pa.float64()),
            "density": pa.array([r[2].density for r in rows], pa.float64()),
            "mass_kg": pa.array([r[1] for r in rows], pa.float64()),
            "seconds": pa.array([r[2].seconds for r in rows], pa.float64()),
        }
        table = pa.Table.from_arrays(list(columns.values()), names=list(columns))
        ctx.check()
        state.ipc_dir.mkdir(parents=True, exist_ok=True)
        target = state.ipc_dir / f"{uuid.uuid4().hex}.arrow"
        with pa.OSFile(str(target), "wb") as sink, pa.ipc.new_file(sink, table.schema) as w:
            w.write_table(table)
        return {"path": str(target), "rows": table.num_rows,
                "flights": [f.flight_id for f in m.flights]}  # fmt: skip

    @server.method("airframe.performance")
    @guarded
    def performance(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        store, cache = state.require_store(), state.cache
        assert cache is not None
        kg = params.get("mass_kg")
        if kg is not None and (isinstance(kg, bool) or not isinstance(kg, int | float)
                               or not math.isfinite(kg) or not 0 < kg <= 1000):  # fmt: skip
            raise RpcError(INVALID_PARAMS, "mass_kg must be a weight above 0 kg")
        air = params.get("air", "usual")
        if air not in ("usual", "standard"):
            raise RpcError(INVALID_PARAMS, "air must be 'usual' or 'standard'")
        trip = params.get("trip", "out_and_back")
        if trip not in ("out_and_back", "drop", "site"):
            raise RpcError(INVALID_PARAMS, "trip must be 'out_and_back', 'drop' or 'site'")
        wind = params.get("wind_ms", 0.0)
        if isinstance(wind, bool) or not isinstance(wind, int | float) or not 0 <= wind <= 30:
            raise RpcError(INVALID_PARAMS, "wind_ms must be from 0 to 30 m/s")
        ctx.progress(0.0, "working out the figures")
        try:
            out = perf_report.report(store, cache, state.pack, _airframe(params),
                                     None if kg is None else float(kg), air, trip,
                                     float(wind))  # fmt: skip
        except perf.PerformanceError as e:
            raise RpcError(BAD_INPUT, str(e)) from None
        ctx.progress(1.0, "done")
        return out

    @server.method("airframe.range_card")
    @guarded
    def range_card(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        store, cache = state.require_store(), state.cache
        assert cache is not None
        ctx.progress(0.0, "working out the card")
        try:
            out = perf_report.range_card(store, cache, state.pack, _airframe(params))
        except perf.PerformanceError as e:
            raise RpcError(BAD_INPUT, str(e)) from None
        ctx.progress(1.0, "done")
        return out

    @server.method("bench.list")
    @guarded
    def bench_list(_ctx: Context, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [b.view() for b in bench.benches(state.require_store(), _platform(params))]

    @server.method("bench.save")
    @guarded
    def bench_save(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        values = params.get("values")
        if not isinstance(values, dict):
            raise RpcError(INVALID_PARAMS, "values must be an object")
        bench_id, version = params.get("id"), params.get("version")
        if bench_id is not None and not isinstance(bench_id, str):
            raise RpcError(INVALID_PARAMS, "id must be a bench table's id")
        if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
            raise RpcError(INVALID_PARAMS, "version must be the table's version as read")
        try:
            got = bench.save(state.require_store(), _platform(params),
                             cast(dict[str, Any], values), bench_id, version)  # fmt: skip
        except bench.BenchError as e:
            raise RpcError(BAD_INPUT, str(e)) from None
        return got.view()
