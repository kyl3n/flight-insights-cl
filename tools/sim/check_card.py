#!/usr/bin/env python3
"""The recovery card against the simulator (spec: Simulator checks; Flying without GNSS,
"Control link lost"): ArduPilot's default quad flown from a ground station, its link cut
mid-flight, and GNSS too, and what the firmware did compared with what the card says it
will do.

Most cases fly the quad as the team flies theirs: from the ground station, the joystick's
sticks sent as MANUAL_CONTROL with the station's heartbeats (RC_OPTIONS bit 0 ignores the
receiver). It takes off in GUIDED to 10 m and flies 5 s in its start mode. Then the link
is cut: the sticks and the heartbeats stop together. Some cases also turn the simulated
GNSS off a few seconds later (SIM_GPS1_ENABLE 0, a parameter write, which the autopilot
does not count as hearing the ground station). The others fly on a pilot's receiver with
the telemetry up, and lose the receiver: no pulses (SIM_RC_FAIL 1), or its failsafe
throttle under FS_THR_VALUE (SIM_RC_FAIL 2), which the card says acts at once.

The log's mode changes made by a failsafe after the cut must be the card's, in order
(recovery_link.play, or recovery_link.both_lost with GNSS lost too):
- **The link's failsafes** each within its margin of the card's time: the radio's covers
  the sticks' spacing, the ground station's a heartbeat's and its 3 Hz check.
- **With GNSS lost too,** the last one must be where the card says the order "link first"
  ends. Two cases fly with drag fusion on (ArduPilot's own SITL drag values, as its
  DeadReckoningInWind autotest sets them) and watch longer: FS_DR_TIMEOUT 1800 keeps RTL
  going home on dead reckoning, where the card counts the way home; 1 s, shorter than the
  climb home, gives the position up and the EKF failsafe lands it.

Run from engine/, which has the app's code: uv run python ../tools/sim/check_card.py
--ardupilot DIR [--keep DIR]. Parameters are only ever written to the local simulator
this script starts.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import read_dataflash
from flight_insights.knowledge import load_pack
from flight_insights.recovery_link import State, both_lost, dead_reckoned_return, play

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "fixtures" / "sitl"))
import scenarios as sc  # noqa: E402
from run_scenario import ScenarioFailed, Vehicle, sitl_command  # noqa: E402

PACK = load_pack()
REG = Registry.load(PACK)
DOC = PACK.json("recovery/copter.json")
FAILSAFE_REASONS = ("RADIO_FAILSAFE", "GCS_FAILSAFE", "EKF_FAILSAFE", "DEADRECKON_FAILSAFE")
MARGIN_S = {"FAILSAFE_RADIO": 0.5, "FAILSAFE_GCS": 1.0}  # sticks 0.2 s; heartbeats 0.5 s, 3 Hz
NO_PULSES, FAILSAFE_THROTTLE = 1, 2  # SIM_RC_FAIL
DRAG = {"EK3_DRAG_BCOEF_X": 9.5, "EK3_DRAG_BCOEF_Y": 9.5, "EK3_DRAG_MCOEF": 0.082}
STICK_S = 0.02  # wall time between joystick messages: 0.2 s simulated at the speed-up


class Station(Vehicle):
    """A ground station flying the aircraft by joystick: its heartbeats and the sticks as
    MANUAL_CONTROL, sent as messages arrive, until the link is cut."""

    def __init__(self, tlog: Path) -> None:
        self.sticks: tuple[int, int, int, int] | None = None  # pitch, roll, throttle, yaw
        self.last_stick = 0.0
        super().__init__(tlog)

    def _watch(self, conn: object, msg: object) -> None:
        super()._watch(conn, msg)
        if self.sticks is not None and time.monotonic() - self.last_stick >= STICK_S:
            self.last_stick = time.monotonic()
            self.conn.mav.manual_control_send(self.sys, *self.sticks, 0)

    def link(self, on: bool, throttle: int = 500) -> None:
        self.heartbeats(on)
        self.sticks = (0, 0, throttle, 0) if on else None


@dataclass(frozen=True)
class Case:
    name: str
    start: str  # the mode it is flown in as the link drops
    settings: dict[str, float]
    gnss_after_s: float | None = None  # GNSS off this long after the link
    about: str = ""
    receiver: int = 0  # SIM_RC_FAIL for a pilot's receiver lost; 0: the station's link cut
    boot: dict[str, float] = field(default_factory=lambda: {"LOG_DISARMED": 0, "RC_OPTIONS": 1})
    watch_s: float = 20.0  # how long after the cut the log is read
    flown: tuple[float, float] | None = None  # the range and longest flight the card plays

    @property
    def station(self) -> bool:
        return self.receiver == 0


CASES = (
    Case("rtl", "LOITER", {"FS_THR_ENABLE": 1, "FS_GCS_ENABLE": 1},
         about="the radio failsafe's RTL first; the ground station's RTL then changes nothing"),
    Case("land-then-rtl", "LOITER", {"FS_THR_ENABLE": 3, "FS_GCS_ENABLE": 1},
         about="the radio failsafe lands it, then the ground station's failsafe takes it to RTL"),
    Case("pilot-control", "ALT_HOLD",
         {"FS_THR_ENABLE": 0, "FS_GCS_ENABLE": 1, "FS_OPTIONS": 16},
         about="the radio failsafe off, and 'continue under pilot control': nothing changes"),
    Case("guided", "GUIDED", {"FS_THR_ENABLE": 0, "FS_GCS_ENABLE": 1, "FS_OPTIONS": 16},
         about="the same settings in Guided: the ground station's RTL"),
    Case("gnss-too", "LOITER", {"FS_THR_ENABLE": 1, "FS_GCS_ENABLE": 1}, gnss_after_s=3,
         about="RTL on the radio failsafe, then GNSS lost: the EKF failsafe lands it"),
    Case("gnss-too-althold", "LOITER",
         {"FS_THR_ENABLE": 0, "FS_GCS_ENABLE": 1, "FS_EKF_ACTION": 2}, gnss_after_s=7,
         about="the radio failsafe off and the EKF failsafe's AltHold: no one flies it"),
    Case("dead-reckoned-home", "LOITER",
         {"FS_THR_ENABLE": 1, "FS_GCS_ENABLE": 1, **DRAG, "FS_DR_ENABLE": 2,
          "FS_DR_TIMEOUT": 1800}, gnss_after_s=3, watch_s=40, flown=(20.0, 120.0),
         about="RTL on the radio failsafe, then GNSS lost: home on dead reckoning, no Land"),
    Case("dead-reckoned-short", "LOITER",
         {"FS_THR_ENABLE": 1, "FS_GCS_ENABLE": 1, **DRAG, "FS_DR_ENABLE": 2,
          "FS_DR_TIMEOUT": 1}, gnss_after_s=3, watch_s=25, flown=(20.0, 120.0),
         about="the same with FS_DR_TIMEOUT 1 s: the EKF failsafe lands it on the way"),
    Case("receiver-silent", "GUIDED", {"FS_THR_ENABLE": 1, "FS_GCS_ENABLE": 1},
         receiver=NO_PULSES, boot={"LOG_DISARMED": 0, "RC_OPTIONS": 0},
         about="a pilot's receiver that stops sending: RTL after RC_FS_TIMEOUT"),
    Case("receiver-throttle", "GUIDED", {"FS_THR_ENABLE": 1, "FS_GCS_ENABLE": 1},
         receiver=FAILSAFE_THROTTLE, boot={"LOG_DISARMED": 0, "RC_OPTIONS": 0},
         about="a receiver sending its failsafe throttle under FS_THR_VALUE: RTL at once"),
)  # fmt: skip


def fly(case: Case, ardupilot: Path, work: Path) -> tuple[Path, float, dict[str, float]]:
    """The case flown: its log, the cut's time on the autopilot's clock (s since boot) and
    the parameters as the log holds them."""
    scenario = sc.Scenario(
        name=case.name, vehicle="copter", frame="quad +", description=case.about, expected=[],
        fly=lambda _v: None, boot_params=case.boot | case.settings, **sc.COPTER,  # type: ignore[arg-type]
    )  # fmt: skip
    folder = work / case.name
    folder.mkdir(parents=True, exist_ok=True)
    out = (folder / "sitl.txt").open("w")
    proc = subprocess.Popen(sitl_command(scenario, ardupilot, folder), cwd=folder, stdout=out,
                            stderr=subprocess.STDOUT)  # fmt: skip
    v = Station(folder / f"{case.name}.tlog")
    try:
        v.heartbeats(True)
        if case.station:
            v.link(True, throttle=0)  # the station is up from start-up: no receiver to find
        v.wait_ready()
        if v.get_param("SIM_SPEEDUP") <= 0:  # only the simulator has SIM_ parameters
            raise ScenarioFailed("not a simulator")
        v.mode("GUIDED")
        v.arm()
        v.command(sc.ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 10)
        v.wait_altitude(9.5)
        if case.station:
            v.link(True)  # throttle centred: hold height in the pilot's modes
        v.hold(2)
        v.mode(case.start)
        v.hold(5)
        m = v.conn.recv_match(type="ATTITUDE", blocking=True, timeout=10)
        if m is None:
            raise ScenarioFailed("no ATTITUDE messages")
        cut = float(m.time_boot_ms) / 1000  # type: ignore[attr-defined]
        if case.station:
            v.link(False)
        else:  # the telemetry stays up: only the receiver is lost
            v.set_param("SIM_RC_FAIL", case.receiver)
        if case.gnss_after_s is not None:
            v.hold(case.gnss_after_s)
            v.set_param("SIM_GPS1_ENABLE", 0)
            v.hold(case.watch_s - case.gnss_after_s)
        else:
            v.hold(case.watch_s)
        if case.station:
            v.link(True)
        else:
            v.set_param("SIM_RC_FAIL", 0)
        v.hold(1)
        if case.gnss_after_s is not None:
            v.set_param("SIM_GPS1_ENABLE", 1)
        try:
            sc.land_and_disarm(v)
        except ScenarioFailed:
            v.disarm(force=True)
        v.hold(3)
        v.close()
    except ScenarioFailed as e:
        raise ScenarioFailed(f"{case.name}: {e}; last messages {v.texts[-5:]}") from e
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        out.close()
    logs = sorted((folder / "logs").glob("*.BIN"), key=lambda p: p.stat().st_mtime)
    if not logs:
        raise ScenarioFailed(f"{case.name}: the simulator wrote no log")
    log = folder / f"{case.name}.bin"
    shutil.copyfile(logs[-1], log)
    parm = read_dataflash(log).table("PARM")
    params: dict[str, float] = {}
    for name, value in zip(parm.column("Name").to_pylist(), parm.column("Value").to_pylist(),
                           strict=True):  # fmt: skip
        params[bytes(name).rstrip(b"\0").decode()] = float(value)  # 16 bytes, null-padded
    return log, cut, params


def observed(log: Path, cut: float, watch_s: float) -> list[tuple[float, str, str]]:
    """The failsafes' mode changes after the cut: seconds after it, mode and reason."""
    names: dict[str, Any] = REG.names("4.7.0")
    modes, reasons = names["modes"]["copter"], names["mode_reasons"]
    table = read_dataflash(log).table("MODE")
    out: list[tuple[float, str, str]] = []
    for t, n, r in zip(table.column("TimeUS").to_pylist(), table.column("ModeNum").to_pylist(),
                       table.column("Rsn").to_pylist(), strict=True):  # fmt: skip
        after = round(int(t) / 1e6 - cut, 2)
        reason = reasons.get(str(r), str(r))
        if 0 <= after <= watch_s and reason in FAILSAFE_REASONS:
            out.append((after, modes.get(str(n), str(n)), reason))
    return out


