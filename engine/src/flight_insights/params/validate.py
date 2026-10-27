"""A value against its firmware's parameter metadata (spec: Recommendation engine,
"Validation"; Analyzers, "Parameter audit").

One rule set for both sides: the parameter audit judges the values an aircraft flies
with, and the recommendation engine judges the values it proposes, the same way.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from flight_insights.params.files import format_value

TOLERANCE = 1e-6

Problem = tuple[str, dict[str, Any]]  # a reason code and the values its wording fills in


def metadata_problem(
    native: str, value: float, meta: Mapping[str, Any] | None, firmware: str
) -> Problem | None:
    """What is wrong with `value` for this parameter in this firmware, or None.

    - unknown_param: the firmware has no such parameter;
    - read_only: it cannot be set;
    - out_of_range: outside the documented range, unless the firmware is known to accept
      it (`accepts`, from params/beyond_range.json: `beyond_documented` says so);
    - not_a_value: without a range, the listed values are the only ones; with a range
      they are only suggestions;
    - undefined_bits: a bitmask value sets bits the metadata does not define.
    """
    if meta is None:
        return "unknown_param", {"param": native, "firmware": firmware}
    if meta.get("read_only"):
        return "read_only", {"param": native}
    text = format_value(value)
    bounds = meta.get("range")
    if bounds is not None:
        low, high = float(bounds[0]), float(bounds[1])
        if not low - TOLERANCE <= value <= high + TOLERANCE and not _accepted(value, meta):
            return "out_of_range", {
                "param": native, "value": text,
                "low": format_value(low), "high": format_value(high),
            }  # fmt: skip
    elif meta.get("values"):
        values = cast(Mapping[str, str], meta["values"])
        if not any(abs(float(k) - value) < TOLERANCE for k in values):
            return "not_a_value", {"param": native, "value": text}
    if meta.get("bitmask"):
        bits = {int(k) for k in cast(Mapping[str, str], meta["bitmask"])}
        defined = sum(1 << b for b in bits)
        if value < 0 or value != int(value) or int(value) & ~defined:
            return "undefined_bits", {"param": native, "value": text}
    return None


def _accepted(value: float, meta: Mapping[str, Any]) -> bool:
    accepts = meta.get("accepts")
    return accepts is not None and (
        float(accepts[0]) - TOLERANCE <= value <= float(accepts[1]) + TOLERANCE
    )


def beyond_documented(native: str, value: float, meta: Mapping[str, Any] | None) -> Problem | None:
    """A value the firmware accepts although it is past the documented range: worth saying,
    as a ground station may warn when the file is loaded. None otherwise."""
    bounds = None if meta is None else meta.get("range")
    if meta is None or bounds is None or not _accepted(value, meta):
        return None
    low, high = float(bounds[0]), float(bounds[1])
    if low - TOLERANCE <= value <= high + TOLERANCE:
        return None
    return "beyond_documented", {
        "param": native, "value": format_value(value),
        "low": format_value(low), "high": format_value(high),
    }  # fmt: skip
