"""The import methods (spec: Platforms, "Every import asks which airframe and what
changed").

| Method | Does |
| --- | --- |
| import.scan | Reads a batch of files and proposes an airframe for each log; nothing is stored |
| import.commit | Stores the batch as the person decided; undecided logs go to the hold queue |
| import.held | The flights waiting in the hold queue, with why and their candidates |
| import.resolve | Gives held flights an airframe, existing or new |
| import.unknown | Which of the files found in watched folders are new (the shell's own call) |
| import.ignore | Sets aside files a person chose not to import, so they are not offered again |

A scanned batch stays in the engine's memory until it is committed or the engine
stops; the interface holds only its id.
"""

from __future__ import annotations

import math
import secrets
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from flight_insights import imports, stations
from flight_insights.imports import ConfirmedChange, Decision, Proposal, Scanned
from flight_insights.imports.commit import REASONS
from flight_insights.rpc.server import BAD_INPUT, INVALID_PARAMS, Context, RpcError, Server

if TYPE_CHECKING:
    from flight_insights.rpc.methods import EngineState

MAX_FILES = 500


def _view(item: Scanned, proposal: Proposal) -> dict[str, Any]:
    """What the import dialog shows for a file: no board ID, no positions."""
    return {
        "id": item.id,
        "name": item.path.name,
        "path": str(item.path),
        "kind": item.kind,
        "size": item.size,
        "error": item.error,
        "vehicle": item.vehicle,
        "firmware": item.firmware,
        "board": item.board_name,
        "flights": [
            {
                "number": f.number,
                "key": f.key,
                "duration_s": f.duration_s,
                "boot_utc_us": f.boot_utc_us,
                "arm_us": f.arm_us,
                "known": f.known_id is not None,
            }
            for f in item.flights
        ],
        "boots": len(item.boots),
        "param_writes": len(item.writes),
        "proposal": asdict(proposal),
    }


def _strings(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in cast(list[Any], value)):
        raise RpcError(INVALID_PARAMS, f"{name} must be a list of text")
    return cast(list[str], value)


def _changes(value: Any) -> list[ConfirmedChange]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RpcError(INVALID_PARAMS, "changes must be a list")
    out: list[ConfirmedChange] = []
    for c in cast(list[Any], value):
        if not isinstance(c, dict):
            raise RpcError(INVALID_PARAMS, "each change must be an object")
        d = cast(dict[str, Any], c)
        part, kind, reason = d.get("part"), d.get("reason_kind"), d.get("reason") or ""
        if not isinstance(part, str) or not part or not isinstance(reason, str):
            raise RpcError(INVALID_PARAMS, "a change needs its part and reason as text")
        if kind not in REASONS:
            raise RpcError(INVALID_PARAMS, "reason_kind is failure, upgrade, test or detected")
        out.append(ConfirmedChange(part[:100], kind, reason[:500]))
    return out


def _new_airframe(value: Any) -> tuple[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RpcError(INVALID_PARAMS, "new_airframe must be an object")
    d = cast(dict[str, Any], value)
    platform, name = d.get("platform_id"), d.get("name")
    if not isinstance(platform, str) or not isinstance(name, str):
        raise RpcError(INVALID_PARAMS, "new_airframe needs platform_id and name")
    return platform, name


def _weight(value: Any, most_kg: float) -> float | None:
    """A take-off weight in kilograms, as weighed before the flight."""
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 < value <= most_kg
    ):
        raise RpcError(
            INVALID_PARAMS, f"takeoff_kg must be a weight above 0 and up to {most_kg:g} kg"
        )
    return float(value)


def _decision(value: Any, most_kg: float) -> Decision:
    if not isinstance(value, dict):
        raise RpcError(INVALID_PARAMS, "each decision must be an object")
    d = cast(dict[str, Any], value)
    log, action = d.get("log"), d.get("action", "import")
    if not isinstance(log, str) or action not in ("import", "skip"):
        raise RpcError(INVALID_PARAMS, "a decision needs its log and an action: import or skip")
    airframe, payload, station = d.get("airframe_id"), d.get("payload"), d.get("station_version_id")
    for name, v in (
        ("airframe_id", airframe),
        ("payload", payload),
        ("station_version_id", station),
    ):
        if v is not None and not isinstance(v, str):
            raise RpcError(INVALID_PARAMS, f"{name} must be text")
    return Decision(
        log,
        action,
        airframe,
        _new_airframe(d.get("new_airframe")),
        _changes(d.get("changes")),
        _strings(d.get("packs"), "packs"),
        payload,
        station,
        _weight(d.get("takeoff_kg"), most_kg),
    )


