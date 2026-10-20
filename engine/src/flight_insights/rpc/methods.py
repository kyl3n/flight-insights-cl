"""The methods the shell can call (spec: System architecture, "Engine interface").

| Method | Does |
| --- | --- |
| engine.hello | Versions of the engine, the protocol and the knowledge pack |
| engine.unlock | Opens the encrypted store with the keys the shell read from the keychain |
| engine.shutdown | Closes the store and stops the engine |
| log.summary | What a log holds: message counts, skipped bytes |
| log.analyze | Validation, flights, hover, parameters, fingerprint and archetypes |
| log.columns | Writes chosen columns of a message type to an Arrow IPC file; returns its path |
| files.release | Deletes an Arrow IPC file the shell has finished with |
| source.register | Registers a source file by its content hash; duplicates are reported |
| setup.schema | The guided setup's steps and fields, in every language |
| platform.create / .list / .get | Platforms with their setup answers and completeness |
| platform.save | Saves setup answers on a platform's reference build |
| platform.detect | Answers read from a parameter file or bench log, each with its source |
| airframe.create / .list / .get | Airframes: the reference build plus their own differences |
| airframe.record_change | A new build version with its reason |

Position columns are never written to an IPC file in Phase 0. Keys arrive over
stdin, live in memory only, and are never logged or echoed in an error.
"""

from __future__ import annotations

import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, cast

import pyarrow as pa

from flight_insights import __version__, platforms
from flight_insights.analysis import analyze_log, summary
from flight_insights.dataflash import read_dataflash
from flight_insights.knowledge import Pack, load_pack
from flight_insights.rpc.server import (
    BAD_INPUT,
    CONFLICT,
    INVALID_PARAMS,
    LOCKED,
    Context,
    RpcError,
    Server,
)
from flight_insights.store import ColumnCache, Store, StoreKeys, is_position, open_store
from flight_insights.store.cache import schema_fields
from flight_insights.store.db import StoreError
from flight_insights.store.keys import KEY_BYTES, check_laptop_id, short_code
from flight_insights.store.register import register_source

PROTOCOL = 1
SOURCE_KINDS = ("dataflash", "tlog", "param", "params")


@dataclass
class EngineState:
    pack: Pack
    store: Store | None = None
    cache: ColumnCache | None = None
    board_key: bytes | None = None
    data_dir: Path | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def ipc_dir(self) -> Path:
        if self.data_dir is None:
            raise RpcError(LOCKED, "call engine.unlock first")
        return self.data_dir / "cache" / "ipc"

    def require_store(self) -> Store:
        if self.store is None:
            raise RpcError(LOCKED, "call engine.unlock first")
        return self.store


