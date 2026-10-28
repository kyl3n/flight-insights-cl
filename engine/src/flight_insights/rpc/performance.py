"""RPC for the airframe's power model (spec: Power and efficiency analysis, "Range,
endurance and speed"; Engine protocol): its fit, and its points for the power against
airspeed chart as an Arrow IPC file. The points carry airspeed, climb, power, air density,
weight and phase only: no time, no heading, nothing about where."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from flight_insights.performance import airframe as perf
from flight_insights.performance.fit import NAMES
from flight_insights.rpc.server import INVALID_PARAMS, Context, RpcError, Server

if TYPE_CHECKING:
    from flight_insights.rpc.methods import EngineState


def _airframe(params: dict[str, Any]) -> str:
    value = params.get("airframe_id")
    if not isinstance(value, str) or not value:
        raise RpcError(INVALID_PARAMS, "airframe_id must be an airframe's id")
    return value


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
        store, cache = state.require_store(), state.cache
        assert cache is not None
        ctx.progress(0.0, "fitting the power model")
        m = perf.load(store, cache, state.pack, _airframe(params))
        ctx.progress(1.0, "done")
        return summary(m)

    @server.method("airframe.performance_points")
    @guarded
    def performance_points(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        store, cache = state.require_store(), state.cache
        assert cache is not None
        m = perf.load(store, cache, state.pack, _airframe(params))
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
