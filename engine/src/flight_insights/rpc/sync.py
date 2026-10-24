"""Sync's engine methods (spec: Shared storage and sync). The shell encrypts, signs,
uploads, downloads and checks objects; the engine only turns records into plaintext
objects and applies the ones the shell has checked. Shell-only, except the two the
sync panel reads:

| Method | What it does |
| --- | --- |
| sync.outbox | This laptop's objects not yet sent, oldest first, with pending changes exported |
| sync.sent | The shell wrote an object and the drive confirmed it |
| sync.apply | Applies another laptop's object the shell checked: records, or a source file |
| sync.quarantine | Keeps an object the shell's own checks refused, with the reason |

The UI may read (in the shell's UI allowlist):

| Method | What it does |
| --- | --- |
| sync.status | Waiting to send, applied per laptop with any gaps, the quarantine, conflicts |
| sync.conflicts | Fields two laptops changed before syncing, with both values |
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flight_insights import sync
from flight_insights.rpc.server import BAD_INPUT, INVALID_PARAMS, Context, RpcError, Server
from flight_insights.store.keys import LAPTOP_ID

if TYPE_CHECKING:
    from flight_insights.rpc.methods import EngineState

OBJECT_KEY = re.compile(r"^[0-9a-f]{16,128}$")


def _object(params: dict[str, Any]) -> tuple[str, str, int]:
    key, laptop, sequence = params.get("object_key"), params.get("laptop"), params.get("sequence")
    if not isinstance(key, str) or not OBJECT_KEY.match(key):
        raise RpcError(INVALID_PARAMS, "object_key must be lower-case hex")
    if not isinstance(laptop, str) or not LAPTOP_ID.fullmatch(laptop):
        raise RpcError(INVALID_PARAMS, "laptop must be a laptop ID")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise RpcError(INVALID_PARAMS, "sequence must be a whole number from 1")
    return key, laptop, sequence


def add_sync_methods(server: Server, state: EngineState, guarded: Callable[[Any], Any]) -> None:
    @server.method("sync.outbox")
    @guarded
    def sync_outbox(_ctx: Context, params: dict[str, Any]) -> list[dict[str, Any]]:
        limit = params.get("limit", 50)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise RpcError(INVALID_PARAMS, "limit must be a whole number from 1 to 500")
        return sync.outbox(state.require_store(), limit)

    @server.method("sync.sent")
    @guarded
    def sync_sent(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        key, sequence = params.get("object_key"), params.get("sequence")
        if not isinstance(key, str) or not OBJECT_KEY.match(key):
            raise RpcError(INVALID_PARAMS, "object_key must be lower-case hex")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise RpcError(INVALID_PARAMS, "sequence must be a whole number")
        try:
            sync.mark_sent(state.require_store(), sequence, key)
        except ValueError as e:
            raise RpcError(BAD_INPUT, str(e)) from None
        return {}

    @server.method("sync.apply")
    @guarded
    def sync_apply(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        key, laptop, sequence = _object(params)
        store = state.require_store()
        if laptop == store.laptop_id:
            raise RpcError(BAD_INPUT, "this laptop's own objects are never applied back")
        kind = params.get("kind")
        if kind == "records":
            return sync.apply_records(store, key, laptop, sequence, params.get("payload")).json()
        if kind == "source":
            path = params.get("path")
            if not isinstance(path, str) or not Path(path).is_file():
                raise RpcError(INVALID_PARAMS, "path must be the decrypted file")
            assert state.cache is not None
            return sync.apply_source(
                store, state.cache, state.pack, key, laptop, sequence, params.get("payload"),
                Path(path),
            ).json()  # fmt: skip
        raise RpcError(INVALID_PARAMS, "kind must be records or source")

    @server.method("sync.quarantine")
    @guarded
    def sync_quarantine(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        key, laptop, sequence = _object(params)
        reason = params.get("reason")
        if not isinstance(reason, str) or not 0 < len(reason) <= 300:
            raise RpcError(INVALID_PARAMS, "reason must be 1 to 300 characters")
        return sync.quarantine(state.require_store(), key, laptop, sequence, reason).json()

    @server.method("sync.status")
    @guarded
    def sync_status(_ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
        return sync.status(state.require_store())

    @server.method("sync.conflicts")
    @guarded
    def sync_conflicts(_ctx: Context, params: dict[str, Any]) -> list[dict[str, Any]]:
        return sync.conflicts(state.require_store(), bool(params.get("include_resolved")))
