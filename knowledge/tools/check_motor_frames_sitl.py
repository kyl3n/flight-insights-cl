#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy==2.3.3", "pymavlink==2.4.50", "pyyaml==6.0.2"]
# ///
"""Check the motor frame table against ArduPilot SITL: fly each multirotor frame SITL
has a model for, with System ID mode adding a chirp to the mixer's roll, pitch, yaw and
throttle input in turn (SID_AXIS 10 to 13), and fit each motor's output to what the
mixer was asked for. The fitted factors must match knowledge/frames/<version>.json.

RATE logs the mixer's roll, pitch and yaw inputs and its throttle; RCOU logs each
motor's output at 10 Hz. With fast and medium attitude logging both on, System ID logs
RATE every loop after the motors are output, so each RCOU row pairs exactly with the
RATE row just before it (ArduCopter/Copter.cpp task order, mode_systemid.cpp). With
MOT_THST_EXPO 0 and MOT_BAT_VOLT_MAX 0 the mixer is linear
(AP_MotorsMatrix::output_armed_stabilizing, AP_Motors_Thrust_Linearization.cpp):

    PWM_i = a + b * (throttle * t_i + roll * r_i + pitch * p_i + yaw * y_i)

with a and b the same for every motor. A least-squares fit per motor, on rows where no
motor is at a limit, gives b times each factor; the largest throttle factor is 1, which
gives b. Rows the fit leaves far off (yaw held back for headroom, a limit) are dropped
and the fit run again.

The check fails when any factor is off by more than 2% of its axis's range (0.01 of the
mixer's 0.5 for roll, pitch and yaw; 0.02 of 1 for throttle). Parameters are only ever
written to this local simulator.

Usage (from the repository root):
  uv run knowledge/tools/check_motor_frames_sitl.py --ardupilot DIR --version 4.7.0
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "fixtures" / "sitl"))

from run_scenario import SITL_HOME, ScenarioFailed, Vehicle  # noqa: E402
from scenarios import ML, land_and_disarm, sticks  # noqa: E402

# SITL's multirotor frames (Tools/autotest/pysim/vehicleinfo.py): the model, and the
# default parameter files after copter.parm, which set FRAME_CLASS and FRAME_TYPE.
FRAMES: dict[str, list[str]] = {
    "+": [],
    "X": ["copter-X.parm"],
    "bfx": ["copter-bfx.parm"],
    "djix": ["copter-djix.parm"],
    "cwx": ["copter-cwx.parm"],
    "hexa": ["copter-hexa.parm"],
    "hexax": ["copter-hexa.parm", "copter-X.parm"],
    "hexa-cwx": ["copter-hexa.parm", "copter-hexa-cwx.parm"],
    "hexa-dji": ["copter-hexa.parm", "copter-hexa-dji.parm"],
    "octa": ["copter-octa.parm"],
    "octa-cwx": ["copter-octa.parm", "copter-octa-cwx.parm"],
    "octa-dji": ["copter-octa.parm", "copter-octa-dji.parm"],
    "octa-quad": ["copter-octaquad.parm"],
    "octa-quad-cwx": ["copter-octaquad.parm", "copter-octaquad-cwx.parm"],
    "octa-quad-cor": ["copter-octaquad-cor.parm"],
    "octa-quad-cw-cor": ["copter-octaquad-cw-cor.parm"],
    "deca": ["copter-deca.parm"],
    "deca-cwx": ["copter-deca.parm", "copter-deca-cwx.parm"],
    "y6": ["copter-y6.parm"],
    "dodeca-hexa": ["copter-dodecahexa.parm"],
}
LINEAR = {"MOT_THST_EXPO": 0.0, "MOT_BAT_VOLT_MAX": 0.0}  # the mixer's output, unbent
# The sticks stay where they were last sent while modes change (a lapsed override would
# drop to the simulated radio's idle throttle in Stabilize).
HOLD_STICKS = {"RC_OVERRIDE_TIME": -1.0}
# A chirp of 0.05 on the mixer's input (a tenth of a factor's range), slow enough that
# the attitude loops keep the aircraft level around it.
SYSID = {"SID_MAGNITUDE": 0.05, "SID_F_START_HZ": 0.3, "SID_F_STOP_HZ": 2.0,
         "SID_T_FADE_IN": 1.0, "SID_T_REC": 15.0, "SID_T_FADE_OUT": 1.0}  # fmt: skip
AXES = ("throttle", "roll", "pitch", "yaw")
TOLERANCE = {"throttle": 0.02, "roll": 0.01, "pitch": 0.01, "yaw": 0.01}
SYSTEMID_MODE = 25
LOG_ATTITUDE_FAST_AND_MED = 1 << 0 | 1 << 1
EDGE_US = 5  # rows with any motor this close to its spin limits are left out
SPEEDUP = 10


def fly(v: Vehicle) -> None:
    """Take off; then for each mixer axis, System ID mode adds a chirp to that axis's
    input (SID_AXIS 10 to 13) while the attitude loops hold the aircraft; then land."""
    v.mode("GUIDED")
    v.arm()
    v.command(ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 30)
    v.wait_altitude(29.5)
    centre = {"ch1": 1500, "ch2": 1500, "ch3": 1500, "ch4": 1500}
    v.rc(**centre)
    # With fast and medium attitude logging both on, System ID logs RATE every loop
    # (ModeSystemId::run), so each RCOU row has its own loop's RATE row before it.
    v.set_param("LOG_BITMASK", int(v.get_param("LOG_BITMASK")) | LOG_ATTITUDE_FAST_AND_MED)
    for axis in (10, 11, 12, 13):  # mixer roll, pitch, yaw and throttle
        v.set_param("SID_AXIS", axis)
        v.mode("ALT_HOLD")
        sticks(v, 3, centre)
        v.mode("STABILIZE")  # System ID on the attitude loops starts from manual throttle
        v.mode("SYSTEMID")
        sticks(v, SYSID["SID_T_FADE_IN"] + SYSID["SID_T_REC"] + SYSID["SID_T_FADE_OUT"] + 1, centre)
    v.mode("ALT_HOLD")
    land_and_disarm(v)


def fly_frame(ardupilot: Path, frame: str, work: Path) -> tuple[Path, dict[str, float]]:
    """Fly one frame; returns its log and its parameters."""
    binary = ardupilot / "build" / "sitl" / "bin" / "arducopter"
    if not binary.exists():
        raise ScenarioFailed(f"{binary} not built")
    params = ardupilot / "Tools" / "autotest" / "default_params"
    boot = work / "linear.parm"
    boot.write_text("".join(f"{k} {v}\n" for k, v in (LINEAR | SYSID | HOLD_STICKS).items()))
    defaults = [params / "copter.parm", *(params / p for p in FRAMES[frame]), boot]
    command = [
        str(binary), "--model", "+" if frame == "+" else frame, "--speedup", str(SPEEDUP),
        "--defaults", ",".join(str(d) for d in defaults), "--home", SITL_HOME, "-I0",
    ]  # fmt: skip
    with (work / "sitl.txt").open("w") as out:
        proc = subprocess.Popen(command, cwd=work, stdout=out, stderr=subprocess.STDOUT)
        try:
            v = Vehicle(work / "flight.tlog")
            v.wait_ready()
            values = {n: v.get_param(n) for n in ("FRAME_CLASS", "FRAME_TYPE", *LINEAR)}
            fly(v)
            v.hold(3)
            v.close()
        finally:
            proc.terminate()
            proc.wait(timeout=30)
    logs = sorted((work / "logs").glob("*.BIN"), key=lambda p: p.stat().st_mtime)
    if not logs:
        raise ScenarioFailed(f"{frame}: SITL wrote no log")
    return logs[-1], values


def rows(
    log: Path, motors: list[int]
) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any], float, float]:
    """Each RCOU row logged in System ID mode in the same loop as a RATE row (less than a
    loop apart): the mixer's inputs (throttle, roll, pitch, yaw) and every motor's output;
    and the spin limits in PWM."""
    from pymavlink import mavutil

    mlog = mavutil.mavlink_connection(str(log), dialect="ardupilotmega")
    params: dict[str, float] = {}
    rate: Any = None
    testing = False
    inputs: list[list[float]] = []
    outputs: list[list[float]] = []
    while (m := mlog.recv_match(type=["PARM", "MODE", "RATE", "RCOU"])) is not None:
        kind = m.get_type()
        if kind == "PARM":
            params[m.Name] = m.Value
        elif kind == "MODE":
            testing = m.ModeNum == SYSTEMID_MODE
        elif kind == "RATE":
            rate = m
        elif kind == "RCOU" and testing and rate is not None:
            if m.TimeUS - rate.TimeUS < 1e6 / params.get("SCHED_LOOP_RATE", 400.0):
                inputs.append([rate.AOut, rate.ROut, rate.POut, rate.YOut])
                outputs.append([getattr(m, f"C{n}") for n in motors])  # SERVOn = motor n
            rate = None  # each RATE row pairs with one RCOU row at most
    lo, hi = params.get("MOT_PWM_MIN", 1000.0), params.get("MOT_PWM_MAX", 2000.0)
    spin_min = lo + (hi - lo) * params.get("MOT_SPIN_MIN", 0.15)
    spin_max = lo + (hi - lo) * params.get("MOT_SPIN_MAX", 0.95)
    return np.array(inputs), np.array(outputs, float), spin_min, spin_max


def fit(x: np.ndarray[Any, Any], y: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    """Least squares of y on [1, x], twice: the second without rows more than 4 times the
    first fit's median absolute residual off (yaw held back for headroom)."""
    a = np.column_stack([np.ones(len(x)), x])
    coef = np.linalg.lstsq(a, y, rcond=None)[0]
    residual = np.abs(y - a @ coef)
    keep = residual <= 4 * max(float(np.median(residual)), 0.5)
    return np.linalg.lstsq(a[keep], y[keep], rcond=None)[0]


