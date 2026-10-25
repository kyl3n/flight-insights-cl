#!/usr/bin/env python3
"""Generate the parameter gates of one ArduPilot release from its source: which
parameter switches a feature's other parameters into view, and whether they appear
after a parameter refresh or only after a reboot.

ArduPilot hides parameters in two ways, both read here from a checkout of the release
tag, never guessed from names:

- An 8-bit parameter declared with `AP_PARAM_FLAG_ENABLE` (`INS_HNTCH_ENABLE`,
  `BATT2_MONITOR`, `RNGFND1_TYPE`, `ARSPD_ENABLE`...) hides everything after it in its
  group, nested groups included, while it is 0 (`AP_Param::next` with `skip_disabled`).
  A wider one with the flag (`SERVO_FTW_MASK`, an `AP_Int32`) hides nothing, so each
  flag's declared type is read from the library's headers. Setting a flag changes the
  parameter count, so the hidden parameters appear when a ground station refreshes its
  list; no reboot is needed for them to show. Logs leave hidden parameters out too
  (`AP_Param::next_scalar`).
- A backend's own parameters (`AP_SUBGROUPVARPTR`, `backend_var_info`) exist only once
  the backend has been created at boot from its type parameter, so they appear only
  after a reboot. Their gate is the instance's own flagged type parameter
  (`BATT2_MONITOR` for `BATT2_AMP_PERVLT`).

Parameters are found the way ArduPilot's param_parse.py finds them: from the vehicle's
Parameters.cpp and AP_Vehicle.cpp, following each `@Group` to its `@Path` files, with
`@Param{Vehicle}` docs kept only for the vehicle. Only names in the release's own
parameter metadata (built by build_param_metadata.py) are written, so a parameter
compiled out of every board is never listed.

Usage (from the repository root):
  python3 knowledge/tools/build_param_gates.py ARDUPILOT_CHECKOUT VERSION
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

PACK = Path(__file__).resolve().parents[1]
VEHICLES = {"copter": ("ArduCopter", "Copter"), "plane": ("ArduPlane", "Plane")}
After = Literal["refresh", "reboot"]
KINDS = ("param", "group", "path", "flags", "sub", "end")
EIGHT_BIT = ("AP_Int8", "AP_Enum")  # AP_Param hides a group only behind an AP_PARAM_INT8
BACKEND = "AP_SUBGROUPVARPTR"  # a backend's var_info: created at boot from its type

EVENT = re.compile(
    r"//\s*@Param(?:\{(?P<pveh>[^}]*)\})?:\s*(?P<param>\w+)"
    r"|//\s*@Group:\s*(?P<group>\w*)"
    r"|//\s*@Path:\s*(?P<path>\S+)"
    r"|\b(?P<flags>AP_GROUPINFO_FLAGS\w*)\s*\("
    r"|\b(?P<sub>AP_SUBGROUP(?:VARPTR|PTR|INFO|EXTENSION)|GOBJECT(?:VARPTR|PTR|N)?"
    r"|AP_NESTEDGROUPINFO)\s*\("
    r"|\b(?P<end>AP_GROUPEND)\b"
)


@dataclass(frozen=True)
class Gate:
    gate: str  # the parameter that switches this one into view
    after: After  # what makes it appear once the gate is set


@dataclass
class Walk:
    root: Path
    vehicle: str  # "Copter" or "Plane", as @Param{...} tags name it
    gates: dict[str, Gate] = field(default_factory=dict[str, Gate])
    flags: set[str] = field(default_factory=set[str])  # every flagged parameter
    dynamic: dict[str, str] = field(default_factory=dict[str, str])  # param -> backend prefix
    seen: set[tuple[Path, str]] = field(default_factory=set[tuple[Path, str]])

    def file(self, path: Path, prefix: str, gate: Gate | None, backend: str | None) -> None:
        """Every parameter `path` documents, named under `prefix`; `gate` hides them
        all (from an enclosing group), `backend` is set inside a backend's group."""
        key = (path.resolve(), prefix)
        if key in self.seen or not path.exists():
            return
        self.seen.add(key)
        text = path.read_text(encoding="utf-8", errors="replace")
        current = gate  # what hides the next item in this group
        last_param: str | None = None
        pending: list[tuple[str, list[str]]] = []
        for kind, m in _events(text):
            if kind == "param":
                vehicles = m.group("pveh")
                if vehicles and self.vehicle not in [v.strip() for v in vehicles.split(",")]:
                    last_param = None
                    continue
                last_param = prefix + m.group("param")
                self._param(last_param, current, backend)
            elif kind == "group":
                pending.append((m.group("group"), []))
            elif kind == "path" and pending:
                pending[-1][1].extend(p.strip() for p in m.group("path").split(",") if p.strip())
            elif kind == "flags":
                args = _call_args(text, m.end())
                hides = "AP_PARAM_FLAG_ENABLE" in args and _eight_bit(path.parent, args)
                if hides and last_param is not None:
                    self.flags.add(last_param)
                    after: After = current.after if current is not None else "refresh"
                    current = Gate(last_param, after)
            elif kind == "sub":
                dynamic = m.group("sub") == BACKEND
                for name, paths in pending:
                    inner = current
                    if dynamic and inner is not None:
                        inner = Gate(inner.gate, "reboot")
                    for p in paths:
                        self.file(
                            (path.parent / p).resolve(),
                            prefix + name,
                            inner,
                            prefix + name if dynamic else backend,
                        )
                pending = []
            elif kind == "end":
                current, last_param, pending = gate, None, []

    def _param(self, name: str, gate: Gate | None, backend: str | None) -> None:
        if gate is not None and gate.gate != name:
            self.gates.setdefault(name, gate)
        if backend is not None:
            self.dynamic.setdefault(name, backend)


