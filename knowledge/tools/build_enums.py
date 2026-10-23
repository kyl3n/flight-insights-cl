#!/usr/bin/env python3
"""Generate the flight-mode and log-event names of one ArduPilot release from its source.

Logs record modes and events as numbers (MODE.ModeNum, EV.Id). Their names come
from the release's own enums, read here from a checkout of the release tag, not
typed by hand:

- Copter modes: `enum class Number` in ArduCopter/mode.h
- Plane modes (QuadPlane included): `enum Number` in ArduPlane/mode.h
- Log events: `enum class LogEvent` in libraries/AP_Logger/AP_Logger.h

Usage (from the repository root):
  python3 knowledge/tools/build_enums.py ARDUPILOT_CHECKOUT VERSION
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PACK = Path(__file__).resolve().parents[1]
SOURCES = {
    ("modes", "copter"): ("ArduCopter/mode.h", r"enum\s+class\s+Number\s*:\s*uint8_t\s*\{"),
    ("modes", "plane"): ("ArduPlane/mode.h", r"enum\s+Number\s*:\s*uint8_t\s*\{"),
    ("events", None): (
        "libraries/AP_Logger/AP_Logger.h",
        r"enum\s+class\s+LogEvent\s*:\s*uint8_t\s*\{",
    ),
}
ENTRY = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(\d+)\s*,", re.MULTILINE)


def enum_values(path: Path, start: str) -> dict[str, str]:
    """Number -> name for every entry of the enum, whatever #if it sits under."""
    text = path.read_text()
    m = re.search(start, text)
    if not m:
        raise SystemExit(f"enum not found in {path}")
    body = text[m.end() : text.index("};", m.end())]
    body = re.sub(r"//[^\n]*", "", body)
    values: dict[str, str] = {}
    for name, number in ENTRY.findall(body):
        if number in values:
            raise SystemExit(f"{path}: {number} is both {values[number]} and {name}")
        values[number] = name
    if not values:
        raise SystemExit(f"no entries read from the enum in {path}")
    return dict(sorted(values.items(), key=lambda kv: int(kv[0])))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="ArduPilot checkout at the release tag")
    parser.add_argument("version", help="release, such as 4.7.2")
    args = parser.parse_args()
    out: dict[str, object] = {"version": args.version, "modes": {}}
    for (kind, vehicle), (rel, start) in SOURCES.items():
        values = enum_values(args.source / rel, start)
        if vehicle is None:
            out[kind] = values
        else:
            out[kind][vehicle] = values  # type: ignore[index]
    commit = subprocess.run(
        ["git", "-C", str(args.source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    rel_out = f"enums/{args.version}.json"
    (PACK / rel_out).parent.mkdir(exist_ok=True)
    (PACK / rel_out).write_text(json.dumps(out, indent=1) + "\n")
    manifest = json.loads((PACK / "manifest.json").read_text())
    manifest.setdefault("enums", {})[args.version] = {
        "ardupilot_commit": commit,
        "generator": "knowledge/tools/build_enums.py",
        "file": rel_out,
    }
    manifest["enums"] = dict(sorted(manifest["enums"].items()))
    (PACK / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {rel_out} from {commit[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
