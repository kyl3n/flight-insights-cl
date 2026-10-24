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

PACKET: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema": {"enum": [SCHEMA_ID]},
        "purpose": {"enum": ["analysis", "chat", "explain"]},
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
