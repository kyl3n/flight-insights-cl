"""JSON-RPC 2.0 over stdio, one JSON message per line (spec: System architecture,
"Engine interface").

The shell writes requests to the engine's stdin and reads responses and
notifications from its stdout. Nothing else may reach stdout: while the server
runs, `sys.stdout` points at stderr, so a stray print lands in the log instead
of corrupting the protocol. Logs go to stderr only, and never hold keys.

Requests run on a small thread pool, so a long analysis does not block a quick
call. A handler reports progress with `$/progress` notifications and stops early
when the shell sends `$/cancelRequest` for it. Methods registered as barriers
(unlocking, shutting down) run in order instead: every request already sent
finishes first, and no later request starts until the barrier is done.
"""

from __future__ import annotations

import io
import json
import logging
import math
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import IO, Any, cast

log = logging.getLogger("flight_insights.rpc")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# Application errors (JSON-RPC reserves -32000 to -32099 for the server).
LOCKED = -32001  # the store is not unlocked yet
CANCELLED = -32002
BAD_INPUT = -32003  # the file or arguments cannot be used
CONFLICT = -32004  # the record changed since the caller read it


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class Cancelled(RpcError):
    def __init__(self) -> None:
        super().__init__(CANCELLED, "cancelled")


@dataclass
class Context:
    """What a handler gets besides its parameters."""

    server: Server
    request_id: Any
    cancel: threading.Event = field(default_factory=threading.Event)

    def progress(self, fraction: float, message: str = "") -> None:
        self.check()
        self.server.notify(
            "$/progress", {"id": self.request_id, "fraction": fraction, "message": message}
        )

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled


Handler = Callable[[Context, dict[str, Any]], Any]


def jsonable(value: Any) -> Any:
    """JSON has no NaN or infinity: they become null rather than invalid output."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in cast(dict[Any, Any], value).items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in cast(list[Any], value)]
    return value


class Server:
    def __init__(self, out: IO[str], workers: int = 4) -> None:
        self._out = out
        self._write_lock = threading.Lock()
        self._methods: dict[str, Handler] = {}
        self._barriers: set[str] = set()
        self._pending: set[Future[dict[str, Any] | None]] = set()
        self._pending_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rpc")
        self._running: dict[Any, Context] = {}
        self._running_lock = threading.Lock()
        self.stopping = threading.Event()

    def method(self, name: str, barrier: bool = False) -> Callable[[Handler], Handler]:
        def register(fn: Handler) -> Handler:
            self._methods[name] = fn
            if barrier:
                self._barriers.add(name)
            return fn

        return register

    # --- output -----------------------------------------------------------------

    def _send(self, message: dict[str, Any]) -> None:
        line = json.dumps(jsonable(message), allow_nan=False, separators=(",", ":"))
        with self._write_lock:
            self._out.write(line + "\n")
            self._out.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _error(self, rid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": rid, "error": err}

    # --- input ------------------------------------------------------------------

    def serve(self, lines: IO[str]) -> None:
        """Read requests until stdin closes or the shell asks the engine to stop."""
        for line in lines:
            if not line.strip():
                continue
            self.handle_line(line)
            if self.stopping.is_set():
                break
        self._pool.shutdown(wait=True, cancel_futures=False)

    def handle_line(self, line: str) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            self._send(self._error(None, PARSE_ERROR, "parse error"))
            return
        if isinstance(message, list):
            # A batch: answered together, after every call in it has finished.
            batch: list[Any] = message  # pyright: ignore[reportUnknownVariableType]
            if not batch:
                self._send(self._error(None, INVALID_REQUEST, "empty batch"))
                return
            futures = [self._dispatch(m, batch=True) for m in batch]
            replies = [r for f in futures if f is not None for r in [f.result()] if r is not None]
            if replies:
                with self._write_lock:
                    self._out.write(json.dumps(jsonable(replies), allow_nan=False) + "\n")
                    self._out.flush()
            return
        self._dispatch(message, batch=False)

    def _dispatch(self, message: Any, batch: bool) -> Future[dict[str, Any] | None] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":  # pyright: ignore[reportUnknownMemberType]
            reply = self._error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
            return self._done(reply) if batch else self._send(reply)
        msg: dict[str, Any] = message  # pyright: ignore[reportUnknownVariableType]
        method, rid, is_call = msg.get("method"), msg.get("id"), "id" in msg
        params = msg.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            reply = self._error(rid, INVALID_REQUEST, "method must be a string, params an object")
            return self._done(reply) if batch else self._send(reply)
        if method == "$/cancelRequest":
            with self._running_lock:
                ctx = self._running.get(params.get("id"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            if ctx is not None:
                ctx.cancel.set()
            return None
        handler = self._methods.get(method)
        if handler is None:
            if not is_call:
                return None
            reply = self._error(rid, METHOD_NOT_FOUND, f"no method {method}")
            return self._done(reply) if batch else self._send(reply)
        ctx = Context(self, rid)
        if is_call:
            with self._running_lock:
                self._running[rid] = ctx
        if method in self._barriers:
            self.drain()
            reply = self._run(handler, ctx, params, is_call, batch)  # pyright: ignore[reportUnknownArgumentType]
            return self._done(reply) if batch and reply is not None else None
        fut = self._pool.submit(self._run, handler, ctx, params, is_call, batch)  # pyright: ignore[reportUnknownArgumentType]
        with self._pending_lock:
            self._pending.add(fut)
        fut.add_done_callback(self._forget)
        return fut if batch else None

    def _forget(self, fut: Future[dict[str, Any] | None]) -> None:
        with self._pending_lock:
            self._pending.discard(fut)

    def drain(self) -> None:
        """Wait for every request already started."""
        with self._pending_lock:
            pending = list(self._pending)
        for fut in pending:
            fut.exception()

    def _done(self, reply: dict[str, Any]) -> Future[dict[str, Any] | None]:
        fut: Future[dict[str, Any] | None] = Future()
        fut.set_result(reply)
        return fut

    def _run(
        self, handler: Handler, ctx: Context, params: dict[str, Any], is_call: bool, batch: bool
    ) -> dict[str, Any] | None:
        try:
            result = handler(ctx, params)
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": ctx.request_id, "result": result}
        except RpcError as e:
            reply = self._error(ctx.request_id, e.code, e.message, e.data)
        except Exception as e:
            log.exception("request %r failed", ctx.request_id)
            reply = self._error(ctx.request_id, INTERNAL_ERROR, f"{type(e).__name__}: {e}")
        finally:
            with self._running_lock:
                self._running.pop(ctx.request_id, None)
        if not is_call:
            return None
        if batch:
            return reply
        self._send(reply)
        return None


def run_stdio(build: Callable[[IO[str]], Server]) -> int:
    """Serve on the process's stdin and stdout until stdin closes or shutdown."""
    protocol_out = sys.stdout
    for stream in (sys.stdin, protocol_out):  # the protocol is UTF-8 whatever the locale
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    sys.stdout = sys.stderr  # stray prints must not corrupt the protocol
    try:
        build(protocol_out).serve(sys.stdin)
    finally:
        sys.stdout = protocol_out
    return 0