def add_import_methods(server: Server, state: EngineState, guarded: Callable[[Any], Any]) -> None:
    batches: dict[str, tuple[list[Scanned], list[Proposal]]] = {}

    @server.method("import.scan")
    @guarded
    def import_scan(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        paths = [Path(p) for p in _strings(params.get("paths"), "paths")]
        if not paths or len(paths) > MAX_FILES:
            raise RpcError(INVALID_PARAMS, f"paths must name 1 to {MAX_FILES} files")
        missing = [str(p) for p in paths if not p.is_file()]
        if missing:
            raise RpcError(BAD_INPUT, "no such file", {"paths": missing})
        store = state.require_store()
        key = state.board_key or secrets.token_bytes(32)
        batch = imports.scan(
            store, state.pack, key, paths, ctx.progress, ctx.check, state.legacy_board_key
        )
        proposals = imports.propose(store, state.pack, batch)
        batch_id = uuid.uuid4().hex
        batches[batch_id] = (batch, proposals)
        return {
            "batch": batch_id,
            "files": [_view(i, p) for i, p in zip(batch, proposals, strict=True)],
        }

    @server.method("import.commit")
    @guarded
    def import_commit(ctx: Context, params: dict[str, Any]) -> list[dict[str, Any]]:
        batch_id = params.get("batch")
        if not isinstance(batch_id, str) or batch_id not in batches:
            raise RpcError(BAD_INPUT, "no such batch: scan the files again")
        raw: Any = params.get("decisions") or []
        if not isinstance(raw, list):
            raise RpcError(INVALID_PARAMS, "decisions must be a list")
        most_kg = float(state.pack.json("rules/payload.json")["max_takeoff_kg"])
        decisions = [_decision(d, most_kg) for d in cast(list[Any], raw)]
        store, cache = state.require_store(), state.cache
        assert cache is not None
        station = None
        if state.host_hash and (sid := stations.station_for_host(store, state.host_hash)):
            station = (store.get("ground_station", sid) or {}).get("current_version_id")
        with state.lock:  # a second commit of the batch waits here, then finds it gone
            if batch_id not in batches:
                raise RpcError(BAD_INPUT, "this batch is already imported: scan the files again")
            batch, proposals = batches[batch_id]
            results = imports.commit(
                store,
                state.pack,
                cache,
                batch,
                proposals,
                decisions,
                station,
                ctx.progress,
                ctx.check,
            )
            del batches[batch_id]
        return [asdict(r) for r in results]

    @server.method("import.held")
    @guarded
    def import_held(_ctx: Context, _params: dict[str, Any]) -> list[dict[str, Any]]:
        return imports.held(state.require_store())

    @server.method("import.unknown")
    @guarded
    def import_unknown(_ctx: Context, params: dict[str, Any]) -> list[str]:
        paths = [Path(p) for p in _strings(params.get("paths"), "paths")][:MAX_FILES]
        return [str(p) for p in imports.watch.unknown(state.require_store(), paths)]

    @server.method("import.ignore")
    @guarded
    def import_ignore(_ctx: Context, params: dict[str, Any]) -> dict[str, int]:
        paths = [Path(p) for p in _strings(params.get("paths"), "paths")][:MAX_FILES]
        with state.lock:
            return {"ignored": imports.watch.ignore(state.require_store(), paths)}

    @server.method("import.resolve")
    @guarded
    def import_resolve(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        flights = _strings(params.get("flights"), "flights")
        if not flights:
            raise RpcError(INVALID_PARAMS, "flights must name at least one held flight")
        airframe = params.get("airframe_id")
        if airframe is not None and not isinstance(airframe, str):
            raise RpcError(INVALID_PARAMS, "airframe_id must be text")
        store = state.require_store()
        with state.lock:
            aid = imports.resolve(
                store,
                state.pack,
                flights,
                airframe,
                _new_airframe(params.get("new_airframe")),
                _changes(params.get("changes")),
            )
        return {"airframe_id": aid}
