"""The analysis packet: what the AI layer may tell Claude about one flight (spec: AI
layer, "Request flow"; "Allowlist only").

Built only from feature objects the engine made, never from raw log messages:

- the aircraft, from the fingerprint: vehicle, firmware, frame, board family, motor
  protocol, notch mode, IMU count, cells;
- the platform's answers that are choices, numbers or yes/no (never free text, which
  could name a place or a person);
- the flight: its duration, hover time and time in each mode;
- the analysis: every metric, and every finding with the evidence it cites, under
  packet-local references (no flight, run or evidence ids, no hashes);
- the parameters a person tunes or configures, with calibration, learned values,
  counters, hardware IDs and every location-revealing parameter removed.

Times are relative to arming. Numbers are rounded to what reasoning needs. The packet
then goes through the scrubber before anything else sees it.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from flight_insights.ai.policy import Policy
from flight_insights.ai.schema import SCHEMA_ID
from flight_insights.analyzers.flight import FlightError, build_answers
from flight_insights.analyzers.run import run_analysis
from flight_insights.history.persist import load_snapshot
from flight_insights.knowledge import Pack
from flight_insights.platforms.schema import load_schema
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store

PURPOSES = ("analysis", "chat", "explain")
KEY = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
ANSWER_KINDS = ("choice", "number", "integer", "boolean")
SUMMARY_MAX = 200


def number(x: float) -> float | int:
    """A value rounded to what reasoning needs: six significant figures, and no more
    than four decimals in the range of degrees. Whole numbers stay whole."""
    if not math.isfinite(x):
        return 0
    v = float(f"{x:.6g}")
    if 1.0 <= abs(v) <= 180.0:
        v = round(v, 4)
    return int(v) if v.is_integer() and abs(v) < 1e15 else v


def _scalar(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return number(float(value))
    if isinstance(value, str):
        return value[:SUMMARY_MAX]
    return None  # nested objects never leave: they have no place in the shape


def _keyed(values: Mapping[str, Any], policy: Policy) -> dict[str, Any]:
    """Finding parameters and evidence values: snake_case keys, scalars or short lists,
    and nothing keyed by a removed parameter."""
    out: dict[str, Any] = {}
    for key, value in values.items():
        if not KEY.match(key) or policy.parameter_removed(key.upper()):
            continue
        if isinstance(value, list):
            out[key] = [_scalar(v) for v in cast(list[Any], value)[:64]]
        elif isinstance(value, dict):
            continue
        else:
            out[key] = _scalar(value)
    return dict(list(out.items())[:64])


def _attr(fp: Mapping[str, Any], name: str) -> Any:
    entry = fp.get(name)
    return cast(Mapping[str, Any], entry).get("value") if isinstance(entry, dict) else None


def _text(value: Any, limit: int = 40) -> str | None:
    return str(value)[:limit] if value is not None else None


def _aircraft(row: Mapping[str, Any], fp: Mapping[str, Any]) -> dict[str, Any]:
    frame = cast(Mapping[str, Any], _attr(fp, "frame") or {})
    board = cast(Mapping[str, Any], fp.get("board") or {})
    imus, cells = _attr(fp, "imu_count"), _attr(fp, "cells")
    dshot = _attr(fp, "bidirectional_dshot")
    return {
        "vehicle": str(fp.get("vehicle") or ""),
        "firmware": str(row["firmware"] or fp.get("firmware") or ""),
        "vehicle_class": frame.get("vehicle_class"),
        "frame_class": _text(frame.get("frame_class")),
        "frame_type": _text(frame.get("frame_type")),
        "board_family": _text(board.get("family"), 16),
        "motor_protocol": _text(_attr(fp, "motor_protocol")),
        "bidirectional_dshot": dshot if isinstance(dshot, bool) else None,
        "notch_mode": _text(_attr(fp, "notch_mode")),
        "imu_count": int(imus) if isinstance(imus, int | float) else None,
        "cells": int(cells) if isinstance(cells, int | float) else None,
        "simulated": board.get("hal") == "SITL",
    }


def _answers(store: Store, pack: Pack, build_id: str | None) -> dict[str, Any]:
    kinds = {f.id: f.type for f in load_schema(pack).fields.values()}
    out: dict[str, Any] = {}
    for key, value in build_answers(store, build_id).items():
        if kinds.get(key) not in ANSWER_KINDS or not KEY.match(key):
            continue
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, int | float):
            out[key] = number(float(value))
        elif isinstance(value, str) and len(value) <= 40:
            out[key] = value
    return out


def _flight(row: Mapping[str, Any], fp: Mapping[str, Any]) -> dict[str, Any]:
    modes = cast(Mapping[str, float], fp.get("mode_s") or {})
    duration = (
        (int(row["disarm_us"]) - int(row["arm_us"])) / 1e6
        if row["arm_us"] is not None and row["disarm_us"] is not None
        else None
    )
    hover = fp.get("hover_s")
    return {
        "duration_s": None if duration is None else number(duration),
        "hover_s": number(float(hover)) if isinstance(hover, int | float) else None,
        "modes": [
            {"mode": name, "s": number(float(s))}
            for name, s in sorted(modes.items(), key=lambda kv: -kv[1])[:32]
            if re.fullmatch(r"[A-Z0-9_]{1,24}", name)
        ],
    }


def _segment(segment: Any, arm_us: int) -> dict[str, Any] | None:
    if not isinstance(segment, dict):
        return None
    s = cast(Mapping[str, Any], segment)
    return {
        "kind": str(s.get("kind") or "flight")[:63],
        "start_s": number((int(s["start_us"]) - arm_us) / 1e6),
        "end_s": number((int(s["end_us"]) - arm_us) / 1e6),
    }


def _evidence(e: Mapping[str, Any], ref: str, arm_us: int, policy: Policy) -> dict[str, Any]:
    messages = [m for m in e["messages"] if re.fullmatch(r"[A-Z0-9]{2,4}", m)]
    fields = [x for x in e["fields"] if re.fullmatch(r"[A-Za-z0-9_]{1,16}", x)]
    return {
        "ref": ref,
        "kind": e["kind"],
        "summary": str(e["summary"])[:SUMMARY_MAX],
        "messages": messages[:16],
        "fields": fields[:32],
        "segment": _segment(e.get("segment"), arm_us),
        "values": _keyed(e.get("values") or {}, policy),
    }


def _analysis(
    analysis: Mapping[str, Any], arm_us: int, policy: Policy, keys: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Metrics, findings and the evidence they cite; `keys` gets which finding or
    evidence each packet-local reference stands for."""
    metrics = [
        {"metric": m["metric"], "segment": m["segment"], "value": number(float(m["value"])),
         "zone": m["zone"]}
        for m in analysis["metrics"]
        if isinstance(m["value"], int | float) and math.isfinite(m["value"])
    ][:512]  # fmt: skip
    known = {e["id"]: e for e in analysis["evidence"]}
    refs: dict[str, str] = {}
    evidence: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for i, f in enumerate(cast(Sequence[Mapping[str, Any]], analysis["findings"])[:256]):
        cited: list[str] = []
        for eid in f["evidence"][:32]:
            e = known.get(eid)
            if e is None:
                continue
            if eid not in refs:
                refs[eid] = f"E{len(refs) + 1}"
                evidence.append(_evidence(e, refs[eid], arm_us, policy))
            cited.append(refs[eid])
        keys[f"F{i + 1}"] = str(f["id"])
        findings.append(
            {"ref": f"F{i + 1}", "code": f["code"], "severity": f["severity"],
             "confidence": f["confidence"], "params": _keyed(f["params"], policy),
             "evidence": cited}
        )  # fmt: skip
    keys |= {ref: eid for eid, ref in refs.items()}
    return metrics, findings, evidence[:512]