def check(case: Case, log: Path, cut: float, params: dict[str, float]) -> list[str]:
    rules: dict[str, Any] = REG.rules["failsafes"]
    p = dict(DOC["link"]["defaults"]) | params
    telemetry = case.station  # a receiver's loss leaves the station's link up
    steps = play(rules, p, State(case.start), True, telemetry)
    wanted: list[tuple[float | None, str, float]] = []  # when, the mode, the margin
    mode = case.start
    for s in steps:
        if s.state.mode != mode:
            at = (
                0.0
                if case.receiver == FAILSAFE_THROTTLE and s.failsafe == "FAILSAFE_RADIO"
                else s.after_s
            )  # the card's "act at once instead"
            wanted.append((at, s.state.mode, MARGIN_S.get(s.failsafe, 1.0)))
            mode = s.state.mode
    if case.gnss_after_s is not None:
        drag = p.get("EK3_DRAG_BCOEF_X", 0) > 1 or p.get("EK3_DRAG_MCOEF", 0) > 0.001
        ekf_on = int(p.get("FS_EKF_ACTION", 1)) != 0 and p.get("FS_EKF_THRESH", 0.8) != 0
        back = None if case.flown is None else dead_reckoned_return(
            p, DOC["link"]["defaults"], *case.flown
        )  # fmt: skip
        link_first = both_lost(rules, p, State(case.start), True, telemetry, drag, ekf_on,
                               back)[0]  # fmt: skip
        if link_first.mode != mode:
            wanted.append((None, link_first.mode, 0.0))
    seen = observed(log, cut, case.watch_s)
    print(f"{case.name}: {case.about}")
    print(f"  card  {[(t, m) for t, m, _ in wanted]}")
    print(f"  log   {[(t, m, r) for t, m, r in seen]}")
    problems: list[str] = []
    if [m for _, m, _ in wanted] != [m for _, m, _ in seen]:
        problems.append(f"{case.name}: the card's modes {[m for _, m, _ in wanted]} against "
                        f"the log's {[m for _, m, _ in seen]}")  # fmt: skip
    else:
        for (t, m, margin), (got, _, _) in zip(wanted, seen, strict=True):
            if t is not None and abs(got - t) > margin:
                problems.append(f"{case.name}: {m} at {got} s against the card's {t} s")
    print(("  FAIL  " + "; ".join(problems)) if problems else "  ok    as the card says")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--ardupilot", type=Path, required=True)
    parser.add_argument("--keep", type=Path, help="keep the logs in this folder")
    parser.add_argument("cases", nargs="*", help="the cases to fly (all by default)")
    args = parser.parse_args(argv)
    chosen = [c for c in CASES if not args.cases or c.name in args.cases]
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = args.keep or Path(tmp)
        for case in chosen:
            try:
                problems += check(case, *fly(case, args.ardupilot, work))
            except ScenarioFailed as e:
                print(f"  FAIL  {e}")
                problems.append(str(e))
    print(json.dumps({"problems": problems}, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
