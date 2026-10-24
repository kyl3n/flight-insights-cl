"""The AI path's engine methods (spec: AI layer, "Request flow"). Shell-only: none of
them is in the shell's UI allowlist, so the UI can never fetch a flight's tripwire.

| Method | What it does |
| --- | --- |
| ai.prepare | Builds a flight's packet, checks it, records it; returns it with the tripwire |
| ai.record | Records the shell's own check of a packet, and whether it was sent |
| ai.selftest | Runs the scrubber's built-in clean and leaky packets |
| ai.exchange_spec | The static parts of a request: system prompt, tools, reply schema |
| ai.tool | Runs a tool Claude asked for; checks and records its result as a packet |
| ai.accept | Records what came back for a sent packet, checked, with its changes validated |

The UI may read what came of it (in the shell's UI allowlist; no tripwire, no key):

| Method | What it does |
| --- | --- |
| ai.results | A flight's latest exchange: the checked reply and each change's verdict |
| ai.chat_history | A flight's answered chat turns: each question as sent, each answer as accepted |
| ai.sent | What left this machine: every packet as checked, with both verdicts |
| ai.spend | A month's AI spend, for the cap |
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from flight_insights import analyzers
from flight_insights.ai import accept, exchange, record, selftest, tools
from flight_insights.ai.packet import PURPOSES, build
from flight_insights.ai.policy import Policy
from flight_insights.ai.scrubber import check
from flight_insights.ai.tripwire import flight_points
from flight_insights.analyzers.registry import Registry
from flight_insights.rpc.server import BAD_INPUT, INVALID_PARAMS, Context, RpcError, Server
from flight_insights.store.db import StoreError

MONTH = re.compile(r"^\d{4}-\d{2}$")

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
        question = params.get("question")
        if purpose == "chat" and (not isinstance(question, str) or not 0 < len(question) <= 500):
            raise RpcError(INVALID_PARAMS, "a chat packet needs a question of 1 to 500 characters")
        row = store.get("flight", flight_id)
        if row is None:
            raise RpcError(BAD_INPUT, f"no flight {flight_id}")
        ctx.progress(0.0, "building the packet")
        turns = accept.conversation(store, flight_id) if purpose == "chat" else []
        try:
            built = build(store, cache, state.pack, flight_id, purpose, question, turns)
        except analyzers.FlightError as e:
            raise RpcError(BAD_INPUT, str(e)) from None
        packet = built.packet
        points = flight_points(cache, str(row["flight_key"]))
        policy = Policy.load(
            state.pack, str(packet["aircraft"]["firmware"] or state.pack.versions[-1])
        )
        violations = check(packet, points, policy)
        text = record.packet_text(packet)
        kept = record.record_packet(
            store, flight_id, purpose, text, violations,
            run_id=built.run_id, run_key=built.run_key, refs=built.refs,
        )  # fmt: skip
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

    @server.method("ai.exchange_spec")
    def ai_exchange_spec(_ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
        return exchange.spec(state.pack)

    contexts: dict[str, tools.Context] = {}

    def tool_context(flight_id: str) -> tools.Context:
        if flight_id not in contexts:
            store, cache = state.require_store(), state.cache
            assert cache is not None
            try:
                flight = analyzers.FlightData.load(store, cache, state.pack, flight_id)
            except analyzers.FlightError as e:
                raise RpcError(BAD_INPUT, str(e)) from None
            if len(contexts) >= 4:
                contexts.pop(next(iter(contexts)))
            policy = Policy.load(state.pack, flight.firmware or state.pack.versions[-1])
            contexts[flight_id] = tools.Context(
                store, state.pack, flight, policy, Registry.load(state.pack)
            )
        return contexts[flight_id]

    @server.method("ai.tool")
    @guarded
    def ai_tool(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        store, cache = state.require_store(), state.cache
        assert cache is not None
        parent_id, name, inputs = params.get("packet_id"), params.get("name"), params.get("input")
        if not isinstance(parent_id, str) or not isinstance(inputs, dict):
            raise RpcError(INVALID_PARAMS, "packet_id must be a packet's id, input an object")
        if name not in tools.TOOL_NAMES:
            raise RpcError(INVALID_PARAMS, f"no tool {name}")
        parent = store.get("ai_packet", parent_id)
        if parent is None or not parent["sent"] or parent["purpose"] == "tool":
            raise RpcError(BAD_INPUT, "a tool answers only a packet that was sent")
        flight_id = parent["flight_id"]
        if not flight_id:
            raise RpcError(BAD_INPUT, "the packet has no flight to run tools on")
        ctx = tool_context(str(flight_id))
        aircraft = json.loads(parent["packet_json"])["aircraft"]
        result = tools.run(ctx, str(name), cast(dict[str, Any], inputs))
        packet = tools.packet(aircraft["firmware"], aircraft["vehicle"], result)
        flight = store.get("flight", flight_id)
        assert flight is not None
        points = flight_points(cache, str(flight["flight_key"]))
        violations = check(packet, points, ctx.policy)
        if violations:
            # Kept as refused, never sent; Claude is told only that the result is withheld.
            record.record_packet(
                store, flight_id, "tool", record.packet_text(packet), violations,
                parent_id=parent_id,
            )  # fmt: skip
            packet = tools.packet(
                aircraft["firmware"], aircraft["vehicle"],
                {"name": name, "ok": False,
                 "error": "the result was withheld: it did not pass the location check"},
            )  # fmt: skip
            violations = check(packet, points, ctx.policy)
        text = record.packet_text(packet)
        kept = record.record_packet(store, flight_id, "tool", text, violations, parent_id=parent_id)
        reply: dict[str, Any] = {
            "packet_id": kept["id"],
            "engine": {"ok": not violations, "reasons": [asdict(v) for v in violations]},
        }
        if not violations:
            reply |= {"packet_json": text, "sha256": kept["sha256"], "tripwire": points}
        return reply

    @server.method("ai.accept")
    @guarded
    def ai_accept(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        packet_id, model = params.get("packet_id"), params.get("model")
        stop, reply = params.get("stop_reason"), params.get("reply")
        usage: Any = params.get("usage") or {}
        failure = params.get("failure")
        if not isinstance(packet_id, str) or not isinstance(model, str) or not model:
            raise RpcError(INVALID_PARAMS, "packet_id and model are required")
        if not (stop is None or isinstance(stop, str)) or not (
            reply is None or isinstance(reply, str)
        ):
            raise RpcError(INVALID_PARAMS, "stop_reason and reply must be text or null")
        if not isinstance(usage, dict) or not all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0
            for v in cast(dict[str, Any], usage).values()
        ):
            raise RpcError(INVALID_PARAMS, "usage must map token counts to whole numbers")
        cost, calls = params.get("cost_usd", 0.0), params.get("tool_calls", 0)
        if isinstance(cost, bool) or not isinstance(cost, int | float) or cost < 0:
            raise RpcError(INVALID_PARAMS, "cost_usd must be a non-negative number")
        if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
            raise RpcError(INVALID_PARAMS, "tool_calls must be a whole number")
        if not (failure is None or isinstance(failure, str)):
            raise RpcError(INVALID_PARAMS, "failure must be text or null")
        try:
            return accept.accept(
                state.require_store(), state.pack, packet_id, model=model, stop_reason=stop,
                reply_text=reply, usage=cast(dict[str, int], usage), tool_calls=calls,
                cost_usd=float(cost), failure=failure[:200] if failure else None,
            )  # fmt: skip
        except StoreError as e:
            raise RpcError(BAD_INPUT, str(e)) from None

    @server.method("ai.results")
    @guarded
    def ai_results(_ctx: Context, params: dict[str, Any]) -> dict[str, Any] | None:
        flight_id = params.get("flight_id")
        if not isinstance(flight_id, str) or not flight_id:
            raise RpcError(INVALID_PARAMS, "flight_id must be a flight's id")
        return accept.results_for(state.require_store(), flight_id)

    @server.method("ai.chat_history")
    @guarded
    def ai_chat_history(_ctx: Context, params: dict[str, Any]) -> list[dict[str, Any]]:
        flight_id = params.get("flight_id")
        if not isinstance(flight_id, str) or not flight_id:
            raise RpcError(INVALID_PARAMS, "flight_id must be a flight's id")
        return accept.chat_history(state.require_store(), flight_id)

    @server.method("ai.sent")
    @guarded
    def ai_sent(_ctx: Context, params: dict[str, Any]) -> list[dict[str, Any]]:
        limit = params.get("limit", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise RpcError(INVALID_PARAMS, "limit must be a whole number from 1 to 1000")
        return accept.sent(state.require_store(), limit)

    @server.method("ai.spend")
    @guarded
    def ai_spend(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        month = params.get("month") or datetime.now(UTC).strftime("%Y-%m")
        if not isinstance(month, str) or not MONTH.match(month):
            raise RpcError(INVALID_PARAMS, "month must be YYYY-MM")
        return accept.spend(state.require_store(), month)
