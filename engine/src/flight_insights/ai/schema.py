"""The shape of every AI packet, and the check that a packet has exactly that shape
(spec: AI layer, "Allowlist only").

The schema lives in the engine's code, not in the knowledge pack, so a pack update can
never widen what leaves the laptop. It names every key a packet may hold: there are no
free keys, only keys that match a narrow pattern (metric ids, parameter names, finding
parameter names) with scalar values. Strings are short, and every string and number is
then searched for coordinates by the scrubber. The Rust shell keeps its own copy of
this shape, written separately (spec: "Checked twice"); a change here needs the same
change there and a scrubber test (CLAUDE.md).

`validate` understands only the parts of JSON Schema this file uses.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

SCHEMA_ID = "fi.ai.packet/1"

TEXT = {"type": "string", "maxLength": 200}
NAME = {"type": "string", "pattern": r"^[a-z][a-z0-9_.]{0,63}$"}  # a code, id or word
SCALAR = {"type": ["number", "string", "boolean", "null"], "maxLength": 200}
SCALARS = {"type": "array", "maxItems": 64, "items": SCALAR}
KEYED = {  # finding parameters, evidence values: snake_case keys, scalar values
    "type": "object",
    "patternProperties": {r"^[a-z][a-z0-9_]{0,40}$": {"anyOf": [SCALAR, SCALARS]}},
    "additionalProperties": False,
    "maxProperties": 64,
}
SEGMENT = {
    "type": "object",
    "properties": {"kind": NAME, "start_s": {"type": "number"}, "end_s": {"type": "number"}},
    "required": ["kind", "start_s", "end_s"],
    "additionalProperties": False,
}
REF = {"type": "string", "pattern": r"^[EF][0-9]{1,4}$"}  # packet-local references
PARAMETER = r"^[A-Z][A-Z0-9_]{0,16}$"
MAYBE_NUMBER = {"type": ["number", "null"]}
UNIT = {"type": ["string", "null"], "maxLength": 24}


def numbers(n: int) -> dict[str, Any]:
    return {"type": "array", "maxItems": n, "items": {"type": "number"}}


def strict(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


# What a local tool hands back to Claude (spec: AI layer, "Tools Claude can call": all
# executed locally, all scrubbed). Each result is a packet of its own, purpose "tool",
# checked and recorded like any other. Times are seconds from arming.
TOOL_NAMES = ("spectrum", "filter_simulation", "parameter_doc", "metric_history", "segment_stats")
SIMULATED = strict(
    {
        "available": {"type": "boolean"},
        "reason": {"type": ["string", "null"], "maxLength": 200},
        "noise_db": MAYBE_NUMBER,
        "lag_deg": MAYBE_NUMBER,
        "notches": {"type": ["integer", "null"]},
        "within_budget": {"type": ["boolean", "null"]},
        "centres_hz": numbers(16),
        "bandwidth_hz": MAYBE_NUMBER,
        "lowpass_hz": MAYBE_NUMBER,
    },
    "available",
)
TOOL_RESULTS: dict[str, Any] = {
    "spectrum": strict(
        {
            "axis": {"enum": ["x", "y", "z"]},
            "stage": {"enum": ["before", "flown"]},
            "segment": NAME,
            "seconds": {"type": "number"},
            "rate_hz": {"type": "number"},
            "hz": numbers(256),
            "db": numbers(256),
            "peaks": {
                "type": "array",
                "maxItems": 16,
                "items": strict(
                    {
                        "hz": {"type": "number"},
                        "db": {"type": "number"},
                        "prominence_db": {"type": "number"},
                    },
                    "hz",
                    "db",
                ),
            },
        },
        "axis",
        "stage",
        "hz",
        "db",
    ),
    "filter_simulation": strict(
        {
            "params": {
                "type": "object",
                "patternProperties": {PARAMETER: {"type": "number"}},
                "additionalProperties": False,
                "maxProperties": 16,
            },
            "current": SIMULATED,
            "candidate": SIMULATED,
        },
        "params",
        "current",
        "candidate",
    ),
    "parameter_doc": strict(
        {
            "name": {"type": "string", "pattern": PARAMETER},
            "display": {"type": ["string", "null"], "maxLength": 200},
            "description": {"type": ["string", "null"], "maxLength": 600},
            "units": UNIT,
            "range": {"anyOf": [numbers(2), {"type": "null"}]},
            "increment": MAYBE_NUMBER,
            "values": {
                "type": "array",
                "maxItems": 64,
                "items": strict({"value": {"type": "number"}, "label": TEXT}, "value", "label"),
            },
            "bits": {
                "type": "array",
                "maxItems": 32,
                "items": strict({"bit": {"type": "integer"}, "label": TEXT}, "bit", "label"),
            },
            "reboot": {"type": "boolean"},
            "current": MAYBE_NUMBER,
            "default": MAYBE_NUMBER,
        },
        "name",
    ),
    "metric_history": strict(
        {
            "metric": NAME,
            "segment": NAME,
            "unit": UNIT,
            "points": {
                "type": "array",
                "maxItems": 50,
                "items": strict(
                    {
                        "flight": {"type": "integer"},
                        "value": {"type": "number"},
                        "zone": {"enum": ["target", "caution", "out", None]},
                        "good": {"type": "boolean"},
                    },
                    "flight",
                    "value",
                ),
            },
            "baseline": {
                "anyOf": [
                    strict({"low": {"type": "number"}, "high": {"type": "number"}}, "low", "high"),
                    {"type": "null"},
                ]
            },
        },
        "metric",
        "segment",
        "points",
    ),
    "segment_stats": strict(
        {
            "message": {"type": "string", "pattern": r"^[A-Z0-9]{2,4}$"},
            "field": {"type": "string", "pattern": r"^[A-Za-z0-9_]{1,16}$"},
            "start_s": {"type": "number"},
            "end_s": {"type": "number"},
            "unit": UNIT,
            "seconds": {"type": "number"},
            "rate_hz": MAYBE_NUMBER,
            **{k: MAYBE_NUMBER for k in ("min", "p05", "p50", "mean", "p95", "max")},
        },
        "message",
        "field",
        "start_s",
        "end_s",
    ),
}
TOOL = strict(
    {
        "name": {"enum": list(TOOL_NAMES)},
        "ok": {"type": "boolean"},
        "error": {"type": ["string", "null"], "maxLength": 200},
        **TOOL_RESULTS,
    },
    "name",
    "ok",
)

PACKET: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema": {"enum": [SCHEMA_ID]},
        "purpose": {"enum": ["analysis", "chat", "explain", "tool"]},
        "aircraft": {
            "type": "object",
            "properties": {
                "vehicle": {"enum": ["copter", "plane"]},
                "firmware": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
                "vehicle_class": {"enum": ["multirotor", "helicopter", "plane", "quadplane", None]},
                "frame_class": {"type": ["string", "null"], "maxLength": 40},
                "frame_type": {"type": ["string", "null"], "maxLength": 40},
                "board_family": {"type": ["string", "null"], "maxLength": 16},
                "motor_protocol": {"type": ["string", "null"], "maxLength": 40},
                "bidirectional_dshot": {"type": ["boolean", "null"]},
                "notch_mode": {"type": ["string", "null"], "maxLength": 40},
                "imu_count": {"type": ["integer", "null"]},
                "cells": {"type": ["integer", "null"]},
                "simulated": {"type": "boolean"},
            },
            "required": ["vehicle", "firmware"],
            "additionalProperties": False,
        },
        "answers": {
            "type": "object",
            "patternProperties": {
                r"^[a-z][a-z0-9_]{0,40}$": {
                    "type": ["string", "number", "boolean"],
                    "maxLength": 40,
                }
            },
            "additionalProperties": False,
            "maxProperties": 64,
        },
        "flight": {
            "type": "object",
            "properties": {
                "duration_s": {"type": ["number", "null"]},
                "hover_s": {"type": ["number", "null"]},
                "modes": {
                    "type": "array",
                    "maxItems": 32,
                    "items": {
                        "type": "object",
                        "properties": {
                            "mode": {"type": "string", "pattern": r"^[A-Z0-9_]{1,24}$"},
                            "s": {"type": "number"},
                        },
                        "required": ["mode", "s"],
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
        },
        "metrics": {
            "type": "array",
            "maxItems": 512,
            "items": {
                "type": "object",
                "properties": {
                    "metric": NAME,
                    "segment": NAME,
                    "value": {"type": "number"},
                    "zone": {"enum": ["target", "caution", "out", None]},
                },
                "required": ["metric", "segment", "value"],
                "additionalProperties": False,
            },
        },
        "findings": {
            "type": "array",
            "maxItems": 256,
            "items": {
                "type": "object",
                "properties": {
                    "ref": REF,
                    "code": NAME,
                    "severity": {"enum": ["info", "caution", "out", "safety"]},
                    "confidence": {"enum": ["low", "medium", "high"]},
                    "params": KEYED,
                    "evidence": {"type": "array", "maxItems": 32, "items": REF},
                },
                "required": ["ref", "code", "severity", "confidence"],
                "additionalProperties": False,
            },
        },
        "evidence": {
            "type": "array",
            "maxItems": 512,
            "items": {
                "type": "object",
                "properties": {
                    "ref": REF,
                    "kind": {"enum": ["metric", "series", "parameter", "fact"]},
                    "summary": TEXT,
                    "messages": {
                        "type": "array",
                        "maxItems": 16,
                        "items": {"type": "string", "pattern": r"^[A-Z0-9]{2,4}$"},
                    },
                    "fields": {
                        "type": "array",
                        "maxItems": 32,
                        "items": {"type": "string", "pattern": r"^[A-Za-z0-9_]{1,16}$"},
                    },
                    "segment": {"anyOf": [SEGMENT, {"type": "null"}]},
                    "values": KEYED,
                },
                "required": ["ref", "kind", "summary"],
                "additionalProperties": False,
            },
        },
        "parameters": {
            "type": "object",
            "patternProperties": {r"^[A-Z][A-Z0-9_]{0,16}$": {"type": "number"}},
            "additionalProperties": False,
            "maxProperties": 2000,
        },
        "tool": TOOL,
        # chat: the engineer's question and the earlier turns of the conversation, as
        # typed and as answered; checked like every other text in the packet
        "question": {"type": "string", "maxLength": 500},
        "conversation": {
            "type": "array",
            "maxItems": 20,
            "items": strict(
                {
                    "role": {"enum": ["user", "assistant"]},
                    "text": {"type": "string", "maxLength": 2000},
                },
                "role",
                "text",
            ),
        },
    },
    "required": ["schema", "purpose", "aircraft"],
    "additionalProperties": False,
}


def _type_ok(value: Any, kind: str) -> bool:
    if kind == "null":
        return value is None
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "string":
        return isinstance(value, str)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    raise ValueError(f"the schema uses an unknown type {kind}")


def validate(value: Any, schema: Mapping[str, Any] = PACKET, path: str = "$") -> list[str]:
    """Where the value breaks the schema: a list of JSON paths with what is wrong, never
    the value itself (so a refusal can be logged safely). Empty when it fits."""
    errors: list[str] = []
    if "anyOf" in schema:
        options = cast(list[Mapping[str, Any]], schema["anyOf"])
        if not any(not validate(value, s, path) for s in options):
            errors.append(f"{path}: fits none of the allowed shapes")
        return errors
    if "enum" in schema:
        if not any(e == value and type(e) is type(value) for e in schema["enum"]):
            errors.append(f"{path}: not one of the allowed values")
        return errors
    kinds = schema.get("type")
    if kinds is not None:
        allowed = [kinds] if isinstance(kinds, str) else list(kinds)
        if not any(_type_ok(value, k) for k in allowed):
            return [f"{path}: wrong type"]
    if isinstance(value, str):
        if len(value) > int(schema.get("maxLength", 200)):
            errors.append(f"{path}: text too long")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: text not in the allowed form")
    elif isinstance(value, list):
        items = cast(list[Any], value)
        if len(items) > int(schema.get("maxItems", 0)):
            errors.append(f"{path}: too many items")
        item_schema = cast(Mapping[str, Any], schema.get("items", {}))
        for i, item in enumerate(items):
            errors += validate(item, item_schema, f"{path}[{i}]")
    elif isinstance(value, dict):
        obj = cast(dict[Any, Any], value)
        if len(obj) > int(schema.get("maxProperties", 10_000)):
            errors.append(f"{path}: too many keys")
        props = cast(Mapping[str, Mapping[str, Any]], schema.get("properties", {}))
        patterns = cast(Mapping[str, Mapping[str, Any]], schema.get("patternProperties", {}))
        for key in cast(list[str], schema.get("required", [])):
            if key not in obj:
                errors.append(f"{path}: missing {key}")
        for key, item in obj.items():
            if not isinstance(key, str):
                errors.append(f"{path}: a key that is not text")
                continue
            if key in props:
                errors += validate(item, props[key], f"{path}.{key}")
                continue
            matched = [s for p, s in patterns.items() if re.search(p, key)]
            if matched:
                errors += validate(item, matched[0], f"{path}.{key}")
            elif schema.get("additionalProperties", True) is False:
                errors.append(f"{path}: a key the packet may not hold")
    return errors
