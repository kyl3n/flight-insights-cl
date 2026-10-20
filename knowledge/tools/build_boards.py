#!/usr/bin/env python3
"""Generate the board table of one ArduPilot release: board name -> MCU, from its hwdef files.

The log names the board in the system-ID message written after VER
("CubeOrange 00350029 3130510B 36383230"): the board's short name, which is
CHIBIOS_SHORT_BOARD_NAME when a hwdef defines it and the hwdef directory's name
otherwise (libraries/AP_HAL/board/chibios.h). Each board's hwdef.dat, followed
through its includes, gives the MCU ("MCU STM32H7xx STM32H743xx") and the
bootloader board ID (APJ_BOARD_ID, resolved through Tools/AP_Bootloader/board_types.txt).
The MCU family (F4, F7, H7, ...) decides what the board can compute in flight.

Usage (from the repository root):
  python3 knowledge/tools/build_boards.py ARDUPILOT_CHECKOUT VERSION
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PACK = Path(__file__).resolve().parents[1]
HWDEF = Path("libraries/AP_HAL_ChibiOS/hwdef")
BOARD_TYPES = Path("Tools/AP_Bootloader/board_types.txt")
MCU_FAMILY = re.compile(r"^(?:STM|CKS)32([A-Z]\d)")


def lines(path: Path, seen: set[Path] | None = None) -> list[str]:
    """A hwdef file's lines with its includes expanded in place."""
    seen = seen if seen is not None else set()
    if path in seen or not path.is_file():
        return []
    seen.add(path)
    out: list[str] = []
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("include "):
            out += lines((path.parent / line.split(None, 1)[1]).resolve(), seen)
        elif line:
            out.append(line)
    return out


def board_ids(source: Path) -> dict[str, int]:
    ids: dict[str, int] = {}
    for line in (source / BOARD_TYPES).read_text().splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[1].isdigit():
            ids[parts[0]] = int(parts[1])
    return ids


def build(source: Path) -> dict[str, dict[str, object]]:
    ids = board_ids(source)
    boards: dict[str, dict[str, object]] = {}
    for hwdef in sorted((source / HWDEF).glob("*/hwdef.dat")):
        name = hwdef.parent.name
        mcu = short = apj = None
        for line in lines(hwdef.resolve()):
            words = line.split()
            if words[0] == "MCU" and len(words) >= 3:
                mcu = words[2]
            elif words[0] == "APJ_BOARD_ID" and len(words) >= 2:
                apj = int(words[1]) if words[1].isdigit() else ids.get(words[1])
            elif words[:2] == ["define", "CHIBIOS_SHORT_BOARD_NAME"] and len(words) >= 3:
                short = words[2].strip('"')
        if mcu is None:
            continue
        family = MCU_FAMILY.match(mcu)
        entry = {
            "hwdef": name,
            "mcu": mcu,
            "family": family[1] if family else None,
            "apj_board_id": apj,
        }
        boards.setdefault(short or name, entry)
    return dict(sorted(boards.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="ArduPilot checkout at the release tag")
    parser.add_argument("version", help="release, such as 4.7.2")
    args = parser.parse_args()
    boards = build(args.source)
    commit = subprocess.run(
        ["git", "-C", str(args.source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    rel = f"boards/{args.version}.json"
    (PACK / rel).parent.mkdir(exist_ok=True)
    (PACK / rel).write_text(
        json.dumps({"version": args.version, "boards": boards}, indent=1) + "\n"
    )
    manifest = json.loads((PACK / "manifest.json").read_text())
    manifest.setdefault("boards", {})[args.version] = {
        "ardupilot_commit": commit,
        "generator": "knowledge/tools/build_boards.py",
        "file": rel,
        "count": len(boards),
    }
    manifest["boards"] = dict(sorted(manifest["boards"].items()))
    (PACK / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {rel}: {len(boards)} boards from {commit[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
