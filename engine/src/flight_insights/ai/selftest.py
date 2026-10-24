"""The scrubber's start-up self-test (spec: AI layer, "Proof, not promise": a self-test
runs at start-up, and AI calls stay blocked until it passes).

One clean packet and one leaky packet per rule, around an imagined flight in open sea
(no real flight). Every clean packet must pass and every leaky one must be refused. The
same cases are the shared corpus in tests/scrubber, which the shell's own check reads
too; `python -m flight_insights.ai.selftest DIR` writes them there, and a test keeps the
files and this module the same.
"""

from __future__ import annotations

import copy
import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flight_insights.ai.policy import Policy
from flight_insights.ai.schema import SCHEMA_ID
from flight_insights.ai.scrubber import Point, check

LAT, LON = 12.34567, 65.4321
POINTS: list[Point] = [(LAT, LON), (12.3501, 65.4402)]

BASE: dict[str, Any] = {
    "schema": SCHEMA_ID,
    "purpose": "analysis",
    "aircraft": {
        "vehicle": "copter", "firmware": "4.7.0", "vehicle_class": "multirotor",
        "frame_class": "Octa", "frame_type": "X", "board_family": "H7",
        "motor_protocol": "DShot600", "bidirectional_dshot": True,
        "notch_mode": "ESC Telemetry", "imu_count": 3, "cells": 12, "simulated": False,
    },
    "answers": {"payload": "variable", "esc_firmware": "bluejay", "priority": "endurance"},
    "flight": {
        "duration_s": 1084.2, "hover_s": 312.5,
        "modes": [{"mode": "LOITER", "s": 900.1}, {"mode": "LAND", "s": 42}],
    },
    "metrics": [
        {"metric": "vibration.p95", "segment": "hover", "value": 14.21, "zone": "target"},
        {"metric": "power.hover_power", "segment": "hover", "value": 1950.4, "zone": None},
        {"metric": "power.battery_resistance", "segment": "flight", "value": 0.0061,
         "zone": "target"},
        {"metric": "esc.peak_temperature", "segment": "flight", "value": 66, "zone": None},
    ],
    "findings": [
        {"ref": "F1", "code": "esc.hot", "severity": "caution", "confidence": "medium",
         "params": {"motor": 3, "temperature": 66, "siblings": 53}, "evidence": ["E1"]},
        {"ref": "F2", "code": "filter.better_settings", "severity": "info",
         "confidence": "high",
         "params": {"lag_new": 61.2, "lag_now": 93.5, "lag_hz": 20, "noise_db": -31.1},
         "evidence": ["E2"]},
    ],
    "evidence": [
        {"ref": "E1", "kind": "metric",
         "summary": "ESC peak temperatures 52, 53, 66, 54, 53, 52, 55, 54 °C",
         "messages": ["ESC"], "fields": ["Temp"],
         "segment": {"kind": "flight", "start_s": 0, "end_s": 1084.2},
         "values": {"motor3": 66, "motor1": 52}},
        {"ref": "E2", "kind": "series",
         "summary": "1950 W at 90.1 A in hover over 312.5 s; 93.5° of lag at 20 Hz",
         "messages": ["BAT", "GYR"], "fields": ["Volt", "Curr", "GyrX"], "segment": None,
         "values": {"rate_hz": 1000}},
    ],
    "parameters": {
        "ATC_RAT_PIT_P": 0.135, "INS_HNTCH_FREQ": 80, "INS_HNTCH_BW": 40,
        "MOT_SPIN_MAX": 0.95, "BATT_CAPACITY": 22000, "ARM_FENCE": 1, "SERIAL2_BAUD": 57,
        "LOG_BITMASK": 655358,
    },
}  # fmt: skip


def _set(*path: str | int, value: Any) -> Callable[[dict[str, Any]], None]:
    def apply(p: dict[str, Any]) -> None:
        node: Any = p
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return apply


