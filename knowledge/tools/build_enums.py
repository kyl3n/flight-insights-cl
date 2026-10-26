#!/usr/bin/env python3
"""Generate the flight-mode, log-event and error names of one ArduPilot release from its
source.

Logs record modes, events and errors as numbers (MODE.ModeNum, EV.Id, ERR.Subsys and
ERR.ECode, PM.InE). Their names come from the release's own enums, read here from a
checkout of the release tag, not typed by hand:

- Copter modes: `enum class Number` in ArduCopter/mode.h
- Plane modes (QuadPlane included): `enum Number` in ArduPlane/mode.h
- Log events: `enum class LogEvent` in libraries/AP_Logger/AP_Logger.h
- Error subsystems: `enum class LogErrorSubsystem` in the same file
- Why a mode changed (MODE.Rsn): `enum class ModeReason` in
  libraries/AP_Vehicle/ModeReason.h
- How the vehicle was armed or disarmed (ARM.Method): `enum class Method` in
  libraries/AP_Arming/AP_Arming.h
- Error codes: `enum class LogErrorCode`, whose numbers mean different things in
  different subsystems, so each number keeps every name it has
- Internal errors: `enum class error_t` in libraries/AP_InternalError/AP_InternalError.h,
  by bit number, as PM.InE records them

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
    ("error_subsystems", None): (
        "libraries/AP_Logger/AP_Logger.h",
        r"enum\s+class\s+LogErrorSubsystem\s*:\s*uint8_t\s*\{",
    ),
    ("mode_reasons", None): (
        "libraries/AP_Vehicle/ModeReason.h",
        r"enum\s+class\s+ModeReason\s*:\s*uint8_t\s*\{",
    ),
    ("arming_methods", None): (
        "libraries/AP_Arming/AP_Arming.h",
        r"enum\s+class\s+Method\s*\{",
    ),
}
CODES = ("libraries/AP_Logger/AP_Logger.h", r"enum\s+class\s+LogErrorCode\s*:\s*uint8_t\s*\{")
INTERNAL = (
    "libraries/AP_InternalError/AP_InternalError.h",
    r"enum\s+class\s+error_t\s*:\s*uint32_t\s*\{",
)
ENTRY = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(\d+)\s*(?:,|$)", re.MULTILINE)
BIT = re.compile(r"^\s*([a-z][a-z0-9_]*)\s*=\s*\(\s*1U?\s*<<\s*(\d+)\s*\)\s*,", re.MULTILINE)


def enum_body(path: Path, start: str) -> str:
    text = path.read_text()
    m = re.search(start, text)
    if not m:
        raise SystemExit(f"enum not found in {path}")
    body = text[m.end() : text.index("};", m.end())]
    return re.sub(r"//[^\n]*", "", body)


def enum_values(path: Path, start: str) -> dict[str, str]:
    """Number -> name for every entry of the enum, whatever #if it sits under."""
    values: dict[str, str] = {}
    for name, number in ENTRY.findall(enum_body(path, start)):
        if number in values:
            raise SystemExit(f"{path}: {number} is both {values[number]} and {name}")
        values[number] = name
    if not values:
        raise SystemExit(f"no entries read from the enum in {path}")
    return dict(sorted(values.items(), key=lambda kv: int(kv[0])))


def enum_names(path: Path, start: str) -> dict[str, list[str]]:
    """Number -> every name the enum gives it, in source order (LogErrorCode reuses
    numbers across subsystems)."""
    values: dict[str, list[str]] = {}
    for name, number in ENTRY.findall(enum_body(path, start)):
        values.setdefault(number, []).append(name)
    if not values:
        raise SystemExit(f"no entries read from the enum in {path}")
    return dict(sorted(values.items(), key=lambda kv: int(kv[0])))


def enum_bits(path: Path, start: str) -> dict[str, str]:
    """Bit number -> name for a bitmask enum written as (1U << n)."""
    values = {bit: name for name, bit in BIT.findall(enum_body(path, start))}
    if not values:
        raise SystemExit(f"no bits read from the enum in {path}")
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
    out["error_codes"] = enum_names(args.source / CODES[0], CODES[1])
    out["internal_errors"] = enum_bits(args.source / INTERNAL[0], INTERNAL[1])
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
