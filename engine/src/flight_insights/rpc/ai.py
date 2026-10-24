"""The AI path's engine methods (spec: AI layer, "Request flow"). Shell-only: none of
them is in the shell's UI allowlist, so the UI can never fetch a flight's tripwire.

| Method | What it does |
| --- | --- |
| ai.prepare | Builds a flight's packet, checks it, records it; returns it with the tripwire |
| ai.record | Records the shell's own check of a packet, and whether it was sent |
| ai.selftest | Runs the scrubber's built-in clean and leaky packets |
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

from flight_insights import analyzers
from flight_insights.ai import record, selftest
from flight_insights.ai.packet import PURPOSES, build_packet
from flight_insights.ai.policy import Policy
from flight_insights.ai.scrubber import check
from flight_insights.ai.tripwire import flight_points
from flight_insights.rpc.server import BAD_INPUT, INVALID_PARAMS, Context, RpcError, Server

if TYPE_CHECKING:
    from flight_insights.rpc.methods import EngineState


def add_ai_methods(server: Server, state: EngineState, guarded: Callable[[Any], Any]) -> None:
    @server.method("ai.prepare")
    @guarded
    def ai_prepare(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        store, cache = state.require_store(), state.cache
        assert cache is not None
        flight_id, purpose = params.get("flight_id"), params.get("purpose", "analysis")
        if not isinstance(flight_id, str) or not flight_id:
            raise RpcError(INVALID_PARAMS, "flight_id must be a flight's id")
        if purpose not in PURPOSES:
            raise RpcError(INVALID_PARAMS, f"purpose must be one of {', '.join(PURPOSES)}")
        row = store.get("flight", flight_id)
        if row is None:
            raise RpcError(BAD_INPUT, f"no flight {flight_id}")
        ctx.progress(0.0, "building the packet")
        try:
            packet = build_packet(store, cache, state.pack, flight_id, purpose)
        except analyzers.FlightError as e:
            raise RpcError(BAD_INPUT, str(e)) from None
        points = flight_points(cache, str(row["flight_key"]))
        policy = Policy.load(
            state.pack, str(packet["aircraft"]["firmware"] or state.pack.versions[-1])
        )
        violations = check(packet, points, policy)
        text = record.packet_text(packet)
        kept = record.record_packet(store, flight_id, purpose, text, violations)
        reply: dict[str, Any] = {
            "packet_id": kept["id"],
            "engine": {"ok": not violations, "reasons": [asdict(v) for v in violations]},
        }
        if not violations:  # a refused packet goes no further than this laptop's record
            reply |= {"packet_json": text, "sha256": kept["sha256"], "tripwire": points}
        return reply

    @server.method("ai.record")
    @guarded
    def ai_record(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        packet_id, sent = params.get("packet_id"), params.get("sent", False)
        raw: Any = params.get("shell_reasons", [])
        if not isinstance(packet_id, str) or not packet_id:
            raise RpcError(INVALID_PARAMS, "packet_id must be a packet's id")
        if not isinstance(sent, bool) or not isinstance(raw, list):
            raise RpcError(INVALID_PARAMS, "sent must be true or false, shell_reasons a list")
        reasons = [cast(dict[str, str], r) for r in cast(list[Any], raw) if isinstance(r, dict)]
        row = record.record_outcome(state.require_store(), packet_id, reasons, sent)
        return {
            "packet_id": row["id"],
            "shell_verdict": row["shell_verdict"],
            "sent": bool(row["sent"]),
        }

    @server.method("ai.selftest")
    def ai_selftest(_ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
        return selftest.run(Policy.load(state.pack, state.pack.versions[-1]))
