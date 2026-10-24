"""The engine's location scrubber (spec: AI layer, "Location scrubber: deny by default").

A packet leaves only if all of these hold:

1. **Shape.** It fits the packet schema exactly (`ai/schema.py`): no key or text the
   schema does not name.
2. **Parameters.** None of the parameters it holds or names is one the rules remove.
3. **Detectors.** No string holds a coordinate in any common notation (decimal-degree
   pairs, degrees-minutes-seconds, hemisphere letters, UTM, MGRS, geohash, plus codes,
   "lat"/"lon" labels), and no number has the form of one: an integer as large as a
   latitude or longitude scaled by 1e7, or a value in the range of degrees with more
   decimals than any packet needs.
4. **Tripwire.** No number, in the packet or inside its text, matches this flight's own
   coordinates: one value within about 5 m of a latitude or longitude (in degrees,
   x1e7, x1e5 or radians), or a latitude and a longitude of the same point both within
   0.01°. The tripwire holds the flight's home, origin and track, read on the laptop;
   it is passed in, never stored, and never part of a packet.

A refusal names where and which rule, never the value, so it can be shown and logged.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, cast

import numpy as np

from flight_insights.ai.policy import Policy
from flight_insights.ai.schema import validate

Point = tuple[float, float]  # latitude, longitude in degrees

FINE_DEG = 5e-5  # about 5 m: one value this close to a coordinate of the flight leaks it
PAIR_DEG = 0.01  # a latitude and a longitude of one point both this close leak it
MAX_DECIMALS = 4  # numbers in the range of degrees never need more
SCALED_MIN = 1e7  # an integer this large is in the form of a coordinate x1e7

# A decimal comma only where no thousands group can be meant: four or more digits after it.
DECIMAL = r"(?:\.\d+|,\d{4,})"
DECIMAL3 = r"(?:\.\d{3,}|,\d{4,})"
NUMBER = re.compile(rf"[-+]?\d+{DECIMAL}?")
DETECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("degrees-minutes", re.compile(r"\d{1,3}\s*[°º]\s*\d{1,2}\s*['\u2032\u2019]")),
    # "47° N", "47.123 N"; not "950 W" or "5 N", which are watts and newtons
    (
        "hemisphere",
        re.compile(rf"\b\d{{1,3}}{DECIMAL}?\s*[°º]\s*[NSEW]\b|\b\d{{1,3}}{DECIMAL3}\s*[NSEW]\b"),
    ),
    ("degree pair", re.compile(rf"[-+]?\d{{1,2}}{DECIMAL3}\s*[,;/ ]\s*[-+]?\d{{1,3}}{DECIMAL3}")),
    ("lat/lon label", re.compile(r"(?i)\b(lat|lon|lng|latitude|longitude)\b\s*[:=]?\s*[-+]?\d")),
    ("UTM", re.compile(r"\b\d{1,2}\s?[C-HJ-NP-X]\s+\d{6}(?:\.\d+)?\s*m?\s*E?\s+\d{7}")),
    ("plus code", re.compile(r"\b[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3}\b")),
)
MGRS = re.compile(r"\b\d{1,2}[C-HJ-NP-X]\s?[A-HJ-NP-Z]{2}\s?(\d{2,5})\s?(\d{2,5})\b")
GEOHASH = re.compile(r"\b[0-9bcdefghjkmnpqrstuvwxyz]{6,12}\b")


@dataclass(frozen=True)
class Violation:
    path: str  # where in the packet, as a JSON path
    rule: str  # which check refused it; never the value


class ScrubError(ValueError):
    def __init__(self, violations: Sequence[Violation]) -> None:
        self.violations = list(violations)
        super().__init__(f"the packet was refused: {len(self.violations)} problems")


def _walk(value: Any, path: str = "$") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for k, v in cast(dict[str, Any], value).items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(cast(list[Any], value)):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, value


def _decimals(text: str) -> int:
    text = text.replace(",", ".")
    return len(text.split(".", 1)[1].rstrip("0")) if "." in text else 0


def _number_rules(x: float, decimals: int) -> str | None:
    if not math.isfinite(x):
        return None
    if abs(x) >= SCALED_MIN and float(x).is_integer() and abs(x) <= 1.81e9:
        return "a number in the form of a scaled coordinate"
    if 1.0 <= abs(x) <= 180.0 and decimals > MAX_DECIMALS:
        return "more precision than a packet needs, in the range of degrees"
    return None


def _string_rules(text: str) -> str | None:
    for name, pattern in DETECTORS:
        if pattern.search(text):
            return f"a coordinate in text ({name})"
    for m in MGRS.finditer(text):
        if len(m.group(1)) == len(m.group(2)):
            return "a coordinate in text (MGRS)"
    for m in GEOHASH.finditer(text):
        word = m.group(0)
        if any(c.isdigit() for c in word) and any(c.isalpha() for c in word):
            return "a coordinate in text (geohash)"
    return None


def _numbers(packet: Any) -> list[tuple[str, float, int]]:
    """Every number in the packet, its own or written inside text, with its decimals."""
    out: list[tuple[str, float, int]] = []
    for path, value in _walk(packet):
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int | float):
            out.append((path, float(value), _decimals(repr(float(value)))))
        elif isinstance(value, str):
            for m in NUMBER.finditer(value):
                token = m.group(0)
                out.append((path, float(token.replace(",", ".")), _decimals(token)))
    return out


def _tripwire(numbers: list[tuple[str, float, int]], points: Sequence[Point]) -> list[Violation]:
    if not points or not numbers:
        return []
    values = np.array([n[1] for n in numbers], dtype=np.float64)
    order = np.argsort(values)
    ordered = values[order]

    def near(target: float, tol: float) -> np.ndarray[Any, Any]:
        lo, hi = np.searchsorted(ordered, [target - tol, target + tol])
        return order[lo:hi]

    out: list[Violation] = []
    seen: set[int] = set()
    coords = {c for p in points for c in p}
    for c in coords:  # one value too close to any coordinate of the flight
        for form, scale, tol in (("degrees", 1.0, FINE_DEG), ("x1e7", 1e7, FINE_DEG * 1e7),
                                 ("x1e5", 1e5, FINE_DEG * 1e5),
                                 ("radians", math.pi / 180, FINE_DEG * math.pi / 180)):  # fmt: skip
            for i in near(c * scale, tol):
                if int(i) not in seen:
                    seen.add(int(i))
                    out.append(
                        Violation(numbers[int(i)][0], f"matches this flight's position ({form})")
                    )
    for lat, lon in points:  # a latitude and a longitude of one point
        a = [int(i) for i in near(lat, PAIR_DEG)]
        b = [int(i) for i in near(lon, PAIR_DEG)]
        pair = next(((i, j) for i in a for j in b if i != j), None)  # two different values
        if pair is not None:
            for i in pair:
                if i not in seen:
                    seen.add(i)
                    out.append(
                        Violation(
                            numbers[i][0], "with another value, matches a point of this flight"
                        )
                    )
    return out


def _parameter_names(packet: Any) -> Iterator[tuple[str, str]]:
    """Every parameter name the packet holds: its parameters, a simulated candidate's,
    and a parameter's documentation."""

    def get(node: Any, *keys: str) -> Any:
        for k in keys:
            node = cast(dict[str, Any], node).get(k) if isinstance(node, dict) else None
        return node

    for path, names in (
        ("$.parameters", get(packet, "parameters")),
        ("$.tool.filter_simulation.params", get(packet, "tool", "filter_simulation", "params")),
    ):
        if isinstance(names, dict):
            for name in cast(dict[str, Any], names):
                yield path, name
    doc = get(packet, "tool", "parameter_doc", "name")
    if isinstance(doc, str):
        yield "$.tool.parameter_doc.name", doc


def check(packet: Any, tripwire: Sequence[Point], policy: Policy) -> list[Violation]:
    """Every reason the packet may not leave; empty when it may."""
    out = [Violation(e.split(":", 1)[0], "shape: " + e.split(": ", 1)[1]) for e in validate(packet)]
    for path, name in _parameter_names(packet):
        if policy.parameter_removed(name):
            out.append(Violation(path, "a location-revealing parameter"))
    for path, value in _walk(packet):
        if isinstance(value, str) and (rule := _string_rules(value)):
            out.append(Violation(path, rule))
    numbers = _numbers(packet)
    for path, x, decimals in numbers:
        if rule := _number_rules(x, decimals):
            out.append(Violation(path, rule))
    out += _tripwire(numbers, tripwire)
    return out


def scrub(packet: Any, tripwire: Sequence[Point], policy: Policy) -> Any:
    """The packet, if it may leave; ScrubError with every reason otherwise."""
    violations = check(packet, tripwire, policy)
    if violations:
        raise ScrubError(violations)
    return packet
