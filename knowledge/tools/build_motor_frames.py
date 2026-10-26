#!/usr/bin/env python3
"""Generate the motor geometry of every multirotor frame one ArduPilot release mixes,
from its own source.

For each frame class and type, the mixer gives every motor a roll, a pitch, a yaw and a
throttle factor: how much of each axis's demand that motor carries. The motor-out margin
(spec: Propulsion, "Motor-out margin") needs them exactly as the firmware sets them, so
they are read from a checkout of the release tag, never typed by hand:

- frame classes and types: `enum motor_frame_class` and `enum motor_frame_type` in
  libraries/AP_Motors/AP_Motors_Class.h, whose numbers are FRAME_CLASS and FRAME_TYPE;
- the motors: each `setup_<class>_matrix()` in libraries/AP_Motors/AP_MotorsMatrix.cpp,
  which the class's case in `AP_MotorsMatrix::setup_motors()` calls;
- the constants: AP_MOTORS_MATRIX_YAW_FACTOR_CW and _CCW (AP_MotorsMatrix.h), the motor
  numbers AP_MOTORS_MOT_n (AP_Motors_Class.h) and the co-rotating X8 scale factor
  (AP_Motors_config.h).

Each case is replayed as the firmware runs it: `add_motor()` turns an angle into
cos(angle + 90) for roll and cos(angle) for pitch, `add_motor_raw()` takes the factors
as given, a co-rotating layer's loop scales its motors, and `normalise_rpy_factors()`
then scales each axis so its largest factor is 0.5 and throttle so its largest is 1.
Anything the generator does not recognise stops it: a construct a later release adds is
read by a person, never guessed. Cases that only Plane builds compile (QuadPlane frames)
are marked so.

Usage (from the repository root):
  python3 knowledge/tools/build_motor_frames.py ARDUPILOT_CHECKOUT VERSION
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import operator
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PACK = Path(__file__).resolve().parents[1]
CLASS_H = "libraries/AP_Motors/AP_Motors_Class.h"
MATRIX_H = "libraries/AP_Motors/AP_MotorsMatrix.h"
MATRIX_CPP = "libraries/AP_Motors/AP_MotorsMatrix.cpp"
CONFIG_H = "libraries/AP_Motors/AP_Motors_config.h"
PLANE_ONLY = re.compile(r"APM_BUILD_TYPE\(APM_BUILD_ArduPlane\)")
PLACES = 6
NAME = re.compile(r'_frame_type_string\s*=\s*"([^"]+)"\s*;')
ARRAY = re.compile(
    r"static\s+const\s+AP_MotorsMatrix::(MotorDef(?:Raw)?)\s+(\w+)\s*\[\]\s*\{(.*?)\}\s*;", re.S
)
ADD_MOTORS = re.compile(
    r"(add_motors(?:_raw)?)\s*\(\s*(\w+)\s*,\s*ARRAY_SIZE\(\s*\w+\s*\)\s*\)\s*;"
)
ADD_MOTOR = re.compile(r"(add_motor(?:_raw)?)\s*\(([^;]*)\)\s*;")
SCALE = re.compile(
    r"for\s*\(\s*uint8_t\s+i\s*=\s*(\d+)\s*;\s*i\s*<\s*(\d+)\s*;\s*(i\+\+|i\s*\+=\s*\d+)\s*\)"
    r"\s*\{(.*?)\}",
    re.S,
)
STATEMENTS = [
    ("name", NAME),
    ("array", ARRAY),
    ("add_motors", ADD_MOTORS),
    ("add_motor", ADD_MOTOR),
    ("scale", SCALE),
    ("break", re.compile(r"break\s*;")),
    ("refuse", re.compile(r"return\s+false\s*;")),
    ("brace", re.compile(r"[{}]")),
]


class FrameError(ValueError):
    """The source holds something the generator does not know how to read."""


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def enum_values(text: str, name: str) -> dict[str, int]:
    m = re.search(rf"enum\s+{name}\s*\{{(.*?)\}};", text, flags=re.S)
    if m is None:
        raise FrameError(f"no enum {name}")
    out: dict[str, int] = {}
    for entry in m.group(1).split(","):
        entry = entry.strip()
        if entry:
            key, _, value = entry.partition("=")
            out[key.strip()] = int(value.strip())
    return out


def defines(text: str) -> dict[str, float]:
    return {
        m.group(1): float(m.group(2).rstrip("Uf"))
        for m in re.finditer(r"#define\s+(\w+)\s+(-?[\d.]+[Uf]?)\s*$", text, flags=re.M)
    }


OPS: dict[type, Any] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.USub: operator.neg, ast.UAdd: operator.pos,
}  # fmt: skip


def number(expr: str, names: dict[str, float]) -> float:
    """A numeric expression of literals and known macros, nothing else."""
    text = re.sub(r"(?<=\d)[fU]\b", "", expr.strip())

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id in names:
            return names[node.id]
        if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
            return OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            return OPS[type(node.op)](ev(node.left), ev(node.right))
        raise FrameError(f"cannot read the value {expr!r}")

    return ev(ast.parse(text, mode="eval").body)


def function_body(text: str, name: str) -> str:
    m = re.search(rf"bool\s+AP_MotorsMatrix::{name}\s*\([^)]*\)\s*\{{", text)
    if m is None:
        raise FrameError(f"no AP_MotorsMatrix::{name}")
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end() : i - 1]


def plane_only_regions(body: str) -> list[tuple[int, int]]:
    """Spans of the body inside an #if that only Plane builds compile."""
    out: list[tuple[int, int]] = []
    stack: list[tuple[int, bool]] = []
    for m in re.finditer(r"^\s*#\s*(if|ifdef|ifndef|else|elif|endif)\b([^\n]*)$", body, re.M):
        kind, cond = m.group(1), m.group(2)
        if kind.startswith("if"):
            stack.append((m.end(), bool(PLANE_ONLY.search(cond))))
        elif kind == "endif":
            start, plane = stack.pop()
            if plane:
                out.append((start, m.start()))
        else:
            raise FrameError(f"#{kind} inside a frame's setup is not read: {m.group(0).strip()}")
    return out


