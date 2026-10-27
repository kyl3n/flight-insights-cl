#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pymavlink==2.4.50"]
# ///
"""Fly an airframe's model, exported by Flight Insights, in ArduPilot's own simulator, and
keep the log for the app to compare with the airframe's flights (spec: Simulator checks).

  uv run fly_sitl.py --ardupilot ~/ardupilot --model hl1-40.5kg.json --frame octa-quad \\
      --params hl1-40.5kg.param --flight motors --out hl1-motors.bin

The ArduPilot checkout must be the release the model was exported for, with the copter
simulator built (./waf configure --board sitl && ./waf copter). Flights:

- hover: take off to 15 m, hover 30 s, land. The hover and power the model came from.
- legs: at 20 m, 6 s legs at 3, 6 and 9 m/s on eight courses with the nose turned off
  them, then a hover facing eight ways, and land. What the app measures the drag from.
- motors: take off to 15 m, hover 10 s, then stop each motor in turn for 12 s
  (SIM_ENGINE_FAIL, thrust times SIM_ENGINE_MUL 0) with 8 s between, and land. What the
  remaining motors do, against the app's motor-out margin.

It talks only to the simulator it starts itself on this computer: the link must be
loopback and the vehicle must have the simulator's own SIM_ parameters before anything is
written. Nothing it does can reach an aircraft.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pymavlink import mavutil

ML = mavutil.mavlink
HOST, PORT = "127.0.0.1", 5760
# ArduPilot's own test site (Tools/autotest/locations.txt, CMAC): a public field. Give
# your own take-off altitude to fly in the air your flights took off in.
HOME = "-35.363261,149.165230,584,353"
VELOCITY_YAW = 0b0000_1001_1100_0111  # SET_POSITION_TARGET_LOCAL_NED: velocity and yaw


class FlightFailed(Exception):
    pass


class Simulator:
    """ArduPilot SITL started here, and the one link this script writes to."""

    def __init__(self, ardupilot: Path, model: Path, frame: str, params: Path,
                 extra: dict[str, float], home: str, speedup: int) -> None:  # fmt: skip
        binary = ardupilot / "build" / "sitl" / "bin" / "arducopter"
        defaults = ardupilot / "Tools" / "autotest" / "default_params" / "copter.parm"
        if not binary.exists() or not defaults.exists():
            raise FlightFailed(f"no built copter simulator in {ardupilot}")
        self.work = Path(tempfile.mkdtemp(prefix="fi-sitl-"))
        shutil.copy(model, self.work / model.name)  # the simulator reads it from here
        boot = self.work / "boot.parm"
        boot.write_text("".join(f"{k} {v}\n" for k, v in extra.items()), encoding="utf-8")
        command = [
            str(binary), "--model", f"{frame}:{model.name}", "--speedup", str(speedup),
            "--defaults", ",".join(str(p) for p in (defaults, params.resolve(), boot)),
            "--home", home, "-I0",
        ]  # fmt: skip
        self.out = (self.work / "sitl.txt").open("w", encoding="utf-8")
        self.proc = subprocess.Popen(command, cwd=self.work, stdout=self.out,
                                     stderr=subprocess.STDOUT)  # fmt: skip
        self.conn: Any = mavutil.mavlink_connection(
            f"tcp:{HOST}:{PORT}", source_system=255, source_component=190, retries=60
        )
        self.texts: list[str] = []
        self.ready = False
        self.conn.message_hooks.append(self._watch)
        self.conn.wait_heartbeat(timeout=60)
        self.sys, self.comp = self.conn.target_system, self.conn.target_component
        self.conn.mav.request_data_stream_send(self.sys, self.comp, ML.MAV_DATA_STREAM_ALL,
                                               4, 1)  # fmt: skip
        self._check_simulator()

    def _watch(self, _conn: object, msg: Any) -> None:
        if msg.get_type() == "STATUSTEXT":
            self.texts.append(str(msg.text))
            self.ready |= "ArduPilot Ready" in str(msg.text)

    def _check_simulator(self) -> None:
        hosts = [HOST]
        with contextlib.suppress(AttributeError, OSError):
            hosts.append(str(self.conn.port.getpeername()[0]))
        for h in hosts:
            if not ipaddress.ip_address(h).is_loopback:
                raise FlightFailed(f"refusing a link to {h}: not this computer")
        try:
            self.get_param("SIM_SPEEDUP")
        except FlightFailed:
            raise FlightFailed("refusing a vehicle with no SIM_ parameters") from None

    def close(self) -> Path | None:
        """Stop the simulator; the newest log it wrote."""
        with contextlib.suppress(Exception):
            self.conn.close()
        self.proc.terminate()
        self.proc.wait(timeout=30)
        self.out.close()
        logs = sorted((self.work / "logs").glob("*.BIN"), key=lambda p: p.stat().st_mtime)
        return logs[-1] if logs else None

    # --- the link ----------------------------------------------------------------------------

    def wait(self, what: str, test: Callable[[Any], bool], kind: str, timeout: float) -> Any:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            m = self.conn.recv_match(type=kind, blocking=True, timeout=0.5)
            if m is not None and test(m):
                return m
        raise FlightFailed(f"timed out waiting for {what}; last messages: {self.texts[-5:]}")

    def hold(self, seconds: float) -> None:
        """Wait `seconds` of simulated time."""
        start = None
        while True:
            m = self.conn.recv_match(type="ATTITUDE", blocking=True, timeout=10)
            if m is None:
                raise FlightFailed("no ATTITUDE messages")
            if start is None:
                start = m.time_boot_ms
            elif m.time_boot_ms - start >= seconds * 1000:
                return

    def command(self, cmd: int, *params: float) -> None:
        p = [*params, 0, 0, 0, 0, 0, 0, 0][:7]
        self.conn.mav.command_long_send(self.sys, self.comp, cmd, 0, *p)

    def get_param(self, name: str) -> float:
        self.conn.mav.param_request_read_send(self.sys, self.comp, name.encode(), -1)
        m = self.wait(name, lambda m: m.param_id == name, "PARAM_VALUE", 10)
        return float(m.param_value)

    def set_sim(self, name: str, value: float) -> None:
        """Only the simulator's own parameters are ever set."""
        if not name.startswith("SIM_"):
            raise FlightFailed(f"refusing to set {name}: only SIM_ parameters are set")
        for _ in range(5):
            self.conn.mav.param_set_send(self.sys, self.comp, name.encode(), value,
                                         ML.MAV_PARAM_TYPE_REAL32)  # fmt: skip
            with contextlib.suppress(FlightFailed):
                self.wait(name, lambda m: m.param_id == name and abs(m.param_value - value)
                          < 1e-3, "PARAM_VALUE", 3)  # fmt: skip
                return
        raise FlightFailed(f"could not set {name}")

    def mode(self, name: str) -> None:
        """Set a mode and see it hold: the simulated radio's mode switch, read for the first
        time just after boot, can set a mode of its own."""
        number = self.conn.mode_mapping()[name]
        for _ in range(5):
            self.conn.set_mode(number)
            with contextlib.suppress(FlightFailed):
                self.wait(f"mode {name}", lambda m: m.custom_mode == number, "HEARTBEAT", 10)
                held, end = 0, time.monotonic() + 30
                while held < 3 and time.monotonic() < end:
                    m = self.conn.recv_match(type="HEARTBEAT", blocking=True, timeout=1)
                    if m is None or m.get_srcSystem() != self.sys:
                        continue
                    if m.custom_mode != number:
                        break
                    held += 1
                if held == 3:
                    return
        raise FlightFailed(f"the vehicle would not hold mode {name}")

    def take_off(self, metres: float) -> None:
        end = time.monotonic() + 120
        while not self.ready and time.monotonic() < end:
            self.conn.recv_match(blocking=True, timeout=0.5)
        prearm = ML.MAV_SYS_STATUS_PREARM_CHECK
        self.wait("pre-arm checks", lambda m: m.onboard_control_sensors_health & prearm,
                  "SYS_STATUS", 180)  # fmt: skip
        self.wait("an absolute position", lambda m: m.flags & ML.EKF_POS_HORIZ_ABS,
                  "EKF_STATUS_REPORT", 180)  # fmt: skip
        self.mode("GUIDED")
        armed = ML.MAV_MODE_FLAG_SAFETY_ARMED
        for _ in range(60):
            self.command(ML.MAV_CMD_COMPONENT_ARM_DISARM, 1)
            with contextlib.suppress(FlightFailed):
                self.wait("arming", lambda m: m.base_mode & armed, "HEARTBEAT", 2)
                break
        else:
            raise FlightFailed(f"the vehicle would not arm: {self.texts[-5:]}")
        self.command(ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, metres)
        self.wait(f"{metres} m", lambda m: m.relative_alt / 1000 >= metres - 0.5,
                  "GLOBAL_POSITION_INT", 180)  # fmt: skip

    def land(self) -> None:
        self.mode("LAND")
        self.wait("touchdown", lambda m: m.relative_alt < 500, "GLOBAL_POSITION_INT", 300)
        armed = ML.MAV_MODE_FLAG_SAFETY_ARMED
        self.wait("disarm", lambda m: not m.base_mode & armed, "HEARTBEAT", 120)
        self.hold(3)  # let the logger flush

    def velocity(self, north: float, east: float, yaw_deg: float, seconds: float) -> None:
        """Hold a velocity and heading in GUIDED, resent every 0.5 s of simulated time."""
        for _ in range(round(seconds / 0.5)):
            self.conn.mav.set_position_target_local_ned_send(
                0, self.sys, self.comp, ML.MAV_FRAME_LOCAL_NED, VELOCITY_YAW,
                0, 0, 0, north, east, 0, 0, 0, 0, math.radians(yaw_deg), 0,
            )  # fmt: skip
            self.hold(0.5)