def _str(params: dict[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise RpcError(INVALID_PARAMS, f"{name} must be a non-empty string")
    return value


def _file(params: dict[str, Any]) -> Path:
    path = Path(_str(params, "path"))
    if not path.is_file():
        raise RpcError(BAD_INPUT, "no such file", {"path": str(path)})
    return path


def _key(keys: dict[str, Any], name: str) -> bytes:
    value = keys.get(name)
    try:
        key = bytes.fromhex(value) if isinstance(value, str) else b""
    except ValueError:
        key = b""
    if len(key) != KEY_BYTES:
        raise RpcError(INVALID_PARAMS, f"keys.{name} must be {KEY_BYTES} bytes in hex")
    return key


def build_server(out: IO[str], pack: Pack | None = None) -> Server:
    state = EngineState(pack or load_pack())
    server = Server(out)

    @server.method("engine.hello")
    def hello(_ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
        manifest = state.pack.manifest
        return {
            "engine": __version__,
            "protocol": PROTOCOL,
            "knowledge": {k: sorted(manifest.get(k, {})) for k in ("params", "enums", "boards")},
            "unlocked": state.store is not None,
        }

    @server.method("engine.unlock", barrier=True)
    def unlock(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        keys = params.get("keys")
        if not isinstance(keys, dict):
            raise RpcError(INVALID_PARAMS, "keys must be an object")
        k = cast(dict[str, Any], keys)
        try:
            laptop_id = check_laptop_id(_str(params, "laptop_id"))
            store_keys = StoreKeys(_key(k, "database"), _key(k, "cache"), _key(k, "positions"))
        except ValueError as e:
            raise RpcError(INVALID_PARAMS, str(e)) from None
        board_key = _key(k, "board_id")
        data_dir = Path(_str(params, "data_dir"))
        with state.lock:
            if state.store is not None:
                raise RpcError(BAD_INPUT, "already unlocked")
            data_dir.mkdir(parents=True, exist_ok=True)
            store = open_store(
                data_dir / "fleet.db", store_keys, laptop_id, params.get("laptop_name")
            )
            state.store, state.board_key, state.data_dir = store, board_key, data_dir
            state.cache = ColumnCache(data_dir / "cache" / "columns", store_keys)
        return {"laptop": short_code(laptop_id), "migrations_applied": store.applied_migrations}

    @server.method("engine.shutdown", barrier=True)
    def shutdown(_ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
        with state.lock:
            if state.store is not None:
                state.store.close()
                state.store = None
            if state.data_dir is not None:
                shutil.rmtree(state.ipc_dir, ignore_errors=True)
        server.stopping.set()
        return {}

    @server.method("log.summary")
    def log_summary(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        with read_dataflash(_file(params)) as df:
            return summary(df)

    @server.method("log.analyze")
    def log_analyze(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        path = _file(params)
        ctx.progress(0.0, "decoding")
        with read_dataflash(path) as df:
            result = analyze_log(path, df, state.pack, state.board_key, ctx.progress)
        ctx.progress(1.0, "done")
        return result

    @server.method("log.columns")
    def log_columns(ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        path, name = _file(params), _str(params, "type")
        wanted = params.get("columns")
        out_dir = state.ipc_dir
        with read_dataflash(path) as df:
            if name not in df.message_types:
                raise RpcError(BAD_INPUT, f"the log has no {name} messages")
            table = df.table(name)
        fields = schema_fields(table.schema)
        hidden = {f.name for f in fields if is_position(f)}
        if wanted is None:
            chosen = [f.name for f in fields if f.name not in hidden]
        elif isinstance(wanted, list) and all(isinstance(c, str) for c in cast(list[Any], wanted)):
            chosen = cast(list[str], wanted)
        else:
            raise RpcError(INVALID_PARAMS, "columns must be a list of names")
        if blocked := sorted(set(chosen) & hidden):
            raise RpcError(BAD_INPUT, "position columns stay in the encrypted cache", blocked)
        if missing := sorted(set(chosen) - {f.name for f in fields}):
            raise RpcError(BAD_INPUT, f"{name} has no column", missing)
        ctx.check()
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{uuid.uuid4().hex}.arrow"
        selected = table.select(chosen)
        with pa.OSFile(str(target), "wb") as sink, pa.ipc.new_file(sink, selected.schema) as w:
            w.write_table(selected)
        return {"path": str(target), "rows": selected.num_rows, "columns": chosen}

    @server.method("files.release")
    def release(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        path = Path(_str(params, "path")).resolve()
        if path.parent != state.ipc_dir.resolve() or path.suffix != ".arrow":
            raise RpcError(BAD_INPUT, "not an engine IPC file")
        path.unlink(missing_ok=True)
        return {}

    @server.method("source.register")
    def source_register(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        path, kind = _file(params), _str(params, "kind")
        if kind not in SOURCE_KINDS:
            raise RpcError(INVALID_PARAMS, f"kind must be one of {', '.join(SOURCE_KINDS)}")
        store = state.require_store()
        with state.lock:
            r = register_source(store, path, kind)
        return {"id": r.id, "sha256": r.sha256, "duplicate": r.duplicate}

    schema = platforms.load_schema(state.pack)

    def guarded(fn: Any) -> Any:
        """Setup and store errors become JSON-RPC errors the UI can show."""

        def run(ctx: Context, params: dict[str, Any]) -> Any:
            try:
                return fn(ctx, params)
            except platforms.SetupError as e:
                raise RpcError(BAD_INPUT, str(e)) from None
            except StoreError as e:
                raise RpcError(CONFLICT, str(e)) from None
            except ValueError as e:
                raise RpcError(INVALID_PARAMS, str(e)) from None

        return run

    def _dict(params: dict[str, Any], name: str) -> dict[str, Any]:
        value = params.get(name)
        if not isinstance(value, dict):
            raise RpcError(INVALID_PARAMS, f"{name} must be an object")
        return cast(dict[str, Any], value)

    def _int(params: dict[str, Any], name: str) -> int:
        value = params.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise RpcError(INVALID_PARAMS, f"{name} must be a whole number")
        return value

    @server.method("setup.schema")
    def setup_schema(_ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
        return schema.raw

    @server.method("platform.create")
    @guarded
    def platform_create(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        store = state.require_store()
        pid = platforms.create_platform(
            store, schema, _str(params, "name"), _str(params, "vehicle")
        )
        return platforms.platform_view(store, schema, pid)

    @server.method("platform.list")
    @guarded
    def platform_list(_ctx: Context, _params: dict[str, Any]) -> list[dict[str, Any]]:
        return platforms.list_platforms(state.require_store(), schema)

    @server.method("platform.get")
    @guarded
    def platform_get(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        return platforms.platform_view(state.require_store(), schema, _str(params, "id"))

    @server.method("platform.save")
    @guarded
    def platform_save(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        return platforms.save_answers(
            state.require_store(),
            schema,
            _str(params, "id"),
            _int(params, "build_version"),
            _dict(params, "values"),
            "detected" if params.get("detected") else "entered",
        )

    @server.method("platform.detect")
    @guarded
    def platform_detect(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        return platforms.detect(_file(params), state.pack)

    @server.method("airframe.create")
    @guarded
    def airframe_create(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        store = state.require_store()
        aid = platforms.create_airframe(store, _str(params, "platform_id"), _str(params, "name"))
        return platforms.airframe_view(store, schema, aid)

    @server.method("airframe.list")
    @guarded
    def airframe_list(_ctx: Context, params: dict[str, Any]) -> list[dict[str, Any]]:
        platform_id = params.get("platform_id")
        if platform_id is not None and not isinstance(platform_id, str):
            raise RpcError(INVALID_PARAMS, "platform_id must be a string")
        return platforms.list_airframes(state.require_store(), schema, platform_id)

    @server.method("airframe.get")
    @guarded
    def airframe_get(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        return platforms.airframe_view(state.require_store(), schema, _str(params, "id"))

    @server.method("airframe.record_change")
    @guarded
    def airframe_record_change(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        reason_kind = _str(params, "reason_kind")
        if reason_kind not in ("failure", "upgrade", "test", "detected"):
            raise RpcError(INVALID_PARAMS, "reason_kind is failure, upgrade, test or detected")
        when = params.get("effective_date")
        if when is not None and not isinstance(when, str):
            raise RpcError(INVALID_PARAMS, "effective_date must be a date like 2026-10-20")
        return platforms.record_change(
            state.require_store(),
            schema,
            _str(params, "id"),
            _dict(params, "changes"),
            reason_kind,
            str(params.get("reason") or ""),
            when,
        )

    return server
