"""Text files and the protocol are UTF-8 whatever the computer's locale.

Windows defaults to its ANSI code page (cp1252 in CI), so a text read or write
without an encoding works on Linux and fails there on the first non-ASCII byte,
such as the Ukrainian text in the setup schemas.
"""

from __future__ import annotations

import ast
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

ENGINE = Path(__file__).resolve().parents[1]
SOURCE = ENGINE / "src" / "flight_insights"


BINARY_OPENERS = {"gzip", "pq", "os"}  # modules whose open() is binary or takes no encoding


def _needs_encoding(call: ast.Call) -> bool:
    """A read_text/write_text, or an open() in text mode (no mode, or one without "b")."""
    func = call.func
    if isinstance(func, ast.Attribute):
        name, owner = func.attr, func.value
        if isinstance(owner, ast.Name) and owner.id in BINARY_OPENERS:
            return False
        mode_index = 0  # Path.open(mode)
    elif isinstance(func, ast.Name):
        name, mode_index = func.id, 1  # open(file, mode)
    else:
        return False
    if name in ("read_text", "write_text"):
        return True
    if name != "open":
        return False
    mode = call.args[mode_index] if len(call.args) > mode_index else None
    mode = next((kw.value for kw in call.keywords if kw.arg == "mode"), mode)
    return not (isinstance(mode, ast.Constant) and "b" in str(mode.value))


def test_every_text_read_and_write_names_its_encoding() -> None:
    missing = [
        f"{path.relative_to(ENGINE)}:{node.lineno}"
        for path in sorted(SOURCE.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and _needs_encoding(node)
        and not any(kw.arg == "encoding" for kw in node.keywords)
    ]
    assert not missing, "text I/O without encoding=:\n" + "\n".join(missing)


def test_the_protocol_is_utf8_under_a_legacy_locale(tmp_path: Path) -> None:
    """Non-ASCII names round-trip when the process's own encoding is cp1252."""
    keys = {k: secrets.token_hex(32) for k in ("database", "cache", "positions", "board_id")}
    name = "Важкий X8 №2"
    requests: list[tuple[str, dict[str, Any]]] = [
        ("engine.unlock", {"laptop_id": "c" * 32, "data_dir": str(tmp_path), "keys": keys}),
        ("platform.create", {"name": name, "vehicle": "copter"}),
        ("platform.list", {}),
        ("engine.shutdown", {}),
    ]
    stdin = "".join(
        json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": p}, ensure_ascii=False) + "\n"
        for i, (m, p) in enumerate(requests)
    ).encode("utf-8")  # as the shell sends it: raw UTF-8, not escaped
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    run = subprocess.run(
        [sys.executable, "-m", "flight_insights", "rpc"], input=stdin, capture_output=True,
        cwd=ENGINE, env=env, timeout=120,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr.decode("utf-8", "replace")
    replies = {m["id"]: m for m in map(json.loads, run.stdout.decode("utf-8").splitlines())}
    assert replies[1]["result"]["name"] == name
    assert [p["name"] for p in replies[2]["result"]] == [name]