# --- the flights -------------------------------------------------------------------------------


def hover(sim: Simulator, _motors: int) -> None:
    sim.take_off(15)
    sim.hold(30)
    sim.land()


LEG_SPEEDS = (3.0, 6.0, 9.0)  # m/s


def legs(sim: Simulator, _motors: int) -> None:
    sim.take_off(20)
    for speed in LEG_SPEEDS:
        for k in range(8):
            course = math.radians(45.0 * k)
            yaw = (45.0 * k + 30.0 * (k % 3)) % 360.0
            sim.velocity(speed * math.cos(course), speed * math.sin(course), yaw, 6)
    for k in range(8):
        sim.velocity(0, 0, 45.0 * k, 5)
    sim.land()


def motors(sim: Simulator, count: int) -> None:
    sim.take_off(15)
    sim.hold(10)
    for k in range(count):
        sim.set_sim("SIM_ENGINE_FAIL", float(1 << k))
        sim.hold(12)
        sim.set_sim("SIM_ENGINE_FAIL", 0)
        sim.hold(8)
    sim.land()


FLIGHTS = {"hover": hover, "legs": legs, "motors": motors}
EXTRA = {"LOG_DISARMED": 0.0, "SIM_ENGINE_MUL": 0.0, "SIM_ENGINE_FAIL": 0.0}


def fly(ardupilot: Path, model: Path, frame: str, params: Path, flight: str, out: Path,
        home: str = HOME, speedup: int = 10) -> Path:  # fmt: skip
    """Fly one flight of the model and copy its log to `out`."""
    count = int(json.loads(model.read_text(encoding="utf-8"))["num_motors"])
    sim = Simulator(ardupilot, model, frame, params, EXTRA, home, speedup)
    log: Path | None = None
    try:
        FLIGHTS[flight](sim, count)
    finally:
        log = sim.close()
    if log is None:
        raise FlightFailed("the simulator wrote no log")
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(log, out)
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--ardupilot", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--frame", required=True, help="the simulator's frame, e.g. octa-quad")
    parser.add_argument("--params", type=Path, required=True)
    parser.add_argument("--flight", choices=sorted(FLIGHTS), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--home", default=HOME, help="lat,lon,altitude,heading")
    parser.add_argument("--speedup", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        log = fly(args.ardupilot, args.model, args.frame, args.params, args.flight, args.out,
                  args.home, args.speedup)  # fmt: skip
    except FlightFailed as e:
        print(f"fly_sitl: {e}", file=sys.stderr)
        return 1
    print(f"log: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