def cases(body: str) -> list[tuple[list[str], str, bool]]:
    """Each case of the frame-type switch: its labels, its statements, and whether only
    Plane builds compile it."""
    labels = list(re.finditer(r"\b(case\s+(MOTOR_FRAME_TYPE_\w+)|default)\s*:", body))
    plane = plane_only_regions(body)
    out: list[tuple[list[str], str, bool]] = []
    pending: list[str] = []
    for i, m in enumerate(labels):
        name = m.group(2) or "default"
        end = labels[i + 1].start() if i + 1 < len(labels) else len(body)
        statements = body[m.end() : end]
        pending.append(name)
        if not re.sub(r"[\s{}]|#[^\n]*", "", statements):  # falls through to the next label
            continue
        only_plane = any(a <= m.start() < b for a, b in plane)
        out.append((pending, statements, only_plane))
        pending = []
    return out


def replay(statements: str, names: dict[str, float]) -> tuple[str | None, list[dict[str, Any]]]:
    """Run one case's statements as the mixer would: its type name and its motors, or
    None when the case refuses the frame type."""
    motors: dict[int, list[float]] = {}  # number -> [roll, pitch, yaw, throttle, order]
    arrays: dict[str, tuple[str, list[list[float]]]] = {}
    type_name: str | None = None
    text = re.sub(r"#[^\n]*", "", statements)
    text = re.sub(r"static_assert\s*\(.*?\);", "", text, flags=re.S)

    def raw(n: int, r: float, p: float, y: float, order: float, thr: float = 1.0) -> None:
        motors[n] = [r, p, y, thr, order]

    def angles(n: int, roll_deg: float, pitch_deg: float, y: float, order: float) -> None:
        raw(n, math.cos(math.radians(roll_deg + 90)), math.cos(math.radians(pitch_deg)), y,
            order)  # fmt: skip

    pos = 0
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        for kind, pattern in STATEMENTS:
            m = pattern.match(text, pos)
            if m is None:
                continue
            if kind == "name":
                type_name = m.group(1)
            elif kind == "array":
                rows = re.findall(r"\{([^{}]*)\}", m.group(3))
                arrays[m.group(2)] = (m.group(1), [
                    [number(v, names) for v in row.split(",") if v.strip()] for row in rows
                ])  # fmt: skip
            elif kind == "add_motors":
                kind_of, rows = arrays[m.group(2)]
                for i, row in enumerate(rows):
                    if m.group(1) == "add_motors" and kind_of == "MotorDef" and len(row) == 3:
                        angles(i, row[0], row[0], row[1], row[2])
                    elif (
                        m.group(1) == "add_motors_raw"
                        and kind_of == "MotorDefRaw"
                        and len(row) == 4
                    ):
                        raw(i, *row)
                    else:
                        raise FrameError(f"{m.group(1)} on {kind_of} rows of {len(row)}")
            elif kind == "add_motor":
                args = [number(a, names) for a in m.group(2).split(",")]
                n = int(args[0])
                if m.group(1) == "add_motor_raw" and len(args) in (5, 6):
                    raw(n, *args[1:])
                elif m.group(1) == "add_motor" and len(args) == 4:
                    angles(n, args[1], args[1], args[2], args[3])
                elif m.group(1) == "add_motor" and len(args) == 5:
                    angles(n, *args[1:])
                else:
                    raise FrameError(f"{m.group(1)} with {len(args)} arguments")
            elif kind == "scale":
                start, stop = int(m.group(1)), int(m.group(2))
                step = 1 if m.group(3) == "i++" else int(m.group(3).split("=")[1])
                factors = {"_roll_factor": 0, "_pitch_factor": 1, "_yaw_factor": 2,
                           "_throttle_factor": 3}  # fmt: skip
                lines = [s.strip() for s in m.group(4).split(";") if s.strip()]
                for line in lines:
                    sm = re.fullmatch(r"(\w+)\[i\]\s*\*=\s*(\w+)", line)
                    if sm is None or sm.group(1) not in factors:
                        raise FrameError(f"cannot read the loop statement {line!r}")
                    for i in range(start, stop, step):
                        motors[i][factors[sm.group(1)]] *= number(sm.group(2), names)
            elif kind == "refuse":
                return None, []
            elif kind == "break":  # the case ends; what follows is after the switch
                pos = len(text)
                break
            pos = m.end()
            break
        else:
            raise FrameError(f"cannot read {text[pos : pos + 80]!r}")
    if not motors:
        raise FrameError("a case with no motors")
    return type_name, normalise(motors)


