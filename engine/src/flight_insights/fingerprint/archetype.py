"""Archetype scoring: which kind of aircraft this most likely is (spec: Vehicle
fingerprinting, "Archetypes").

A rule-based scorer reads its conditions from the rule pack
(knowledge/rules/archetypes.json). Every score lists the evidence behind it:
each condition, the value it was checked against, and whether it was met, not
met, or unknown. The pack says whether its thresholds are calibrated; v0 is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flight_insights.fingerprint.attributes import Fingerprint
from flight_insights.knowledge import Pack

Feature = float | bool | str | None
COAXIAL_FRAMES = {"OctaQuad", "Y6", "DodecaHexa", "CoaxCopter"}


@dataclass(frozen=True)
class Evidence:
    feature: str
    value: Feature
    condition: str  # such as "100 to 250" or "= OctaQuad"
    weight: float
    met: bool | None  # None: the feature is unknown for this vehicle
    why: str


@dataclass(frozen=True)
class ArchetypeScore:
    archetype: str
    score: float  # weight met / weight known, 0 to 1
    confidence: float  # weight known / weight listed, 0 to 1
    evidence: tuple[Evidence, ...]


def features(fp: Fingerprint) -> dict[str, Feature]:
    """The facts the rules can test. A tuning parameter still at its firmware default
    is unknown: nobody chose it for this vehicle. Enable switches count either way."""
    frame = fp.frame.value
    chosen = fp.configured

    def switch(*names: str) -> bool | None:
        present = [n for n in names if n in fp.params]
        return any(fp.params[n] != 0 for n in present) if present else None

    expo = chosen.get("Q_M_THST_EXPO", chosen.get("MOT_THST_EXPO"))
    return {
        "vehicle_class": frame.vehicle_class if frame else None,
        "frame_class": frame.frame_class if frame else None,
        "coaxial": (frame.frame_class in COAXIAL_FRAMES)
        if frame and frame.vehicle_class == "multirotor" and frame.frame_class
        else None,
        "vtol": frame.vtol if frame else None,
        "heli": frame.heli if frame else None,
        "motor_hz_at_hover": fp.motor_hz_at_hover.value,
        "thst_expo": expo,
        "gyro_filter_hz": chosen.get("INS_GYRO_FILTER"),
        "cells": fp.cells.value,
        "capacity_mah": chosen.get("BATT_CAPACITY"),
        "airspeed_cruise_m_s": chosen.get("AIRSPEED_CRUISE"),
        "payload_device": switch("GRIP_ENABLE", "WINCH_TYPE"),
        "ice": switch("ICE_ENABLE"),
    }


def _check(rule: dict[str, Any], value: Feature) -> tuple[str, bool | None]:
    if "equals" in rule:
        text = f"= {rule['equals']}"
        return text, None if value is None else value == rule["equals"]
    lo, hi = rule.get("min"), rule.get("max")
    text = f"{lo} to {hi}" if lo is not None and hi is not None else (
        f">= {lo}" if lo is not None else f"<= {hi}"
    )  # fmt: skip
    if value is None or isinstance(value, (bool, str)):
        return text, None
    return text, (lo is None or value >= lo) and (hi is None or value <= hi)


def score_archetypes(fp: Fingerprint, pack: Pack) -> list[ArchetypeScore]:
    """Archetypes for this vehicle class, best first."""
    facts = features(fp)
    out: list[ArchetypeScore] = []
    for a in pack.json("rules/archetypes.json")["archetypes"]:
        if facts["vehicle_class"] not in a["vehicle_class"]:
            continue
        evidence: list[Evidence] = []
        for rule in a["conditions"]:
            value = facts.get(rule["feature"])
            text, met = _check(rule, value)
            evidence.append(
                Evidence(rule["feature"], value, text, float(rule["weight"]), met, rule["why"])
            )
        total = sum(e.weight for e in evidence)
        known = sum(e.weight for e in evidence if e.met is not None)
        met_w = sum(e.weight for e in evidence if e.met)
        out.append(
            ArchetypeScore(
                a["id"],
                met_w / known if known else 0.0,
                known / total if total else 0.0,
                tuple(evidence),
            )
        )
    out.sort(key=lambda s: (-s.score * s.confidence, -s.score, s.archetype))
    return out
