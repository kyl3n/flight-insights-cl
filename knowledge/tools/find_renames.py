#!/usr/bin/env python3
"""Find parameter renames between two ArduPilot releases, with proof from the firmware.

ArduPilot stores a parameter by its place in the code's parameter tables (its
group, its index in that group, and the variable behind it), not by its name. A
name that disappears in the newer release while another appears in the same
place, backed by the same variable, is a rename: the stored value carries over
unchanged. This tool proposes a rename only with that proof, read from both
releases' source. Removed or added names without it are listed for a person to
decide; a rescale (a unit change with conversion code) always needs a person,
who records the conversion code as the evidence.

Usage (from the repository root, after build_param_metadata.py for both releases):
  python3 knowledge/tools/find_renames.py OLD_ARDUPILOT NEW_ARDUPILOT OLD_VERSION NEW_VERSION
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path
from typing import Any

PACK = Path(__file__).resolve().parents[1]
RENAMES = PACK / "params" / "renames.json"
SOURCE_DIRS = ["libraries", "ArduCopter", "ArduPlane"]
# AP_GROUPINFO("_SUFFIX", index, Class, variable, default) and its flagged variants,
# and top-level GSCALAR(variable, "NAME", default) style declarations.
GROUPINFO = re.compile(
    r'AP_GROUPINFO\w*\(\s*"(?P<name>[A-Z0-9_]+)"\s*,\s*(?P<index>\d+)\s*,\s*(?P<cls>\w+)\s*,'
    r"\s*(?P<var>[\w.\[\]]+)"
)
SCALAR = re.compile(r'\b[GA]\w*SCALAR\w*\(\s*(?P<var>\w+)\s*,\s*"(?P<name>[A-Z0-9_]+)"')


def metadata(version: str, vehicle: str) -> dict[str, Any]:
    manifest = json.loads((PACK / "manifest.json").read_text())
    with gzip.open(PACK / manifest["params"][version]["files"][vehicle]) as f:
        return json.load(f)


def declarations(source: Path) -> dict[str, list[tuple[str, ...]]]:
    """Declared name or suffix -> where it is stored: (file, class, index, variable)."""
    found: dict[str, list[tuple[str, ...]]] = {}
    for sub in SOURCE_DIRS:
        for path in (source / sub).rglob("*.cpp"):
            text = path.read_text(errors="replace")
            rel = str(path.relative_to(source))
            for m in GROUPINFO.finditer(text):
                where = (rel, m["cls"], m["index"], m["var"])
                found.setdefault(m["name"], []).append(where)
            for m in SCALAR.finditer(text):
                found.setdefault(m["name"], []).append((rel, "top-level", "-", m["var"]))
    return found


def storage(name: str, decls: dict[str, list[tuple[str, ...]]]) -> set[tuple[str, ...]]:
    """Every place a full parameter name could be declared: whole, or as a group suffix."""
    places: set[tuple[str, ...]] = set(decls.get(name, []))
    for i, ch in enumerate(name):
        if ch == "_" and i > 0:
            places |= set(decls.get(name[i:], []))
    return places


def find(
    old_src: Path, new_src: Path, old_v: str, new_v: str
) -> tuple[list[dict[str, Any]], list[str]]:
    old_decls, new_decls = declarations(old_src), declarations(new_src)
    proven: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for vehicle in ("copter", "plane"):
        old, new = metadata(old_v, vehicle), metadata(new_v, vehicle)
        removed, added = sorted(set(old) - set(new)), sorted(set(new) - set(old))
        matched: set[str] = set()
        for gone in removed:
            places = storage(gone, old_decls)
            candidates = [n for n in added if places & storage(n, new_decls)]
            if len(candidates) == 1 and old[gone].get("units") == new[candidates[0]].get("units"):
                place = sorted(places & storage(candidates[0], new_decls))[0]
                matched.add(candidates[0])
                proven.append(
                    {
                        "vehicle": vehicle,
                        "from_version": old_v,
                        "to_version": new_v,
                        "old": gone,
                        "new": candidates[0],
                        "scale": 1.0,
                        "evidence": {
                            "kind": "same storage",
                            "file": place[0],
                            "class": place[1],
                            "index": place[2],
                            "variable": place[3],
                        },
                    }
                )
            else:
                unresolved.append(f"{vehicle}: {gone} removed in {new_v}, no proven rename")
        unresolved += [f"{vehicle}: {n} added in {new_v}" for n in added if n not in matched]
    return proven, unresolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("old_source", type=Path)
    parser.add_argument("new_source", type=Path)
    parser.add_argument("old_version")
    parser.add_argument("new_version")
    args = parser.parse_args()
    proven, unresolved = find(args.old_source, args.new_source, args.old_version, args.new_version)
    table = json.loads(RENAMES.read_text()) if RENAMES.exists() else {"renames": []}
    keep = [
        r
        for r in table["renames"]
        if (r["from_version"], r["to_version"]) != (args.old_version, args.new_version)
        or r["evidence"]["kind"] != "same storage"
    ]
    table["renames"] = keep + proven
    RENAMES.write_text(json.dumps(table, indent=2) + "\n")
    for r in proven:
        print(f"renamed {r['vehicle']}: {r['old']} -> {r['new']} ({r['evidence']['file']})")
    for line in unresolved:
        print(f"review: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