def normalise(motors: dict[int, list[float]]) -> list[dict[str, Any]]:
    """AP_MotorsMatrix::normalise_rpy_factors(): roll, pitch and yaw to a largest
    magnitude of 0.5, throttle to a largest of 1 and never below 0."""
    tops = [max(abs(m[i]) for m in motors.values()) for i in range(3)]
    top_thr = max(max(0.0, m[3]) for m in motors.values())
    out: list[dict[str, Any]] = []
    for n in sorted(motors):
        r, p, y, t, order = motors[n]
        r, p, y = (
            0.5 * v / top if top > 1e-6 else v for v, top in zip((r, p, y), tops, strict=True)
        )
        t = max(0.0, t / top_thr) if top_thr > 1e-6 else t
        out.append({
            "motor": n + 1,
            "roll": round(r, PLACES) + 0.0, "pitch": round(p, PLACES) + 0.0,
            "yaw": round(y, PLACES) + 0.0, "throttle": round(t, PLACES) + 0.0,
            "test_order": int(order),
        })  # fmt: skip
    return out


def _type_order(item: tuple[str, Any]) -> tuple[bool, int]:
    return item[0] == "default", int(item[0]) if item[0].isdigit() else 0


def build(root: Path) -> dict[str, Any]:
    class_h = strip_comments((root / CLASS_H).read_text(encoding="utf-8"))
    matrix = (root / MATRIX_CPP).read_text(encoding="utf-8")
    code = strip_comments(matrix)
    names = defines(class_h) | defines((root / MATRIX_H).read_text(encoding="utf-8"))
    names |= defines((root / CONFIG_H).read_text(encoding="utf-8"))
    frame_classes = enum_values(class_h, "motor_frame_class")
    frame_types = enum_values(class_h, "motor_frame_type")
    dispatch = re.search(r"void\s+AP_MotorsMatrix::setup_motors\s*\(.*?\)\s*\{(.*?)\n\}", code,
                         re.S)  # fmt: skip
    if dispatch is None:
        raise FrameError("no AP_MotorsMatrix::setup_motors")
    out: dict[str, Any] = {}
    for cls, func in re.findall(r"case\s+(MOTOR_FRAME_\w+)\s*:\s*success\s*=\s*(\w+)\s*\(",
                                dispatch.group(1)):  # fmt: skip
        body = function_body(code, func)
        cname = re.search(r'_frame_class_string\s*=\s*"([^"]+)"', body)
        types: dict[str, Any] = {}
        for labels, statements, plane in cases(body):
            type_name, motors = replay(statements, names)
            if type_name is None and not motors:
                continue  # the class refuses these types
            for label in labels:
                key = "default" if label == "default" else str(frame_types[label])
                entry: dict[str, Any] = {"name": type_name or label, "motors": motors}
                if plane:
                    entry["vehicles"] = ["plane"]
                types[key] = entry
        out[str(frame_classes[cls])] = {
            "name": cname.group(1) if cname else cls,
            "types": dict(sorted(types.items(), key=_type_order)),
        }
    return dict(sorted(out.items(), key=lambda kv: int(kv[0])))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ardupilot", type=Path)
    parser.add_argument("version")
    args = parser.parse_args()
    root: Path = args.ardupilot
    try:
        frames = build(root)
    except FrameError as e:
        print(f"build_motor_frames: {e}", file=sys.stderr)
        return 1
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True,
        text=True,
    ).stdout.strip()  # fmt: skip
    rel_out = f"frames/{args.version}.json"
    doc = {
        "about": "Motor factors of every multirotor frame ArduPilot's matrix mixer sets up, "
        "by FRAME_CLASS then FRAME_TYPE ('default': any type the class does not name). Each "
        "motor's roll, pitch and yaw factors are normalised to a largest magnitude of 0.5 "
        "and its throttle factor to a largest of 1, as AP_MotorsMatrix::"
        "normalise_rpy_factors() leaves them. Generated by knowledge/tools/"
        "build_motor_frames.py; do not edit.",
        "ardupilot_commit": commit,
        "sources": [CLASS_H, MATRIX_H, MATRIX_CPP, CONFIG_H],
        "classes": frames,
    }
    (PACK / "frames").mkdir(exist_ok=True)
    (PACK / rel_out).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    manifest = json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))
    manifest.setdefault("frames", {})[args.version] = {
        "ardupilot_commit": commit,
        "generator": "knowledge/tools/build_motor_frames.py",
        "file": rel_out,
    }
    manifest["frames"] = dict(sorted(manifest["frames"].items()))
    (PACK / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {rel_out} from {commit[:10]}: {sum(len(c['types']) for c in frames.values())} "
          f"frames in {len(frames)} classes")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
