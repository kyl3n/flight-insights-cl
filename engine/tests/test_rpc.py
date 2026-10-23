"""The engine's JSON-RPC interface, its CLI, and that it never touches the network."""

from __future__ import annotations

import io
import json
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import numpy as np
import pyarrow as pa
import pytest

from flight_insights.dataflash import read_dataflash
from flight_insights.knowledge import Pack, load_pack
from flight_insights.rpc import build_server
from flight_insights.rpc.server import CANCELLED, LOCKED, METHOD_NOT_FOUND, Context, Server

ENGINE = Path(__file__).resolve().parents[1]
FIXTURES = ENGINE.parent / "tests" / "fixtures" / "sitl"
LOG = FIXTURES / "4.7.0" / "copter-quad-hover.bin"
LAPTOP = "c" * 32


class Output(io.StringIO):
    """Collects what the server writes, so a test can wait for replies."""

    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()

    def write(self, s: str) -> int:
        with self.lock:
            return super().write(s)

    def messages(self) -> list[Any]:  # each a reply, a notification, or a batch list
        with self.lock:
            return [json.loads(x) for x in self.getvalue().splitlines() if x]

    def reply(self, rid: Any, timeout: float = 30) -> dict[str, Any]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for m in self.messages():
                if not isinstance(m, dict):
                    continue
                reply = cast(dict[str, Any], m)
                if reply.get("id") == rid and ("result" in reply or "error" in reply):
                    return reply
            time.sleep(0.01)
        raise TimeoutError(rid)


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


@pytest.fixture
def engine(pack: Pack) -> tuple[Server, Output]:
    out = Output()
    return build_server(out, pack), out


def call(server: Server, out: Output, rid: int, method: str, **params: Any) -> dict[str, Any]:
    server.handle_line(
        json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    )
    return out.reply(rid)


def keys() -> dict[str, str]:
    return {k: secrets.token_hex(32) for k in ("database", "cache", "positions", "board_id")}


def test_hello_and_protocol_errors(engine: tuple[Server, Output]) -> None:
    server, out = engine
    hello = call(server, out, 1, "engine.hello")["result"]
    assert hello["protocol"] == 1 and hello["unlocked"] is False
    assert hello["knowledge"]["params"][:3] == ["4.7.0", "4.7.1", "4.7.2"]
    assert call(server, out, 2, "no.such")["error"]["code"] == METHOD_NOT_FOUND
    server.handle_line("not json")
    server.handle_line(json.dumps({"id": 3, "method": "engine.hello"}))  # no "jsonrpc"
    codes = [m["error"]["code"] for m in out.messages() if "error" in m]
    assert -32700 in codes and -32600 in codes
    server.handle_line(json.dumps({"jsonrpc": "2.0", "method": "engine.hello"}))  # notification
    server.handle_line(
        json.dumps([{"jsonrpc": "2.0", "id": 9, "method": "engine.hello"}, {"jsonrpc": "2.0"}])
    )
    time.sleep(0.2)
    batches: list[list[dict[str, Any]]] = [m for m in out.messages() if isinstance(m, list)]
    assert len(batches) == 1 and [r.get("id") for r in batches[0]] == [9, None]


def test_the_store_needs_unlocking_and_keys_stay_secret(
    engine: tuple[Server, Output], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    server, out = engine
    assert (
        call(server, out, 1, "source.register", path=str(LOG), kind="dataflash")["error"]["code"]
        == LOCKED
    )
    k = keys()
    bad = call(server, out, 2, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path),
               keys=k | {"cache": "zz"})  # fmt: skip
    assert "cache" in bad["error"]["message"]
    ok = call(server, out, 3, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=k)
    assert ok["result"]["laptop"] == "LT-CCCCCC"
    first = call(server, out, 4, "source.register", path=str(LOG), kind="dataflash")["result"]
    again = call(server, out, 5, "source.register", path=str(LOG), kind="dataflash")["result"]
    assert not first["duplicate"] and again["duplicate"] and again["id"] == first["id"]
    call(server, out, 6, "engine.shutdown")
    everything = out.getvalue() + caplog.text
    for value in k.values():
        assert value not in everything


def test_analyze_streams_progress_then_the_result(engine: tuple[Server, Output]) -> None:
    server, out = engine
    result = call(server, out, 7, "log.analyze", path=str(LOG))["result"]
    progress = [m["params"]["fraction"] for m in out.messages() if m.get("method") == "$/progress"]
    assert progress[0] == 0.0 and progress[-1] == 1.0 and progress == sorted(progress)
    assert result["parameters"]["firmware"] == "4.7.0"
    assert result["fingerprint"]["board"]["id_hash"] is None  # SITL, scrubbed
    assert len(result["flights"]) == 1 and result["flights"][0]["key"].startswith("fl1-")


