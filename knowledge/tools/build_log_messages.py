#!/usr/bin/env python3
"""Generate every DataFlash log message one ArduPilot release can write, with each
field's units and type, from the release's own source.

ArduPilot 4.7 writes a message's FMT only when it first logs that message, so no log
shows every message the firmware can write. The location scrubber must classify all of
them (spec: AI layer, "Location scrubber"), so their definitions come from the release's
`Tools/autotest/logger_metadata/parse.py`, run here for Copter (helicopters included) and
Plane (QuadPlanes included), not typed by hand.

Usage (from the repository root):
  python3 knowledge/tools/build_log_messages.py ARDUPILOT_CHECKOUT VERSION
"""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

PACK = Path(__file__).resolve().parents[1]
VEHICLES = {"copter": "Copter", "plane": "Plane"}


def parse(ardupilot: Path, vehicle: str) -> dict[str, dict[str, dict[str, str]]]:
    """message -> field -> {units, type}, for one vehicle."""
    script = ardupilot / "Tools" / "autotest" / "logger_metadata" / "parse.py"
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [sys.executable, str(script), "--vehicle", vehicle],
            cwd=tmp,
            check=True,
            capture_output=True,
        )
        root = ET.parse(Path(tmp) / "LogMessages.xml").getroot()
    out: dict[str, dict[str, dict[str, str]]] = {}
    for message in root.iter("logformat"):
        name = message.get("name")
        if not name:
            continue
        fields: dict[str, dict[str, str]] = {}
        for field in message.findall("./fields/field"):
            fname = field.get("name")
            if fname:
                fields[fname] = {"units": field.get("units") or "", "type": field.get("type") or ""}
        out[name] = fields
    if not out:
        raise SystemExit(f"no log messages read for {vehicle}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="ArduPilot checkout at the release tag")
    parser.add_argument("version", help="release, such as 4.7.2")
    args = parser.parse_args()
    messages: dict[str, dict[str, Any]] = {}
    for vehicle, name in VEHICLES.items():
        for message, fields in parse(args.source, name).items():
            entry = messages.setdefault(message, {"vehicles": [], "fields": {}})
            entry["vehicles"].append(vehicle)
            entry["fields"].update(fields)
    out = {"version": args.version, "messages": dict(sorted(messages.items()))}
    commit = subprocess.run(
        ["git", "-C", str(args.source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    rel_out = f"logs/{args.version}.json.gz"
    path = PACK / rel_out
    path.parent.mkdir(exist_ok=True)
    body = json.dumps(out, indent=0, sort_keys=True, ensure_ascii=False).encode()
    with path.open("wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0, filename="") as gz:
        gz.write(body)
    manifest = json.loads((PACK / "manifest.json").read_text())
    manifest.setdefault("logs", {})[args.version] = {
        "ardupilot_commit": commit,
        "generator": "knowledge/tools/build_log_messages.py",
        "file": rel_out,
    }
    manifest["logs"] = dict(sorted(manifest["logs"].items()))
    (PACK / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {rel_out}: {len(messages)} messages from {commit[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