def _both(*changes: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    def apply(p: dict[str, Any]) -> None:
        for c in changes:
            c(p)

    return apply


LEAKY: dict[str, tuple[str, Callable[[dict[str, Any]], None]]] = {
    "unknown_key": ("A key the schema does not name.", _set("gps", value={"fix": 3})),
    "unknown_aircraft_key": ("A key inside aircraft the schema does not name.",
                             _set("aircraft", "home", value="x")),
    "raw_scaled_int": ("A latitude scaled by 1e7, as GPS.Lat stores it.",
                       _set("evidence", 0, "values", "lat", value=round(LAT * 1e7))),
    "precise_degrees": ("A latitude with five decimals as a metric value.",
                        _set("metrics", 0, "value", value=LAT)),
    "tripwire_fine": ("A value within 5 m of the flight's latitude, rounded to four decimals.",
                      _set("metrics", 1, "value", value=round(LAT, 4))),
    "tripwire_pair": ("The flight's latitude and longitude to two decimals, in two places.",
                      _both(_set("findings", 0, "params", "a", value=round(LAT, 2)),
                            _set("findings", 0, "params", "b", value=round(LON, 2)))),
    "tripwire_radians": ("The latitude in radians.",
                         _set("evidence", 1, "values", "r", value=round(math.radians(LAT), 7))),
    "text_decimal_pair": ("A decimal-degree pair inside evidence text.",
                          _set("evidence", 0, "summary", value=f"hover near {LAT:.4f}, {LON:.4f}")),
    "text_dms": ("Degrees, minutes and seconds in text.",
                 _set("evidence", 0, "summary", value="position 12°20'44\" N 65°25'55\" E")),
    "text_hemisphere": ("Degrees with a hemisphere letter.",
                        _set("evidence", 1, "summary", value="at 12.346 N")),
    "text_label": ("A lat label with a number.", _set("evidence", 1, "summary", value="lat=12.3")),
    "text_utm": ("A UTM coordinate.", _set("evidence", 1, "summary", value="42P 432145 1365432")),
    "text_mgrs": ("An MGRS reference.",
                  _set("evidence", 1, "summary", value="grid 42PWA3214565432")),
    "text_geohash": ("A geohash.", _set("evidence", 1, "summary", value="cell t2kgq8x4")),
    "text_plus_code": ("An Open Location Code.",
                       _set("evidence", 1, "summary", value="7JCM9RQ6+2X")),
    "removed_parameter": ("A parameter the rules remove: the estimator origin.",
                          _set("parameters", "AHRS_ORIG_LAT", value=12.3)),
    "compass_declination": ("Magnetic declination narrows down the place.",
                            _set("parameters", "COMPASS_DEC", value=0.04)),
    "long_text": ("Text longer than the schema allows.",
                  _set("evidence", 0, "summary", value="x" * 300)),
    "bool_as_number": ("A boolean where a number belongs.",
                       _set("metrics", 0, "value", value=True)),
}  # fmt: skip

CLEAN: dict[str, tuple[str, dict[str, Any]]] = {
    "analysis": ("A full analysis packet: every value is engine-built and none is a position.",
                 BASE),
    "minimal": ("The smallest packet the schema allows.",
                {"schema": SCHEMA_ID, "purpose": "chat",
                 "aircraft": {"vehicle": "plane", "firmware": "4.7.2"}}),
}  # fmt: skip


def cases() -> dict[str, dict[str, tuple[str, dict[str, Any]]]]:
    """kind ("clean", "leaky") -> name -> (why, packet)."""
    leaky: dict[str, tuple[str, dict[str, Any]]] = {}
    for name, (why, change) in LEAKY.items():
        packet = copy.deepcopy(BASE)
        change(packet)
        leaky[name] = (why, packet)
    return {"clean": {k: (w, copy.deepcopy(p)) for k, (w, p) in CLEAN.items()}, "leaky": leaky}


def run(policy: Policy) -> dict[str, Any]:
    """Every case through the scrubber: ok only if every clean packet passes and every
    leaky one is refused. Names what went wrong, never a value."""
    failed: list[str] = []
    every = cases()
    for name, (_, packet) in every["clean"].items():
        if check(packet, POINTS, policy):
            failed.append(f"clean packet {name} was refused")
    for name, (_, packet) in every["leaky"].items():
        if not check(packet, POINTS, policy):
            failed.append(f"leaky packet {name} was let through")
    total = len(every["clean"]) + len(every["leaky"])
    return {"ok": not failed, "cases": total, "failed": failed}


def write(folder: Path) -> None:
    """The cases as the shared corpus, for the shell's tests and the engine's."""
    for kind, items in cases().items():
        target = folder / "packets" / kind
        target.mkdir(parents=True, exist_ok=True)
        for name, (why, packet) in items.items():
            body = json.dumps({"why": why, "packet": packet}, indent=1, ensure_ascii=False)
            (target / f"{name}.json").write_text(body + "\n", encoding="utf-8")
    tripwire = {"why": "Synthetic positions of the corpus's imagined flight, in open sea; no "
                "real flight.", "points": [list(p) for p in POINTS]}  # fmt: skip
    (folder / "tripwire.json").write_text(json.dumps(tripwire, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write(Path(sys.argv[1]))
