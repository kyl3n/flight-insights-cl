"""Check a frozen engine works: version, the CLI analysis, and a JSON-RPC session.

Usage: python packaging/smoke.py BINARY FIXTURE_LOG KNOWLEDGE_DIR
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    binary, log, knowledge = (Path(a).resolve() for a in sys.argv[1:4])
    env = os.environ | {"FI_KNOWLEDGE_DIR": str(knowledge)}

    version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True)
    print(version.stdout.strip())

    analysis = subprocess.run(
        [binary, "analyze", str(log), "--json"], capture_output=True, text=True, env=env, check=True
    )
    result = json.loads(analysis.stdout)
    assert result["flights"], "no flights found"
    print("analyze:", result["parameters"]["vehicle"], result["parameters"]["firmware"])

    with tempfile.TemporaryDirectory() as data:
        keys = {k: secrets.token_hex(32) for k in ("database", "cache", "positions", "board_id")}
        calls = [
            ("engine.hello", {}),
            ("engine.unlock", {"laptop_id": secrets.token_hex(16), "data_dir": data, "keys": keys}),
            ("log.analyze", {"path": str(log)}),
            ("log.columns", {"path": str(log), "type": "IMU"}),
            ("source.register", {"path": str(log), "kind": "dataflash"}),
            ("engine.shutdown", {}),
        ]
        stdin = "".join(
            json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": p}) + "\n"
            for i, (m, p) in enumerate(calls)
        )
        rpc = subprocess.run(
            [binary, "rpc"], input=stdin, capture_output=True, text=True, env=env, timeout=300
        )
        replies = {m["id"]: m for m in map(json.loads, rpc.stdout.splitlines()) if "id" in m}
        for i, (method, _) in enumerate(calls):
            reply = replies.get(i, {})
            assert "result" in reply, f"{method}: {reply or rpc.stderr[-2000:]}"
            print("rpc:", method, "ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
