"""The tools Claude may call, run here on the laptop (spec: AI layer, "Tools Claude can
call": all executed locally, all scrubbed).

Each tool answers from what the analyzers already compute, at full rate; only what is
handed back is reduced, for transport. A result becomes a packet of its own (purpose
"tool", shape `ai/schema.py` TOOL_RESULTS) that goes through both location checks and
is recorded before it is sent, exactly like the analysis packet. Times are seconds from
arming. A tool never reads a field the scrubber policy does not clear.

Step response joins the list when the Tune analyzer exists.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import numpy as np

from flight_insights import trends
from flight_insights.ai.packet import number
from flight_insights.ai.policy import Policy
from flight_insights.ai.schema import SCHEMA_ID, TOOL_NAMES
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si
from flight_insights.filters import workbench
from flight_insights.history.persist import load_snapshot
from flight_insights.knowledge import Pack
from flight_insights.spectral.peaks import peaks
from flight_insights.store.db import Store

MAX_BINS = 256
AXES = ("x", "y", "z")
TEXT_MAX = 200


class ToolError(ValueError):
    """The tool cannot answer; the message is shown to Claude, so it never holds data."""


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


# The definitions Claude sees: strict, every property required (null where optional).
DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "spectrum",
        "description": (
            "The gyro noise spectrum of one axis in this flight's steady-hover window, "
            "before the filters or as flown (after them). Call this to look at motor "
            "peaks, harmonics or a noise floor that a finding mentions."
        ),
        "input_schema": _object(
            {
                "axis": {"type": "string", "enum": list(AXES)},
                "stage": {"type": "string", "enum": ["before", "flown"]},
            }
        ),
    },
    {
        "name": "filter_simulation",
        "description": (
            "Simulate the gyro filters on this flight's own pre-filter data with some "
            "parameters changed: noise let through, phase lag and notch count, beside "
            "the flight's own setting. Call this before recommending any harmonic notch "
            "or gyro low-pass change, and recommend only what it supports."
        ),
        "input_schema": _object(
            {
                "changes": {
                    "type": "array",
                    "items": _object(
                        {
                            "name": {"type": "string", "enum": list(workbench.EDITABLE)},
                            "value": {"type": "number"},
                        }
                    ),
                }
            }
        ),
    },
    {
        "name": "parameter_doc",
        "description": (
            "ArduPilot's own documentation for one parameter of this flight's firmware: "
            "meaning, units, range, values or bits, whether it needs a reboot, and the "
            "flight's value and default. Call this before recommending a parameter whose "
            "meaning or range you are not certain of."
        ),
        "input_schema": _object({"name": {"type": "string"}}),
    },
    {
        "name": "metric_history",
        "description": (
            "One metric across this airframe's flights up to this one (0 is this flight, "
            "-1 the one before), with zones and the baseline from its good flights. Call "
            "this to tell a new problem from a long-standing one."
        ),
        "input_schema": _object({"metric": {"type": "string"}, "segment": {"type": "string"}}),
    },
    {
        "name": "segment_stats",
        "description": (
            "Statistics of one logged field (message and field as ArduPilot names them) "
            "between two times in seconds from arming, or over the whole flight when "
            "both are null. Position and location-revealing fields are not available."
        ),
        "input_schema": _object(
            {
                "message": {"type": "string"},
                "field": {"type": "string"},
                "start_s": {"type": ["number", "null"]},
                "end_s": {"type": ["number", "null"]},
            }
        ),
    },
]
assert tuple(d["name"] for d in DEFINITIONS) == TOOL_NAMES
for _d in DEFINITIONS:
    _d["strict"] = True


@dataclass
class Context:
    """One flight, as the tools read it."""

    store: Store
    pack: Pack
    flight: FlightData
    policy: Policy
    registry: Registry
    _bench: Any = field(default=None)
    _bench_loaded: bool = False

    def bench(self) -> workbench.Bench:
        if not self._bench_loaded:
            self._bench = workbench.load(self.flight, self.registry)
            self._bench_loaded = True
        if self._bench is None:
            raise ToolError("this flight has no pre-filter gyro data")
        return cast(workbench.Bench, self._bench)


def _round(x: float | None, step: float = 0.1) -> float | int | None:
    if x is None or not math.isfinite(x):
        return None
    return number(round(x / step) * step)


def _db(psd: Any) -> Any:
    return 10 * np.log10(np.maximum(psd, 1e-12))


def spectrum(ctx: Context, axis: str, stage: str) -> dict[str, Any]:
    if axis not in AXES or stage not in ("before", "flown"):
        raise ToolError("axis must be x, y or z and stage before or flown")
    bench = ctx.bench()
    s = bench.screen.spectrum
    i = AXES.index(axis)
    if stage == "before":
        level = _db(s.psd[i])
    else:
        pred = bench.model.prediction if bench.model is not None else None
        if pred is None or len(pred.freqs) != len(s.freqs):
            raise ToolError("no spectrum after the filters for this flight")
        source = pred.logged if pred.logged is not None else pred.predicted
        level = _db(source[i])
    top = 0.45 * s.rate_hz
    inside = s.freqs <= top
    f, v = s.freqs[inside], level[inside]
    pool = max(1, math.ceil(len(f) / MAX_BINS))  # for transport only: the peaks keep full detail
    n = len(f) // pool * pool
    hz = f[:n].reshape(-1, pool).mean(axis=1)
    db = v[:n].reshape(-1, pool).max(axis=1)
    found = peaks(s, float(f[0]) if len(f) else 0.0, top, axes=(i,)) if stage == "before" else []
    return {
        "axis": axis,
        "stage": stage,
        "segment": str(bench.motors.kind),
        "seconds": _round(s.seconds),
        "rate_hz": _round(s.rate_hz),
        "hz": [_round(float(x)) for x in hz],
        "db": [_round(float(x)) for x in db],
        "peaks": [
            {
                "hz": _round(p.freq_hz),
                "db": _round(p.level_db),
                "prominence_db": _round(p.prominence_db),
            }
            for p in found[:16]
        ],
    }


def _simulated(r: Mapping[str, Any]) -> dict[str, Any]:
    if not r.get("available"):
        return {"available": False, "reason": str(r.get("reason") or "")[:TEXT_MAX]}
    return {
        "available": True,
        "noise_db": _round(r["noise_db"]),
        "lag_deg": _round(r["lag_deg"]),
        "notches": int(r["notches"]),
        "within_budget": bool(r["within_budget"]),
        "centres_hz": [_round(float(c)) for c in r["centres_hz"][:16]],
        "bandwidth_hz": _round(r["bandwidth_hz"]),
        "lowpass_hz": _round(r["lowpass_hz"]),
    }


def filter_simulation(ctx: Context, changes: list[dict[str, Any]]) -> dict[str, Any]:
    values: dict[str, float] = {}
    for ch in changes[:16]:
        name, value = ch.get("name"), ch.get("value")
        if name not in workbench.EDITABLE:
            raise ToolError("only the first harmonic notch and the gyro low-pass can be simulated")
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise ToolError("every value must be a number")
        values[str(name)] = float(value)
    if not values:
        raise ToolError("name at least one parameter to change")
    bench = ctx.bench()
    own = dict(bench.flight.params)
    return {
        "params": {k: number(v) for k, v in values.items()},
        "current": _simulated(workbench.evaluate(bench, own)),
        "candidate": _simulated(workbench.evaluate(bench, own | values)),
    }


def parameter_doc(ctx: Context, name: str) -> dict[str, Any]:
    if not name or len(name) > 16:
        raise ToolError("name a parameter")
    if ctx.policy.parameter_removed(name):
        raise ToolError("that parameter reveals where the aircraft is and is not available")
    row = ctx.store.get("flight", ctx.flight.id)
    assert row is not None
    snapshot = load_snapshot(ctx.store, row["param_snapshot_id"])
    meta = snapshot.dialect(ctx.pack).meta.get(name)
    if meta is None:
        raise ToolError("not a parameter of this flight's firmware")

    def text(key: str, limit: int = TEXT_MAX) -> str | None:
        v = meta.get(key)
        return str(v)[:limit] if v is not None else None

    rng = meta.get("range")
    values = cast(dict[str, Any], meta.get("values") or {})
    bits = cast(dict[str, Any], meta.get("bitmask") or {})
    current = snapshot.values.get(name)
    default = snapshot.defaults.get(name)
    return {
        "name": name,
        "display": text("display"),
        "description": text("description", 600),
        "units": text("units", 24),
        "range": [number(float(x)) for x in rng][:2] if rng else None,
        "increment": number(float(meta["increment"])) if meta.get("increment") else None,
        "values": [
            {"value": number(float(k)), "label": str(v)[:TEXT_MAX]}
            for k, v in sorted(values.items(), key=lambda kv: float(kv[0]))
        ][:64],
        "bits": [
            {"bit": int(k), "label": str(v)[:TEXT_MAX]}
            for k, v in sorted(bits.items(), key=lambda kv: int(kv[0]))
        ][:32],
        "reboot": bool(meta.get("reboot_required")),
        "current": number(float(current)) if current is not None else None,
        "default": number(float(default)) if default is not None else None,
    }


def metric_history(ctx: Context, metric: str, segment: str) -> dict[str, Any]:
    if metric not in ctx.registry.metrics:
        raise ToolError("not a metric the analyzers compute")
    if ctx.flight.airframe_id is None:
        raise ToolError("this flight has no airframe, so no history")
    got = trends.series(ctx.store, ctx.registry, ctx.flight.airframe_id, metric, segment,
                        any_payload=True)  # fmt: skip
    points: list[dict[str, Any]] = got["points"]
    ids = [p["flight"] for p in points]
    if ctx.flight.id not in ids:
        raise ToolError("this flight has no value for that metric and segment")
    upto = ids.index(ctx.flight.id)
    shown = points[max(0, upto - 49) : upto + 1]
    base = got["baseline"]
    return {
        "metric": metric,
        "segment": segment,
        "unit": got["unit"],
        "points": [
            {
                "flight": i - len(shown) + 1,
                "value": number(float(p["value"])),
                "zone": p["zone"],
                "good": bool(p["good"]),
            }
            for i, p in enumerate(shown)
        ],
        "baseline": (
            {"low": number(float(base["low"])), "high": number(float(base["high"]))}
            if base
            else None
        ),
    }


def segment_stats(
    ctx: Context, message: str, field: str, start_s: float | None, end_s: float | None
) -> dict[str, Any]:
    table = ctx.flight.table(message)
    if table is None or field not in table.column_names or "TimeUS" not in table.column_names:
        raise ToolError("this flight's log has no such field")
    meta = si.field_meta(table, field)
    decision = ctx.policy.field(
        message, field, meta.get("unit"), meta.get("position", "0") not in ("0", "")
    )
    if not decision.readable:
        raise ToolError("that field is not available: it can reveal where the aircraft flew")
    arm = ctx.flight.arm_us / 1e6
    flown = (ctx.flight.disarm_us - ctx.flight.arm_us) / 1e6
    lo = 0.0 if start_s is None else float(start_s)
    hi = flown if end_s is None else float(end_s)
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        raise ToolError("start_s must come before end_s")
    try:
        t = si.seconds(table) - arm
        v = si.values(table, field)
    except (TypeError, ValueError, ArithmeticError):
        raise ToolError("that field is not numeric") from None
    sel = (t >= lo) & (t <= hi) & np.isfinite(v)
    picked = v[sel]
    if decision.verdict == "coarsen" and decision.step:  # as Policy.value, for every sample
        picked = np.round(picked / decision.step) * decision.step
    seconds = hi - lo
    out: dict[str, Any] = {
        "message": message,
        "field": field,
        "start_s": _round(lo),
        "end_s": _round(hi),
        "unit": meta.get("unit") or None,
        "seconds": _round(seconds),
        "rate_hz": _round(len(picked) / seconds) if seconds > 0 else None,
    }
    if len(picked) == 0:
        return out | dict.fromkeys(("min", "p05", "p50", "mean", "p95", "max"))
    q = np.percentile(picked, [0, 5, 50, 95, 100])
    stats: dict[str, float] = dict(zip(("min", "p05", "p50", "p95", "max"), q, strict=True))
    stats["mean"] = float(np.mean(picked))
    return out | {k: number(float(x)) for k, x in stats.items()}


def run(ctx: Context, name: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
    """The tool's result as the `tool` object of a packet: ok with its result, or not
    ok with a reason that holds no data."""
    try:
        if name == "spectrum":
            result = spectrum(ctx, str(inputs.get("axis")), str(inputs.get("stage")))
        elif name == "filter_simulation":
            changes = inputs.get("changes")
            if not isinstance(changes, list):
                raise ToolError("changes must be a list")
            result = filter_simulation(ctx, cast(list[dict[str, Any]], changes))
        elif name == "parameter_doc":
            result = parameter_doc(ctx, str(inputs.get("name") or ""))
        elif name == "metric_history":
            result = metric_history(ctx, str(inputs.get("metric")), str(inputs.get("segment")))
        elif name == "segment_stats":
            result = segment_stats(
                ctx, str(inputs.get("message")), str(inputs.get("field")),
                _time(inputs.get("start_s")), _time(inputs.get("end_s")),
            )  # fmt: skip
        else:
            raise ValueError(f"no tool {name}")
    except ToolError as e:
        return {"name": name, "ok": False, "error": str(e)[:TEXT_MAX]}
    return {"name": name, "ok": True, name: result}


def _time(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ToolError("times must be numbers of seconds from arming, or null")
    return float(v)


def packet(firmware: str, vehicle: str, tool: Mapping[str, Any]) -> dict[str, Any]:
    """A tool result as the packet that goes through both checks."""
    return {
        "schema": SCHEMA_ID,
        "purpose": "tool",
        "aircraft": {"vehicle": vehicle, "firmware": firmware},
        "tool": dict(tool),
    }
