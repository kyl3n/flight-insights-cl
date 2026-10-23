"""Parameter files: Mission Planner .param, QGroundControl .params, Methodic Configurator.

- Mission Planner writes one `NAME,VALUE` per line; spaces or a tab also separate
  them in files from other tools, and `#` starts a comment.
- QGroundControl writes tab-separated `vehicle-id component-id NAME VALUE type`
  lines, so CAN peripherals and gimbals keep their own parameters apart.
- Methodic Configurator keeps a vehicle folder of numbered step files
  (`02_imu_temperature_calibration_setup.param`) in Mission Planner format, each
  value usually followed by `# the reason for it`.

Files are untrusted input: a line that cannot be read is reported in `problems`,
never raised, and the rest of the file still counts (spec: Data ingestion).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np

Kind = Literal["mission planner", "qgroundcontrol"]

NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,15}$")  # ArduPilot names: at most 16 characters
STEP_FILE = re.compile(r"^(\d+)_(.+)\.param$", re.IGNORECASE)
# ASCII digits only: str.isdigit() also takes "²", which int() refuses.
ID = re.compile(r"[0-9]{1,10}")
MAV_PARAM_TYPE_REAL32 = 9


@dataclass(frozen=True)
class ParamEntry:
    name: str
    value: float
    text: str  # the value exactly as written
    line: int  # 1-based
    component: tuple[int, int] | None = None  # (vehicle id, component id), QGroundControl only
    type: int | None = None  # MAV_PARAM_TYPE, QGroundControl only
    comment: str = ""  # Methodic Configurator puts the reason for a value here


@dataclass
class ParamFile:
    kind: Kind
    entries: list[ParamEntry] = field(default_factory=list[ParamEntry])
    header: list[str] = field(default_factory=list[str])  # comment lines, without '#'
    problems: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])

    def values(self, component: tuple[int, int] | None = None) -> dict[str, float]:
        """Name -> value, for one component (QGroundControl) or for every entry."""
        return {
            e.name: e.value
            for e in self.entries
            if component is None or e.component in (None, component)
        }


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if np.isfinite(value) else None


def _sniff(text: str) -> Kind:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return "qgroundcontrol" if len(line.split("\t")) >= 5 else "mission planner"
    return "mission planner"


def parse_params(text: str, kind: Kind | None = None) -> ParamFile:
    kind = kind or _sniff(text)
    out = ParamFile(kind)
    seen: dict[tuple[tuple[int, int] | None, str], int] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            out.header.append(line[1:].strip())
            continue
        body, _, comment = line.partition("#")
        component: tuple[int, int] | None = None
        ptype: int | None = None
        if kind == "qgroundcontrol":
            parts = raw.split("\t")
            if len(parts) < 5 or not all(ID.fullmatch(p.strip()) for p in parts[:2]):
                out.problems.append(
                    (number, "not a vehicle-id, component-id, name, value, type line")
                )
                continue
            component = (int(parts[0]), int(parts[1]))
            name, value_text = parts[2].strip(), parts[3].strip()
            type_text = parts[4].strip()
            ptype = int(type_text) if ID.fullmatch(type_text) else None
        else:
            fields = re.split(r"[,\s]+", body.strip(), maxsplit=1)
            if len(fields) != 2:
                out.problems.append((number, "not a NAME,VALUE line"))
                continue
            name, value_text = fields[0], fields[1].strip()
        if not NAME.match(name):
            out.problems.append((number, "not a parameter name"))
            continue
        value = _number(value_text)
        if value is None:
            out.problems.append((number, f"{name}: value is not a finite number"))
            continue
        key = (component, name)
        if key in seen:
            out.problems.append(
                (number, f"{name} repeats line {seen[key]}; the later value counts")
            )
            out.entries = [e for e in out.entries if (e.component, e.name) != key]
        seen[key] = number
        out.entries.append(
            ParamEntry(name, value, value_text, number, component, ptype, comment.strip())
        )
    return out


def read_param_file(path: Path | str) -> ParamFile:
    path = Path(path)
    kind: Kind | None = {".param": "mission planner", ".params": "qgroundcontrol"}.get(
        path.suffix.lower()
    )  # type: ignore[assignment]
    return parse_params(path.read_text(encoding="utf-8", errors="replace"), kind)


def format_value(value: float) -> str:
    """The shortest text that reads back as the same float32, as the autopilot stores it."""
    return np.format_float_positional(np.float32(value), unique=True, trim="-")


def write_mission_planner(values: dict[str, float]) -> str:
    return "".join(f"{name},{format_value(v)}\n" for name, v in sorted(values.items()))


def write_qgroundcontrol(
    values: dict[str, float],
    component: tuple[int, int] = (1, 1),
    types: dict[str, int] | None = None,
    title: str = "",
) -> str:
    sysid, compid = component
    header = f"# Onboard parameters for Vehicle {sysid}\n#\n"
    if title:
        header += f"# {title}\n#\n"
    header += "# Vehicle-Id Component-Id Name Value Type\n"
    types = types or {}
    rows = "".join(
        f"{sysid}\t{compid}\t{name}\t{format_value(v)}\t{types.get(name, MAV_PARAM_TYPE_REAL32)}\n"
        for name, v in sorted(values.items())
    )
    return header + rows


@dataclass(frozen=True)
class MethodicStep:
    number: int
    name: str  # e.g. "imu_temperature_calibration_setup"
    path: Path
    params: ParamFile


def read_methodic_folder(folder: Path | str) -> list[MethodicStep]:
    """A Methodic Configurator vehicle folder's numbered step files, in step order."""
    steps: list[MethodicStep] = []
    for path in Path(folder).iterdir():
        m = STEP_FILE.match(path.name)
        if m and path.is_file():
            steps.append(MethodicStep(int(m.group(1)), m.group(2), path, read_param_file(path)))
    return sorted(steps, key=lambda s: (s.number, s.name))
