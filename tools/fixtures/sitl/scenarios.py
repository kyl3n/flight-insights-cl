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
    boot_params: dict[str, float] = field(default_factory=dict)  # read at boot, as defaults
    # A frame model from ArduPilot's Tools/autotest/models, flown carrying a payload.
    model_json: str | None = None
    payload_kg: float = 0.0
    speedup: int | None = None  # slower than the runner's default where logging is heavy
    faults: list[dict[str, Any]] = field(default_factory=list)  # planted on purpose, for the label

    def label(self, version: str, commit: str) -> dict[str, Any]:
        return {
            "source": "sitl",
            "vehicle": self.vehicle,
            "frame": self.frame,
            "firmware": version,
            "ardupilot_commit": commit,
            "scenario": self.name,
            "description": self.description,
            "parameters_set": self.boot_params
            | {k: v.note if isinstance(v, SetBits) else v for k, v in self.params.items()},
            **({"payload": {"mass_kg": self.payload_kg}} if self.payload_kg else {}),
            "faults": self.faults,
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


def copter_hover(
    seconds: float, altitude: float = 10, raw_in_hover: bool = False
) -> Callable[[Vehicle], None]:
    """Take off, hover, land. With `raw_in_hover`, raw IMU logging runs during the hover
    only, as for a filter test flight: a slow heavy-lift climb and descent would
    otherwise fill the log."""

    def fly(v: Vehicle) -> None:
        v.mode("GUIDED")
        v.arm()
        v.command(ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, altitude)
        v.wait_altitude(altitude - 0.5)
        if raw_in_hover:
            bitmask = v.get_param("LOG_BITMASK")
            v.set_param("LOG_BITMASK", LOG_IMU_RAW(bitmask))
        v.hold(seconds)
        if raw_in_hover:
            v.set_param("LOG_BITMASK", bitmask)
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

# Motor vibration and ESC telemetry as ArduPilot's DynamicRpmNotches autotest simulates
# them, with raw IMU logging, so filter analysis has a motor peak to find and both the
# pre- and post-filter gyro to check its prediction against.
VIBRATING = {
    "SIM_VIB_MOT_MAX": 350,
    "SIM_GYR1_RND": 20,
    "SIM_ESC_TELEM": 1,
    "INS_RAW_LOG_OPT": 9,
    "LOG_DISARMED": 0,
    "LOG_FILE_BUFSIZE": 64,
}
RAW_SPEEDUP = 2  # full-rate raw IMU logging drops data at the default speed-up

# ArduPilot's heavy-lift SITL model: Callisto, a 32.5 kg coaxial octocopter on 30-inch
# props, with the autotest's own defaults (which turn hover learning off).
CALLISTO = dict(
    binary="arducopter",
    model="octa-quad",
    model_json="Callisto.json",
    defaults=[
        "default_params/copter.parm",
        "default_params/copter-octaquad.parm",
        "models/Callisto.param",
    ],
    speedup=RAW_SPEEDUP,
)
# Eight motors add their vibration together: a quarter of the quad's per-motor amplitude
# leaves a clear motor peak, where the full amount shook the simulated X8 off its hover.
X8_VIBRATION = {"SIM_GYR1_RND": 5}
# The notch follows throttle, though ESC RPM arrives: the better option is to track it.
# Set up as an engineer would on the light flight (motor peak about 188 Hz at hover
# throttle 0.375); carrying the payload, throttle tracking then runs behind the motors.
THROTTLE_NOTCH = {
    "INS_HNTCH_ENABLE": 1,
    "INS_HNTCH_MODE": 1,
    "INS_HNTCH_FREQ": 180,
    "INS_HNTCH_REF": 0.375,
    "INS_HNTCH_BW": 90,
    "INS_HNTCH_HMNCS": 3,
    "INS_HNTCH_ATT": 40,
}
HEAVY_LIFT_FAULTS = [
    {
        "kind": "filter-setting",
        "description": "harmonic notch tracks throttle although ESC RPM telemetry is present",
        "parameters": ["INS_HNTCH_MODE"],
    },
    {
        "kind": "parameter-error",
        "description": "hover-throttle learning off on an aircraft flown with and without payload",
        "parameters": ["MOT_HOVER_LEARN"],
    },
]
HEAVY_LIFT_EXPECTED = [
    "one armed period with about 14 s of steady hover at 5 m",
    "raw IMU logged during the hover only",
    "ESC RPM from all eight motors",
    "a motor vibration peak in the pre-filter gyro",
    "a throttle-tracking notch: the better option is ESC RPM tracking (INS_HNTCH_MODE 3)",
    "MOT_HOVER_LEARN 0: hover learning should be on for a variable payload",
]

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
            description=(
                "Short hover with simulated motor vibration, ESC RPM and an RPM-tracking"
                " harmonic notch, with pre- and post-filter primary gyro logged at full rate."
            ),
            expected=[
                "INS_RAW_LOG_OPT = 9: primary gyro, pre- and post-filter",
                "raw IMU messages for the whole armed period, with no logging gaps",
                "about 6 s of steady hover at 4 m",
                "a motor vibration peak in the pre-filter gyro, attenuated after the filters",
                "the notch centre tracking ESC RPM (INS_HNTCH_MODE 3)",
            ],
            fly=copter_hover(6, altitude=4),  # full-rate raw IMU fills a log fast
            params={"LOG_BITMASK": LOG_IMU_RAW},
            boot_params=VIBRATING
            | {
                "INS_HNTCH_ENABLE": 1,
                "INS_HNTCH_MODE": 3,
                "INS_HNTCH_FREQ": 80,
                "INS_HNTCH_REF": 1,
                "INS_HNTCH_HMNCS": 5,
                "INS_HNTCH_ATT": 50,
                "INS_HNTCH_BW": 40,
            },
            speedup=RAW_SPEEDUP,
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-heavylift-light",
            vehicle="copter",
            frame="octo coaxial X8",
            description="Heavy-lift X8 without payload: GUIDED take-off to 5 m, 15 s hover, LAND.",
            expected=HEAVY_LIFT_EXPECTED,
            fly=copter_hover(15, altitude=5, raw_in_hover=True),
            boot_params=VIBRATING | THROTTLE_NOTCH | X8_VIBRATION,
            faults=HEAVY_LIFT_FAULTS,
            **CALLISTO,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-heavylift-loaded",
            vehicle="copter",
            frame="octo coaxial X8",
            description=(
                "The same heavy-lift X8 carrying 8 kg more (40.5 kg in all):"
                " GUIDED take-off to 5 m, 15 s hover, LAND."
            ),
            expected=[*HEAVY_LIFT_EXPECTED, "hover throttle higher than the light flight's"],
            fly=copter_hover(15, altitude=5, raw_in_hover=True),
            boot_params=VIBRATING | THROTTLE_NOTCH | X8_VIBRATION,
            payload_kg=8.0,
            faults=HEAVY_LIFT_FAULTS,
            **CALLISTO,  # type: ignore[arg-type]
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