def _parameters(
    store: Store, pack: Pack, row: Mapping[str, Any], policy: Policy
) -> dict[str, float | int]:
    if not row["param_snapshot_id"]:
        return {}
    snapshot = load_snapshot(store, row["param_snapshot_id"])
    dialect = snapshot.dialect(pack)
    values = snapshot.values
    kept = {
        name: number(float(v))
        for name, v in values.items()
        if dialect.param_class(name, values) == "tuning"
        and re.fullmatch(r"[A-Z][A-Z0-9_]{0,16}", name)
        and math.isfinite(v)
    }
    return dict(sorted(policy.parameters(kept).items())[:2000])


@dataclass(frozen=True)
class Built:
    packet: dict[str, Any]
    run_id: str  # the analysis run it was built from
    refs: dict[str, str]  # "F1" -> finding id, "E1" -> evidence id


def build_packet(
    store: Store, cache: ColumnCache, pack: Pack, flight_id: str, purpose: str = "analysis"
) -> dict[str, Any]:
    """The packet for one flight's current analysis, before the scrubber's check."""
    return build(store, cache, pack, flight_id, purpose).packet


def build(
    store: Store, cache: ColumnCache, pack: Pack, flight_id: str, purpose: str = "analysis"
) -> Built:
    """The packet, with what its references stand for."""
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {', '.join(PURPOSES)}")
    row = store.get("flight", flight_id)
    if row is None or not row["fingerprint_json"]:
        raise FlightError(f"no analysed flight {flight_id}")
    fp: dict[str, Any] = json.loads(row["fingerprint_json"])
    policy = Policy.load(pack, str(row["firmware"] or fp.get("firmware") or pack.versions[-1]))
    analysis = run_analysis(store, cache, pack, flight_id)
    keys: dict[str, str] = {}
    metrics, findings, evidence = _analysis(analysis, int(row["arm_us"] or 0), policy, keys)
    packet: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "purpose": purpose,
        "aircraft": _aircraft(row, fp),
        "answers": _answers(store, pack, row["build_version_id"]),
        "flight": _flight(row, fp),
        "metrics": metrics,
        "findings": findings,
        "evidence": evidence,
        "parameters": _parameters(store, pack, row, policy),
    }
    return Built(packet, str(analysis["run"]), keys)