def check(frame: str, log: Path, values: dict[str, float], table: dict[str, Any]) -> list[str]:
    cls, kind = str(int(values["FRAME_CLASS"])), str(int(values["FRAME_TYPE"]))
    types = table[cls]["types"]
    entry = types.get(kind) or types["default"]
    motors: list[dict[str, Any]] = entry["motors"]
    x, y, spin_min, spin_max = rows(log, [int(m["motor"]) for m in motors])
    inside = ((y > spin_min + EDGE_US) & (y < spin_max - EDGE_US)).all(axis=1)
    x, y = x[inside], y[inside]
    if len(x) < 100:
        return [f"{frame}: only {len(x)} rows clear of the spin limits"]
    coef = np.array([fit(x, y[:, i])[1:] for i in range(len(motors))])  # b * factors
    b = coef[:, 0].max()
    found = coef / b
    problems: list[str] = []
    worst = 0.0
    for i, m in enumerate(motors):
        for j, axis in enumerate(AXES):
            off = abs(found[i, j] - float(m[axis]))
            worst = max(worst, off * 0.02 / TOLERANCE[axis])  # of the axis's range
            if off > TOLERANCE[axis]:
                problems.append(
                    f"{frame} ({table[cls]['name']}/{entry['name']}) motor {m['motor']} {axis}: "
                    f"table {m[axis]:+.4f}, SITL {found[i, j]:+.4f}"
                )
    print(f"{frame}: {table[cls]['name']}/{entry['name']}, {len(x)} rows, "
          f"worst {100 * worst:.2f}% of its axis's range")  # fmt: skip
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--ardupilot", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--frames", nargs="*", default=list(FRAMES))
    parser.add_argument("--keep", type=Path, help="keep each frame's log in this folder")
    args = parser.parse_args(argv)
    table = json.loads((ROOT / "knowledge" / "frames" / f"{args.version}.json").read_text())
    problems: list[str] = []
    for frame in args.frames:
        with tempfile.TemporaryDirectory() as work:
            log, values = fly_frame(args.ardupilot, frame, Path(work))
            if args.keep:
                args.keep.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(log, args.keep / f"{frame}.bin")
            problems += check(frame, log, values, table["classes"])
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
