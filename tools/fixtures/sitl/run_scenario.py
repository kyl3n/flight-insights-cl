#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pymavlink==2.4.50", "pyyaml==6.0.2"]
# ///
"""Fly one scenario in ArduPilot SITL and save its log, tlog, parameters and label.

Runs the SITL binary straight from an ArduPilot build, without MAVProxy or
sim_vehicle.py. The tlog is recorded by this script on the ground-station
link, as a ground station would. Outputs are unscrubbed: pass them through
tools/fixtures/scrub_fixture.py before they go anywhere near the repository.

Usage:
  uv run tools/fixtures/sitl/run_scenario.py --ardupilot DIR --version 4.7.0 \
      --out DIR SCENARIO [SCENARIO ...]
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from pymavlink import mavutil

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenarios import SCENARIOS, Scenario

# ArduPilot's usual SITL location (a public flying field). The scrubber moves it anyway.
SITL_HOME = "-35.363261,149.165230,584,353"
SPEEDUP = 10
SERIAL0 = ("127.0.0.1", 5760)
# A ground station's heartbeat, in wall time: 0.5 s of simulated time at the default
# speed-up, well inside the GCS failsafe's 5 s
HEARTBEAT_S = 0.05


class ScenarioFailed(Exception):
    pass


class Vehicle:
    """A thin pymavlink wrapper with the few actions the scenarios need."""

    def __init__(self, tlog: Path) -> None:
        self.conn = self._connect()
        # "ArduPilot Ready" is sent once at boot, possibly before any wait below runs,
        # so watch every message from the moment the link opens.
        self.ready = False
        self.texts: list[str] = []  # status texts received, for diagnosing failures
        self.beating = False  # sending a ground station's heartbeats (see heartbeats)
        self.last_beat = 0.0
        self.conn.message_hooks.append(self._watch)
        self.conn.setup_logfile(str(tlog))
        self.conn.wait_heartbeat(timeout=60)
        self.sys, self.comp = self.conn.target_system, self.conn.target_component
        self.conn.mav.request_data_stream_send(
            self.sys, self.comp, mavutil.mavlink.MAV_DATA_STREAM_ALL, 4, 1
        )

    def _watch(self, _conn: object, msg: object) -> None:
        # Sent as messages arrive, on this thread, so they never interleave with another send
        if self.beating and time.monotonic() - self.last_beat >= HEARTBEAT_S:
            self.last_beat = time.monotonic()
            self.conn.mav.heartbeat_send(
                mavutil.mavlink.MAV_TYPE_GCS, mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0
            )
        kind = getattr(msg, "get_type", lambda: "")()
        if kind == "STATUSTEXT":
            text = str(getattr(msg, "text", ""))
            self.texts = [*self.texts[-29:], text]
            if "ArduPilot Ready" in text:
                self.ready = True
        elif kind == "SYS_STATUS":
            # Pre-arm checks only pass once start-up has finished.
            health = int(getattr(msg, "onboard_control_sensors_health", 0))
            if health & mavutil.mavlink.MAV_SYS_STATUS_PREARM_CHECK:
                self.ready = True

    def heartbeats(self, on: bool) -> None:
        """Send a ground station's heartbeats, as this link's system ID (255, the
        autopilot's MAV_GCS_SYSID), so the GCS failsafe has a ground station to lose; stop
        them to lose it. Nothing else this script sends counts as the GCS being heard."""
        self.beating = on

    def wait_ready(self, timeout: float = 120) -> None:
        end = time.monotonic() + timeout
        while not self.ready:
            if time.monotonic() > end:
                raise ScenarioFailed("timed out waiting for start-up")
            self.conn.recv_match(blocking=True, timeout=0.5)

    @staticmethod
    def _connect() -> mavutil.mavfile:
        # Connect once, retrying until SITL listens: SITL boots for its first client,
        # so a throwaway probe connection would take the start-up messages with it.
        return mavutil.mavlink_connection(
            f"tcp:{SERIAL0[0]}:{SERIAL0[1]}",
            source_system=255,
            source_component=190,
            retries=60,
        )

    def pump(self, seconds: float) -> None:
        """Keep reading (and so recording) messages for a while of wall time."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.conn.recv_match(blocking=True, timeout=0.2)

    def hold(self, seconds: float) -> None:
        """Wait for `seconds` of simulated time, which runs faster than the wall clock."""
        start = None
        while True:
            m = self.conn.recv_match(type="ATTITUDE", blocking=True, timeout=10)
            if m is None:
                raise ScenarioFailed("no ATTITUDE messages")
            if start is None:
                start = m.time_boot_ms
            elif m.time_boot_ms - start >= seconds * 1000:
                return

    def wait(
        self, what: str, check: Callable[[object], bool], msg_type: str, timeout: float
    ) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            m = self.conn.recv_match(type=msg_type, blocking=True, timeout=0.5)
            if m is not None and check(m):
                return
        raise ScenarioFailed(f"timed out waiting for {what}")

    def command(self, cmd: int, *params: float) -> None:
        p = [*params, 0, 0, 0, 0, 0, 0, 0][:7]
        self.conn.mav.command_long_send(self.sys, self.comp, cmd, 0, *p)

    def get_param(self, name: str) -> float:
        self.conn.mav.param_request_read_send(self.sys, self.comp, name.encode(), -1)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            m = self.conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
            if m is not None and m.param_id == name:
                return float(m.param_value)
        raise ScenarioFailed(f"no value for {name}")

    def set_param(self, name: str, value: float) -> None:
        for _ in range(5):
            self.conn.mav.param_set_send(
                self.sys, self.comp, name.encode(), value, mavutil.mavlink.MAV_PARAM_TYPE_REAL32
            )
            end = time.monotonic() + 3
            while time.monotonic() < end:
                m = self.conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
                if m is not None and m.param_id == name and abs(m.param_value - value) < 1e-3:
                    return
        raise ScenarioFailed(f"could not set {name}")

    def mode(self, name: str) -> None:
        mapping = self.conn.mode_mapping()
        if name not in mapping:
            raise ScenarioFailed(f"unknown mode {name}")
        # The simulated radio can come up just after the mode is set, and its mode
        # switch's first reading sets a mode of its own, so the mode must also hold.
        for _ in range(5):
            self.conn.set_mode(mapping[name])
            self.wait(
                f"mode {name}",
                lambda m: m.custom_mode == mapping[name],  # type: ignore[attr-defined]
                "HEARTBEAT",
                30,
            )
            held, end = 0, time.monotonic() + 30
            while held < 3 and time.monotonic() < end:
                m = self.conn.recv_match(type="HEARTBEAT", blocking=True, timeout=1)
                if m is None or m.get_srcSystem() != self.sys:
                    continue
                if m.custom_mode != mapping[name]:
                    break
                held += 1
            if held == 3:
                return
        raise ScenarioFailed(f"mode {name} did not hold")

    def rc(self, **channels: int) -> None:
        values = [65535] * 18  # 65535 = leave the channel alone
        for ch, pwm in channels.items():
            values[int(ch.removeprefix("ch")) - 1] = pwm
        self.conn.mav.rc_channels_override_send(self.sys, self.comp, *values)

    def arm(self, timeout: float = 180) -> None:
        """Arm once the pre-arm checks pass (the estimator needs a while after boot).

        Every arming attempt starts the log, so waiting first keeps the log to the flight.
        """
        prearm = mavutil.mavlink.MAV_SYS_STATUS_PREARM_CHECK
        self.wait(
            "pre-arm checks",
            lambda m: m.onboard_control_sensors_health & prearm,  # type: ignore[attr-defined]
            "SYS_STATUS",
            timeout,
        )
        # The checks can pass before the estimator uses GPS; a take-off sent then is
        # refused, and the vehicle disarms itself on the ground.
        position = mavutil.mavlink.EKF_POS_HORIZ_ABS
        self.wait(
            "an absolute position",
            lambda m: m.flags & position,  # type: ignore[attr-defined]
            "EKF_STATUS_REPORT",
            timeout,
        )
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.command(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 1)
            try:
                self.wait(
                    "arming",
                    lambda m: m.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED,  # type: ignore[attr-defined]
                    "HEARTBEAT",
                    2,
                )
                return
            except ScenarioFailed:
                continue
        raise ScenarioFailed("vehicle would not arm")

    def disarm(self, force: bool = False) -> None:
        self.command(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0, 21196 if force else 0)
        self.wait_disarmed(30)

    def wait_disarmed(self, timeout: float) -> None:
        self.wait(
            "disarm",
            lambda m: not m.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED,  # type: ignore[attr-defined]
            "HEARTBEAT",
            timeout,
        )

    def wait_altitude(self, metres: float, timeout: float = 120) -> None:
        self.wait(
            f"altitude {metres} m",
            lambda m: m.relative_alt / 1000 >= metres,  # type: ignore[attr-defined]
            "GLOBAL_POSITION_INT",
            timeout,
        )

    def upload_mission(self, items: list[tuple[int, int, float, float, float]]) -> None:
        """items: (frame, command, latitude, longitude, altitude); item 0 is home."""
        self.conn.mav.mission_count_send(self.sys, self.comp, len(items))
        sent: set[int] = set()
        end = time.monotonic() + 30
        while time.monotonic() < end:
            m = self.conn.recv_match(
                type=["MISSION_REQUEST", "MISSION_REQUEST_INT", "MISSION_ACK"],
                blocking=True,
                timeout=0.5,
            )
            if m is None:
                continue
            if m.get_type() == "MISSION_ACK":
                if m.type != mavutil.mavlink.MAV_MISSION_ACCEPTED or len(sent) != len(items):
                    raise ScenarioFailed(f"mission rejected ({m.type})")
                return
            frame, cmd, lat, lng, alt = items[m.seq]
            self.conn.mav.mission_item_int_send(
                self.sys, self.comp, m.seq, frame, cmd, 0, 1,
                15 if cmd == mavutil.mavlink.MAV_CMD_NAV_TAKEOFF else 0, 0, 0, 0,
                round(lat * 1e7), round(lng * 1e7), alt,
            )  # fmt: skip
            sent.add(m.seq)
        raise ScenarioFailed("mission upload timed out")

    def fetch_params(self) -> list[tuple[str, float, int]]:
        """Every parameter as (name, value, MAV_PARAM_TYPE), in index order."""
        self.conn.mav.param_request_list_send(self.sys, self.comp)
        got: dict[int, tuple[str, float, int]] = {}
        count = None
        idle_until = time.monotonic() + 10
        while count is None or len(got) < count:
            m = self.conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
            if m is not None:
                count = m.param_count
                if 0 <= m.param_index < count:
                    got[m.param_index] = (m.param_id, float(m.param_value), m.param_type)
                idle_until = time.monotonic() + 3
            elif time.monotonic() > idle_until:
                if count is None:
                    raise ScenarioFailed("no parameters received")
                for i in set(range(count)) - set(got):
                    self.conn.mav.param_request_read_send(self.sys, self.comp, b"", i)
                idle_until = time.monotonic() + 3
        return [got[i] for i in sorted(got)]

    def close(self) -> None:
        self.conn.close()


def format_value(value: float) -> str:
    """The shortest text that reads back as the same float32, as the autopilot holds it.

    Seven significant digits are not always enough: 25.018352 needs eight.
    """
    stored = struct.unpack("<f", struct.pack("<f", value))[0]
    for digits in range(6, 10):
        text = f"{stored:.{digits}g}"
        if struct.unpack("<f", struct.pack("<f", float(text)))[0] == stored:
            return text
    return repr(stored)


def write_param_files(params: list[tuple[str, float, int]], out: Path, about: str) -> None:
    """Mission Planner .param (NAME,VALUE) and QGroundControl .params (tab-separated)."""
    ordered = sorted(params)
    out.with_suffix(".param").write_text(
        "".join(f"{name},{format_value(value)}\n" for name, value, _ in ordered), newline="\n"
    )
    header = (
        f"# Onboard parameters for Vehicle 1\n#\n# {about}\n#\n"
        "# Vehicle-Id Component-Id Name Value Type\n"
    )
    out.with_suffix(".params").write_text(
        header + "".join(f"1\t1\t{n}\t{format_value(v)}\t{t}\n" for n, v, t in ordered),
        newline="\n",
    )


def _diagnose(proc: subprocess.Popen[bytes], sitl_out: Path, vehicle: Vehicle | None) -> str:
    """What SITL printed and said, to explain a failure in CI logs."""
    code = proc.poll()
    lines = sitl_out.read_text(errors="replace").splitlines()[-25:]
    texts = vehicle.texts if vehicle else []
    return "\n".join(
        [
            f"  SITL {'exited with ' + str(code) if code is not None else 'still running'}",
            "  SITL output (last lines):",
            *(f"    {line}" for line in lines),
            "  status texts received:",
            *(f"    {t}" for t in texts),
        ]
    )


def load_frame_model(path: Path) -> dict[str, float]:
    """One of ArduPilot's SITL frame models: JSON with # comments."""
    text = "\n".join(line.split("#", 1)[0] for line in path.read_text().splitlines())
    return json.loads(text)


def carry_payload(model: dict[str, float], kg: float) -> dict[str, float]:
    """The same airframe carrying `kg` more. SITL sizes the motors from the model's
    mass, hover throttle and hover current, so those move together and the motors stay
    as they were: hover throttle scales with the mass, hover power with its 1.5 power
    (momentum theory), and the drag reference angle keeps the same drag area."""
    if not kg:
        return model
    r = (model["mass"] + kg) / model["mass"]
    return model | {
        "mass": model["mass"] * r,
        "hoverThrOut": model["hoverThrOut"] * r,
        "refCurrent": model["refCurrent"] * r**1.5,
        "refAngle": math.degrees(math.atan(math.tan(math.radians(model["refAngle"])) / r)),
    }


def sitl_command(scenario: Scenario, ardupilot: Path, work: Path) -> list[str]:
    """The SITL command line for a scenario, run in `work`; writes the boot defaults and
    any frame model it needs there."""
    binary = ardupilot / "build" / "sitl" / "bin" / scenario.binary
    if not binary.exists():
        raise ScenarioFailed(f"{binary} not built")
    defaults = [str(ardupilot / "Tools" / "autotest" / d) for d in scenario.defaults]
    if scenario.boot_params:
        boot = work / "boot.parm"
        boot.write_text("".join(f"{k} {v}\n" for k, v in scenario.boot_params.items()))
        defaults.append(str(boot))
    model = scenario.model
    if scenario.model_json:
        source = ardupilot / "Tools" / "autotest" / "models" / scenario.model_json
        frame = work / scenario.model_json
        frame.write_text(json.dumps(carry_payload(load_frame_model(source), scenario.payload_kg)))
        model += ":" + frame.name  # SITL reads it from its working folder; a full path fails
    return [
        str(binary), "--model", model, "--speedup", str(scenario.speedup or SPEEDUP),
        "--defaults", ",".join(defaults), "--home", SITL_HOME, "-I0",
    ]  # fmt: skip


def run(scenario: Scenario, ardupilot: Path, version: str, out_dir: Path) -> None:
    import yaml  # here, so the procedure rehearsal can use Vehicle without it

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / scenario.name
    with tempfile.TemporaryDirectory() as work:
        command = sitl_command(scenario, ardupilot, Path(work))
        sitl_out = (Path(work) / "sitl.txt").open("w")
        proc = subprocess.Popen(command, cwd=work, stdout=sitl_out, stderr=subprocess.STDOUT)
        vehicle: Vehicle | None = None
        try:
            vehicle = Vehicle(stem.with_suffix(".tlog"))
            vehicle.wait_ready()
            for name, value in scenario.params.items():
                new = value(vehicle.get_param(name)) if callable(value) else value
                vehicle.set_param(name, new)
            # Download before the flight: the flight changes no parameters, and a
            # download afterwards would keep the logger running for a long tail.
            write_param_files(
                vehicle.fetch_params(),
                stem,
                f"ArduPilot {version} SITL, scenario {scenario.name}",
            )
            scenario.fly(vehicle)
            vehicle.hold(3)  # let the logger flush after disarming
            vehicle.close()
        except ScenarioFailed as e:
            sitl_out.flush()
            raise ScenarioFailed(f"{e}\n{_diagnose(proc, Path(work) / 'sitl.txt', vehicle)}") from e
        finally:
            proc.terminate()
            proc.wait(timeout=30)
            sitl_out.close()
        logs = sorted((Path(work) / "logs").glob("*.BIN"), key=lambda p: p.stat().st_mtime)
        if not logs:
            raise ScenarioFailed("SITL wrote no log")
        shutil.copyfile(logs[-1], stem.with_suffix(".bin"))
    commit = subprocess.run(
        ["git", "-C", str(ardupilot), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    label = scenario.label(version, commit or "unknown")
    stem.with_suffix(".yaml").write_text(yaml.safe_dump(label, sort_keys=False))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("scenarios", nargs="+", choices=sorted(SCENARIOS))
    parser.add_argument("--ardupilot", required=True, type=Path, help="ArduPilot checkout, built")
    parser.add_argument("--version", required=True, help="firmware version, e.g. 4.7.0")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    failed = 0
    for name in args.scenarios:
        print(f"== {name}", flush=True)
        try:
            run(SCENARIOS[name], args.ardupilot, args.version, args.out)
        except ScenarioFailed as e:
            print(f"{name} failed: {e}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