def _events(text: str) -> Iterator[tuple[str, re.Match[str]]]:
    for m in EVENT.finditer(text):
        kind = next(k for k in KINDS if m.group(k) is not None)
        yield kind, m


def _eight_bit(directory: Path, args: str) -> bool:
    """Whether a flagged parameter's member (the macro's fourth argument) is declared as
    an 8-bit type in the library's headers; True when the declaration is not found."""
    parts = _split_args(args)
    if len(parts) < 4:
        return True
    names = re.findall(r"\w+", parts[3].split("[")[0])
    if not names:
        return True
    declaration = re.compile(
        rf"\b(AP_\w+)(?:<[^>;]*>)?\s+{re.escape(names[-1])}\s*(?:\[[^\]]*\])?\s*;"
    )
    for header in sorted(directory.glob("*.h")):
        m = declaration.search(header.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1) in EIGHT_BIT
    return True


def _split_args(args: str) -> list[str]:
    """A macro's arguments, split at the commas outside brackets."""
    parts: list[str] = []
    depth, start = 0, 0
    for i, c in enumerate(args):
        if c in "(<[":
            depth += 1
        elif c in ")>]":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(args[start:i].strip())
            start = i + 1
    parts.append(args[start:].strip())
    return parts


def _call_args(text: str, start: int) -> str:
    """The text of a macro call's arguments, from just after its opening bracket."""
    depth = 1
    i = start
    while i < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    return text[start : i - 1]


def gates_for(root: Path, vehicle: str, known: set[str]) -> dict[str, dict[str, str]]:
    """{param: {"gate", "after"}} for one vehicle, limited to the `known` parameters."""
    directory, truename = VEHICLES[vehicle]
    walk = Walk(root, truename)
    walk.file(root / directory / "Parameters.cpp", "", None, None)
    walk.file(root / "libraries" / "AP_Vehicle" / "AP_Vehicle.cpp", "", None, None)
    out: dict[str, dict[str, str]] = {}
    for name in sorted(known):
        gate = walk.gates.get(name)
        backend = walk.dynamic.get(name)
        if backend is not None and name not in walk.flags:
            # a backend's parameter: its own instance's type creates it, at boot
            own = sorted(
                f for f in walk.flags if f.startswith(backend) and "_" not in f[len(backend) :]
            )
            if own:
                gate = Gate(own[0], "reboot")
            elif gate is not None:
                gate = Gate(gate.gate, "reboot")
        if gate is None or gate.gate not in known:
            continue
        out[name] = {"gate": gate.gate, "after": gate.after}
    return out


def _known(version: str, vehicle: str) -> set[str]:
    path = PACK / "params" / version / f"{vehicle}.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return set(json.load(f))


def write_gzip_json(path: Path, data: object) -> None:
    body = json.dumps(data, indent=0, sort_keys=True, ensure_ascii=False).encode()
    with path.open("wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0, filename="") as gz:
        gz.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ardupilot", type=Path, help="a checkout of the release tag")
    parser.add_argument("version", help="the release, e.g. 4.7.0")
    args = parser.parse_args()
    root: Path = args.ardupilot.resolve()
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    data = {v: gates_for(root, v, _known(args.version, v)) for v in VEHICLES}
    rel = f"params/{args.version}/gates.json.gz"
    write_gzip_json(PACK / rel, data)
    manifest = json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))
    manifest.setdefault("gates", {})[args.version] = {
        "ardupilot_commit": commit,
        "generator": "knowledge/tools/build_param_gates.py",
        "file": rel,
        "counts": {vehicle: len(gates) for vehicle, gates in data.items()},
    }
    manifest["gates"] = dict(sorted(manifest["gates"].items()))
    (PACK / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for vehicle, gates in data.items():
        reboot = sum(1 for g in gates.values() if g["after"] == "reboot")
        print(f"{vehicle}: {len(gates)} gated parameters, {reboot} after a reboot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
