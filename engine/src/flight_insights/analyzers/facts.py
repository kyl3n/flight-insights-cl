"""What a flight shows about the aircraft's capabilities and setup, as the Better options
catalog asks about them (spec: Configuration review, "Better options").

Each fact is true, false, or unknown (None), and some are numbers the catalog's
proposed changes use. Facts come from the parameters, the fingerprint, the log and
the setup answers; an unknown one becomes a question for the platform setup.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.motors import MotorPicture
from flight_insights.analyzers.motors_health import motors
from flight_insights.dataflash import si

Fact = bool | float | None
DSHOT_TYPES = range(4, 8)  # MOT_PWM_TYPE: DShot150 to DShot1200
BDSHOT_FIRMWARE = ("BLHeli_32", "AM32", "Bluejay")  # ESC firmware with bidirectional DShot
DSHOT_FIRMWARE = (*BDSHOT_FIRMWARE, "BLHeli_S")
# Full and empty cell voltages by chemistry, for MOT_BAT_VOLT_MAX and _MIN (ArduPilot's
# motor thrust scaling guidance: 4.2 V and 3.3 V a cell for LiPo).
CELL_VOLTS = {"LiPo": (4.2, 3.3), "LiHV": (4.35, 3.3), "Li-ion": (4.2, 3.0), "LiFePO4": (3.6, 2.8)}


def _known(answers: Mapping[str, Any], name: str) -> Any:
    value = answers.get(name)
    return None if value in (None, "") else value


def _cpu_load_p95(flight: FlightData) -> float | None:
    pm = flight.table("PM")
    if pm is None or "Load" not in pm.column_names:
        return None
    t = np.asarray(pm.column("TimeUS").to_numpy(), np.int64)
    load = si.values(pm, "Load")[(t >= flight.arm_us) & (t <= flight.disarm_us)]
    return float(np.percentile(load, 95)) if len(load) else None


def _attr(fp: Mapping[str, Any], name: str, key: str = "value") -> Any:
    entry: Mapping[str, Any] = fp.get(name) or {}
    return entry.get(key)


def _capable(firmware: Any, which: tuple[str, ...]) -> bool | None:
    """Whether ESC firmware can do something; unknown for unanswered or "other"."""
    if not firmware or firmware == "other":
        return None
    return firmware in which


def facts(flight: FlightData, m: MotorPicture) -> dict[str, Fact]:
    p, fp, answers = flight.params, flight.fingerprint, flight.answers
    family = _attr(fp, "board", "family")
    frame: Mapping[str, Any] = _attr(fp, "frame") or {}
    ms = motors(p)
    pwm_type = int(p.get("MOT_PWM_TYPE", 0))
    firmware = _known(answers, "esc_firmware")
    payload = _known(answers, "payload")
    chemistry = _known(answers, "battery_chemistry")
    cells = _attr(fp, "cells")
    cpu = None if flight.simulated else _cpu_load_p95(flight)  # SITL load is the host's
    volts = CELL_VOLTS.get(str(chemistry)) if chemistry else None
    out: dict[str, Fact] = {
        "copter": flight.vehicle == "copter",
        "heli": frame.get("vehicle_class") == "helicopter",
        "board_h7": family == "H7" if family else None,
        "board_f7_or_h7": family in ("F7", "H7") if family else None,
        "simulated": flight.simulated,
        "imu_count": float(_attr(fp, "imu_count") or 0) or None,
        "cpu_headroom": None if cpu is None else cpu < 50.0,
        "esc_telemetry": bool(m.escs) and any(q.reports_rpm for q in m.escs),
        "esc_rpm_reliable": m.rpm_reliable,
        "dshot_output": pwm_type in DSHOT_TYPES,
        "bdshot_enabled": bool(int(p.get("SERVO_BLH_BDMASK", 0))),
        "esc_dshot_capable": _capable(firmware, DSHOT_FIRMWARE),
        "esc_bdshot_capable": _capable(firmware, BDSHOT_FIRMWARE),
        "variable_payload": (payload == "variable") if payload else None,
        "battery_chemistry_known": volts is not None if chemistry else None,
        "notch_enabled": bool(int(p.get("INS_HNTCH_ENABLE", 0))),
        "raw_imu_logged": flight.has("GYR"),
        "motor_mask": float(sum(1 << (mo.channel - 1) for mo in ms)) or None,
        "motor_count": float(len(ms)) or None,
        "cells": float(cells) if cells else None,
    }
    if volts is not None and cells:
        out["batt_volt_max"] = round(volts[0] * float(cells), 2)
        out["batt_volt_min"] = round(volts[1] * float(cells), 2)
    return out
