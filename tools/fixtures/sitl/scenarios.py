"""SITL scenarios for test fixtures. Each is short, so its files stay small."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pymavlink import mavutil

if TYPE_CHECKING:
    from run_scenario import Vehicle

ML = mavutil.mavlink
HOME_LAT, HOME_LNG = -35.363261, 149.165230


@dataclass(frozen=True)
class SetBits:
    """A parameter change that sets bits in the vehicle's current value."""

    bits: int
    note: str

    def __call__(self, current: float) -> float:
        return int(current) | self.bits


LOG_IMU_RAW = SetBits(1 << 19, "default with bit 19 (raw IMU) set")


@dataclass(frozen=True)
class Scenario:
    name: str
    vehicle: str
    frame: str
    binary: str
    model: str
    defaults: list[str]
    description: str
    expected: list[str]
    fly: Callable[[Vehicle], None]
    params: dict[str, float | SetBits] = field(default_factory=dict)

    def label(self, version: str, commit: str) -> dict[str, Any]:
        return {
            "source": "sitl",
            "vehicle": self.vehicle,
            "frame": self.frame,
            "firmware": version,
            "ardupilot_commit": commit,
            "scenario": self.name,
            "description": self.description,
            "parameters_set": {
                k: v.note if isinstance(v, SetBits) else v for k, v in self.params.items()
            },
            "faults": [],
            "expected": self.expected,
        }


def offset(north_m: float, east_m: float) -> tuple[float, float]:
    """A point near the SITL home, north and east in metres."""
    lat = HOME_LAT + north_m / 111_320.0
    lng = HOME_LNG + east_m / (111_320.0 * math.cos(math.radians(HOME_LAT)))
    return lat, lng


def land_and_disarm(v: Vehicle, mode: str = "LAND") -> None:
    v.mode(mode)
    v.wait(
        "touchdown",
        lambda m: m.relative_alt < 500,  # type: ignore[attr-defined]
        "GLOBAL_POSITION_INT",
        180,
    )
    v.wait_disarmed(120)


def copter_hover(seconds: float) -> Callable[[Vehicle], None]:
    def fly(v: Vehicle) -> None:
        v.mode("GUIDED")
        v.arm()
        v.command(ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 10)
        v.wait_altitude(9.5)
        v.hold(seconds)
        land_and_disarm(v)

    return fly


def heli_hover(v: Vehicle) -> None:
    v.rc(ch3=1000, ch8=1000)  # motor interlock off to arm
    v.mode("GUIDED")
    v.arm()
    v.rc(ch8=2000)  # interlock on: rotor run-up
    v.hold(15)
    v.command(ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 10)
    v.wait_altitude(9.5)
    v.hold(20)
    v.mode("LAND")
    v.wait("touchdown", lambda m: m.relative_alt < 500, "GLOBAL_POSITION_INT", 180)  # type: ignore[attr-defined]
    v.hold(5)
    v.rc(ch8=1000)
    v.disarm()


def plane_circuit(v: Vehicle) -> None:
    north = offset(500, 0)
    east = offset(500, 500)
    back = offset(0, 300)
    rel = ML.MAV_FRAME_GLOBAL_RELATIVE_ALT
    v.upload_mission(
        [
            (ML.MAV_FRAME_GLOBAL, ML.MAV_CMD_NAV_WAYPOINT, HOME_LAT, HOME_LNG, 0),
            (rel, ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 50),
            (rel, ML.MAV_CMD_NAV_WAYPOINT, *north, 80),
            (rel, ML.MAV_CMD_NAV_WAYPOINT, *east, 80),
            (rel, ML.MAV_CMD_NAV_WAYPOINT, *back, 80),
        ]
    )
    v.mode("AUTO")
    v.arm()
    v.wait_altitude(45, timeout=180)
    v.hold(60)
    v.mode("RTL")
    v.hold(20)
    v.disarm(force=True)  # end of scenario, still airborne


def quadplane_hover(v: Vehicle) -> None:
    v.rc(ch3=1000)
    v.mode("QLOITER")
    v.arm()
    v.rc(ch3=1800)
    v.wait_altitude(10)
    v.rc(ch3=1500)  # mid stick holds height
    v.hold(20)
    land_and_disarm(v, "QLAND")


COPTER = dict(binary="arducopter", model="+", defaults=["default_params/copter.parm"])

SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario(
            name="copter-quad-hover",
            vehicle="copter",
            frame="quad +",
            description="GUIDED take-off to 10 m, 20 s hover, LAND.",
            expected=[
                "one armed period",
                "take-off to about 10 m in GUIDED",
                "about 20 s of steady hover",
                "landing in LAND, then automatic disarm",
            ],
            fly=copter_hover(20),
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-rawimu",
            vehicle="copter",
            frame="quad +",
            description="Short hover with pre- and post-filter primary gyro logged at full rate.",
            expected=[
                "INS_RAW_LOG_OPT = 9: primary gyro, pre- and post-filter",
                "raw IMU messages for the whole armed period",
                "about 10 s of steady hover",
            ],
            fly=copter_hover(10),
            params={
                "INS_RAW_LOG_OPT": 9,
                "LOG_BITMASK": LOG_IMU_RAW,
                "SIM_ESC_TELEM": 0,  # not needed here; keeps the log small
            },
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="heli-hover",
            vehicle="heli",
            frame="single-rotor heli",
            binary="arducopter-heli",
            model="heli",
            defaults=["default_params/copter-heli.parm"],
            description="Rotor run-up, GUIDED take-off to 10 m, 20 s hover, LAND.",
            expected=[
                "motor interlock enabled before take-off",
                "about 15 s of rotor run-up on the ground",
                "about 20 s of steady hover",
                "landing in LAND, interlock off, disarm",
            ],
            fly=heli_hover,
            params={"RC_OVERRIDE_TIME": -1},
        ),
        Scenario(
            name="plane-circuit",
            vehicle="plane",
            frame="conventional fixed-wing",
            binary="arduplane",
            model="plane",
            defaults=["models/plane.parm"],
            description="AUTO take-off and a three-waypoint circuit, then RTL.",
            expected=[
                "AUTO take-off to 50 m",
                "a circuit of three waypoints at 80 m",
                "RTL for about 20 s",
                "the log ends with a forced disarm in the air (end of scenario, not a fault)",
            ],
            fly=plane_circuit,
        ),
        Scenario(
            name="quadplane-hover",
            vehicle="quadplane",
            frame="quadplane, quad +",
            binary="arduplane",
            model="quadplane",
            defaults=["default_params/quadplane.parm"],
            description="QLOITER climb to 10 m on the VTOL motors, 20 s hover, QLAND.",
            expected=[
                "VTOL flight only, no transition",
                "about 20 s of hover in QLOITER",
                "landing in QLAND, then automatic disarm",
            ],
            fly=quadplane_hover,
            params={"RC_OVERRIDE_TIME": -1},
        ),
    ]
}