def test_columns_go_through_arrow_files_without_positions(
    engine: tuple[Server, Output], tmp_path: Path
) -> None:
    server, out = engine
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys())
    reply = call(server, out, 2, "log.columns", path=str(LOG), type="GPS")["result"]
    path = Path(reply["path"])
    with pa.memory_map(str(path)) as source:
        table = pa.ipc.open_file(source).read_all()
    assert table.num_rows == reply["rows"] > 0
    assert "Lat" not in table.column_names and "Lng" not in table.column_names
    refused = call(server, out, 3, "log.columns", path=str(LOG), type="GPS", columns=["Lat"])
    assert "position" in refused["error"]["message"]
    call(server, out, 4, "files.release", path=str(path))
    assert not path.exists()
    outside = call(server, out, 5, "files.release", path=str(LOG))
    assert "error" in outside and LOG.exists()
    call(server, out, 6, "engine.shutdown")


def test_a_running_request_can_be_cancelled(engine: tuple[Server, Output]) -> None:
    server, out = engine
    started = threading.Event()

    @server.method("test.slow")
    def slow(ctx: Context, _params: dict[str, Any]) -> None:  # pyright: ignore[reportUnusedFunction]
        started.set()
        while True:
            ctx.check()
            time.sleep(0.01)

    server.handle_line(json.dumps({"jsonrpc": "2.0", "id": "s1", "method": "test.slow"}))
    assert started.wait(5)
    server.handle_line(
        json.dumps({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": "s1"}})
    )
    assert out.reply("s1")["error"]["code"] == CANCELLED


def test_the_cli_analysis_holds_no_positions() -> None:
    run = subprocess.run(
        [sys.executable, "-m", "flight_insights", "analyze", str(LOG), "--json"],
        capture_output=True, text=True, check=True, cwd=ENGINE,
    )  # fmt: skip
    data = json.loads(run.stdout)
    assert data["decode"]["types"]["GPS"] > 0
    with read_dataflash(LOG) as df:
        gps = df.table("GPS")
        lat = np.asarray(gps.column("Lat").to_numpy())
        lng = np.asarray(gps.column("Lng").to_numpy())
    for raw in np.unique(np.concatenate([lat, lng]))[:200]:
        for form in (str(int(raw)), f"{int(raw) / 1e7:.7f}", repr(int(raw) / 1e7)):
            assert form not in run.stdout


NO_NETWORK = r"""
import sys

def guard(event, args):
    if event.startswith(("socket.", "urllib.", "http.", "ftplib.", "smtplib.", "webbrowser.")):
        raise RuntimeError(f"network use: {event}")

sys.addaudithook(guard)
from flight_insights.cli import main
sys.exit(main(["rpc"]))
"""


def test_the_engine_makes_no_network_use(tmp_path: Path) -> None:
    """Every Phase 0 method, run in an engine whose every socket call raises."""
    k = keys()
    requests: list[tuple[str, dict[str, Any]]] = [
        ("engine.hello", {}),
        (
            "engine.unlock",
            {"laptop_id": LAPTOP, "data_dir": str(tmp_path), "keys": k, "host": "GCS-LAPTOP"},
        ),
        ("log.summary", {"path": str(LOG)}),
        ("log.analyze", {"path": str(LOG)}),
        ("log.columns", {"path": str(LOG), "type": "IMU"}),
        ("source.register", {"path": str(LOG), "kind": "dataflash"}),
        ("station.schema", {}),
        ("station.create", {"name": "Field laptop"}),
        ("station.list", {}),
        ("station.detect_software", {}),
        ("engine.shutdown", {}),
    ]
    stdin = "".join(
        json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": p}) + "\n"
        for i, (m, p) in enumerate(requests)
    )
    run = subprocess.run(
        [sys.executable, "-c", NO_NETWORK], input=stdin, capture_output=True, text=True,
        cwd=ENGINE, timeout=300,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr
    replies = {m["id"]: m for m in map(json.loads, run.stdout.splitlines()) if "id" in m}
    assert sorted(replies) == list(range(len(requests)))
    for rid, reply in replies.items():
        assert "result" in reply, (requests[rid][0], reply)
    assert "network use" not in run.stderr
