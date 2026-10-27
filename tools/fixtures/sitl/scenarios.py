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
# Copter's bit 21 logs the notch's inputs and centres (FTN, FCN) at every update instead
# of 10 Hz, so the filters can be replayed exactly (ArduCopter/defines.h MASK_LOG_FTN_FAST).
LOG_IMU_RAW_FAST_NOTCH = SetBits(
    1 << 19 | 1 << 21, "default with bits 19 (raw IMU) and 21 (fast harmonic notch logging) set"
)
# Copter's bits 0 and 12 log RATE and the rate PIDs every loop (ArduCopter/Copter.cpp
# loop_rate_logging), for a tune check.
LOG_RATE_LOOP = SetBits(1 << 0 | 1 << 12, "default with bits 0 (fast attitude) and 12 (PID) set")
# Bit 0 alone logs RATE every loop without the four PID messages, which a measurement of
# the motors' response does not need.
LOG_RATE_ONLY = SetBits(1 << 0, "default with bit 0 (fast attitude) set")


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
    seconds: float,
    altitude: float = 10,
    raw_in_hover: bool = False,
    hover_bits: SetBits = LOG_IMU_RAW,
) -> Callable[[Vehicle], None]:
    """Take off, hover, land. With `raw_in_hover`, raw IMU logging (`hover_bits`) runs
    during the hover only, as for a filter test flight: a slow heavy-lift climb and
    descent would otherwise fill the log."""

    def fly(v: Vehicle) -> None:
        v.mode("GUIDED")
        v.arm()
        v.command(ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, altitude)
        v.wait_altitude(altitude - 0.5)
        if raw_in_hover:
            bitmask = v.get_param("LOG_BITMASK")
            v.set_param("LOG_BITMASK", hover_bits(bitmask))
        v.hold(seconds)
        if raw_in_hover:
            v.set_param("LOG_BITMASK", bitmask)
        land_and_disarm(v)

    return fly


def sticks(v: Vehicle, seconds: float, channels: dict[str, int]) -> None:
    """Hold sticks for `seconds` of simulated time. The override is sent again every
    0.2 s, well inside RC_OVERRIDE_TIME, so it never lapses to the simulated radio."""
    from run_scenario import ScenarioFailed

    start = last = None
    while True:
        m = v.conn.recv_match(type="ATTITUDE", blocking=True, timeout=10)
        if m is None:
            raise ScenarioFailed("no ATTITUDE messages")
        now = m.time_boot_ms / 1000
        if start is None:
            start = now
        if last is None or now - last >= 0.2:
            v.rc(**channels)
            last = now
        if now - start >= seconds:
            return


def tune_check(
    doublets: int,
    axes: tuple[str, ...] = ("ch1", "ch2", "ch4"),
    deflection: int = 160,
    each_way: float = 0.4,
    centred: float = 1.0,
    altitude: float = 5,
    hands_off: float = 3,
    bits: SetBits = LOG_RATE_LOOP,
) -> Callable[[Vehicle], None]:
    """The tune-check profile, shortened so the log stays small: take off, hold AltHold
    hands-off for `hands_off` s (10 s or more gives the hover thrust a payload band needs),
    then doublets on each axis's stick (`deflection` µs from centre, 160 is 40% of the
    throw), with the rate loop logged every loop (`bits`) during them only; then land."""

    def fly(v: Vehicle) -> None:
        v.mode("GUIDED")
        v.arm()
        v.command(ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, altitude)
        v.wait_altitude(altitude - 0.5)
        centre = {"ch1": 1500, "ch2": 1500, "ch3": 1500, "ch4": 1500}
        v.rc(**centre)
        v.mode("ALT_HOLD")
        sticks(v, hands_off, centre)
        bitmask = v.get_param("LOG_BITMASK")
        v.set_param("LOG_BITMASK", bits(bitmask))
        for ch in axes:
            for _ in range(doublets):
                sticks(v, each_way, centre | {ch: 1500 + deflection})
                sticks(v, each_way, centre | {ch: 1500 - deflection})
                sticks(v, centred, centre)
        sticks(v, 2, centre)  # the last input settles with the rate loop still logged
        v.set_param("LOG_BITMASK", bitmask)
        land_and_disarm(v)

    return fly


def autotune(
    finish: bool, altitude: float = 10, before_stop: float = 25
) -> Callable[[Vehicle], None]:
    """Roll AutoTune from AltHold (AUTOTUNE_AXES 1), sticks centred throughout. With
    `finish`, the tune runs to its end ("AutoTune: Success") and the aircraft lands and
    disarms still in AutoTune, so the firmware saves the tuned gains (AC_AutoTune::disarmed).
    Otherwise the pilot switches back to AltHold after `before_stop` s, which stops the
    tune and restores the original gains (AC_AutoTune::stop), then lands."""

    def fly(v: Vehicle) -> None:
        from run_scenario import ScenarioFailed

        v.mode("GUIDED")
        v.arm()
        v.command(ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, altitude)
        v.wait_altitude(altitude - 0.5)
        centre = {"ch1": 1500, "ch2": 1500, "ch3": 1500, "ch4": 1500}
        v.rc(**centre)
        v.mode("ALT_HOLD")
        sticks(v, 3, centre)
        v.texts = []  # an earlier tune's "AutoTune: Success" is not this one's
        v.mode("AUTOTUNE")
        if not finish:
            sticks(v, before_stop, centre)
            v.mode("ALT_HOLD")
            sticks(v, 3, centre)
            land_and_disarm(v)
            return
        for _ in range(int(900 / 5)):  # up to 15 minutes of simulated time
            sticks(v, 5, centre)
            if any("AutoTune: Success" in t for t in v.texts):
                break
        else:
            raise ScenarioFailed("AutoTune did not finish")
        sticks(v, 2, centre)
        # Land in AutoTune with the throttle low (it climbs and descends like AltHold),
        # then disarm on the ground: the tune is complete, so the gains are saved.
        low = centre | {"ch3": 1100}
        while True:
            sticks(v, 1, low)
            m = v.conn.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=10)
            if m is not None and m.relative_alt < 300:
                break
        sticks(v, 4, low)
        v.disarm()

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


def failsafe_takeoff(v: Vehicle, altitude: float = 5) -> None:
    v.mode("GUIDED")
    v.arm()
    v.command(ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, altitude)
    v.wait_altitude(altitude - 0.5)
    v.hold(3)


def gcs_loss(v: Vehicle) -> None:
    """Heartbeats from take-off, then none: the GCS failsafe's RTL lands the aircraft."""
    v.heartbeats(True)
    failsafe_takeoff(v)
    v.heartbeats(False)
    v.wait_disarmed(240)


def battery_stages(v: Vehicle) -> None:
    """The pack to 10.5 V (the low stage's RTL), then 8.5 V (the critical stage's LAND)."""
    failsafe_takeoff(v, 10)
    v.set_param("SIM_BATT_VOLTAGE", 10.5)
    v.hold(5)
    v.set_param("SIM_BATT_VOLTAGE", 8.5)
    v.wait_disarmed(240)


def radio_loss(v: Vehicle) -> None:
    """The receiver's failsafe output (every channel neutral, throttle 950 µs): the radio
    failsafe's RTL lands the aircraft."""
    failsafe_takeoff(v)
    v.set_param("SIM_RC_FAIL", 2)
    v.wait_disarmed(240)


def failsafes_off(v: Vehicle) -> None:
    """The ground station lost, then the receiver, with both failsafes off: nothing
    reacts, and the flight ends in LAND by command."""
    v.heartbeats(True)
    failsafe_takeoff(v)
    v.heartbeats(False)
    v.hold(8)
    v.set_param("SIM_RC_FAIL", 2)
    v.hold(3)
    land_and_disarm(v)


DEG_PER_M = 1 / 111_320.0  # SIM_GPS1_GLTCH_X is degrees of latitude


def gnss_takeoff(v: Vehicle) -> None:
    """GUIDED take-off to 15 m, then LOITER with the sticks centred, which holds position
    and height on GNSS: the mode in which a GNSS fault moves the aircraft."""
    v.mode("GUIDED")
    v.arm()
    v.command(ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 15)
    v.wait_altitude(14.5)
    v.rc(ch1=1500, ch2=1500, ch3=1500, ch4=1500)
    v.mode("LOITER")
    v.hold(10)


def gnss_jam(v: Vehicle) -> None:
    """The simulated receiver jammed for 25 s, then clear for 20 s."""
    gnss_takeoff(v)
    v.set_param("SIM_GPS1_JAM", 1)
    v.hold(25)
    v.set_param("SIM_GPS1_JAM", 0)
    v.hold(20)
    land_and_disarm(v)


def gnss_step(v: Vehicle) -> None:
    """GNSS moved 60 m north at once for 20 s, with the signal healthy, then back."""
    gnss_takeoff(v)
    v.set_param("SIM_GPS1_GLTCH_X", 60 * DEG_PER_M)
    v.hold(20)
    v.set_param("SIM_GPS1_GLTCH_X", 0)
    v.hold(15)
    land_and_disarm(v)


def gnss_ramp(v: Vehicle) -> None:
    """GNSS dragged 80 m north over 60 s, held 10 s, then let go: the slow spoof the
    estimator follows."""
    gnss_takeoff(v)
    for k in range(1, 61):
        v.set_param("SIM_GPS1_GLTCH_X", 80 * DEG_PER_M * k / 60)
        v.hold(1)
    v.hold(10)
    v.set_param("SIM_GPS1_GLTCH_X", 0)
    v.hold(10)
    land_and_disarm(v)


def nognss_takeoff(v: Vehicle, altitude: float) -> None:
    """GUIDED take-off, then LOITER with the sticks centred. SITL's channel 8 rests
    mid-stick, which selects EKF source set 2, so the GPS-disable (7) and source-set (8)
    switches go low before arming."""
    v.rc(ch7=1000, ch8=1000)
    v.hold(5)
    v.mode("GUIDED")
    v.arm()
    v.command(ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, altitude)
    v.wait_altitude(altitude - 0.5)
    v.rc(ch1=1500, ch2=1500, ch3=1500, ch4=1500)
    v.mode("LOITER")
    v.hold(10)


def nognss_drill(v: Vehicle) -> None:
    """GNSS switched off with the GPS-disable switch for 20 s, then on again."""
    nognss_takeoff(v, 15)
    v.rc(ch7=2000)
    v.hold(20)
    v.rc(ch7=1000)
    v.hold(15)
    land_and_disarm(v)


def nognss_flow(v: Vehicle) -> None:
    """Source set 2 (optical flow) on its switch, GNSS off for 30 s, GNSS back, set 1."""
    nognss_takeoff(v, 10)
    v.rc(ch8=1500)
    v.hold(5)
    v.rc(ch7=2000)
    v.hold(30)
    v.rc(ch7=1000)
    v.hold(5)
    v.rc(ch8=1000)
    v.hold(10)
    land_and_disarm(v)


# GUIDED velocity and yaw targets (SET_POSITION_TARGET_LOCAL_NED): use vx, vy, vz and yaw
VELOCITY_YAW_MASK = 0b0000_1001_1100_0111


def guided_velocity(v: Vehicle, north: float, east: float, yaw_deg: float, seconds: float) -> None:
    """Hold a velocity and heading in GUIDED for `seconds` of simulated time, resent every
    0.5 s (GUIDED drops a velocity target after 3 s)."""
    for _ in range(round(seconds / 0.5)):
        v.conn.mav.set_position_target_local_ned_send(
            0, v.sys, v.comp, ML.MAV_FRAME_LOCAL_NED, VELOCITY_YAW_MASK,
            0, 0, 0, north, east, 0, 0, 0, 0, math.radians(yaw_deg), 0,
        )  # fmt: skip
        v.hold(0.5)


def wind_legs(v: Vehicle) -> None:
    """Take off to 20 m, fly 6 s legs at 3, 6 and 9 m/s on eight courses, the nose turned
    0, 30 or 60 degrees off the course, then hover facing eight ways, 5 s each, and land."""
    v.mode("GUIDED")
    v.arm()
    v.command(ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 20)
    v.wait_altitude(19.5)
    for speed in (3.0, 6.0, 9.0):
        for k in range(8):
            course = math.radians(45.0 * k)
            yaw = (45.0 * k + 30.0 * (k % 3)) % 360.0
            guided_velocity(v, speed * math.cos(course), speed * math.sin(course), yaw, 6)
    for k in range(8):
        guided_velocity(v, 0, 0, 45.0 * k, 5)
    land_and_disarm(v)


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
# A roll AutoTune runs for minutes, and SITL logs its own state (SIM2) at loop rate,
# streamed: capping streamed messages at 25 Hz keeps the log small. AutoTune's own
# records (EV, ATUN, MODE, MSG) are not streamed, so all of them are kept.
AUTOTUNE_LOGGING = {"LOG_DISARMED": 0, "SIM_ESC_TELEM": 0, "LOG_FILE_RATEMAX": 25}
RAW_SPEEDUP = 2  # full-rate raw IMU logging drops data at the default speed-up

# The default quad's roll and pitch rate gains (P, I and D: 0.135, 0.135 and 0.0036 in
# 4.7.0) at 0.4 of their defaults: a soft rate loop, slow enough (bandwidth near 2.5 Hz)
# for the tune check's doublets to measure its frequency and step responses. The default
# quad's own loop reaches past 20 Hz, above anything a stick doublet excites.
SOFT_RATE_GAINS = {
    f"ATC_RAT_{axis}_{term}": round(default * 0.4, 6)
    for axis in ("RLL", "PIT")
    for term, default in (("P", 0.135), ("I", 0.135), ("D", 0.0036))
}

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
# For timing the motors: ESC RPM, which SITL reports only with SIM_VIB_MOT_MAX set
# (SIM_Frame.cpp: the slewed command times SIM_VIB_MOT_MAX times 60), with the light
# flight's vibration; and SITL's LOG_BITMASK (180222) without bit 12, so no PID messages.
HEAVY_LIFT_TIMING = {
    "LOG_DISARMED": 0,
    "SIM_ESC_TELEM": 1,
    "LOG_BITMASK": 176126,
    "SIM_VIB_MOT_MAX": 350,
    **X8_VIBRATION,
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
# Failsafe flights log at no more than 10 Hz (LOG_FILE_RATEMAX): what the failsafe
# analyzers read is logged at 10 Hz or slower (BAT, RCIN, RCI2, CTUN) or as events.
FAILSAFE_LOGGING = {"LOG_DISARMED": 0, "SIM_ESC_TELEM": 0, "LOG_FILE_RATEMAX": 10}

# GPS-disable on switch 7 and the EKF source set on switch 8 (aux functions 65 and 90),
# as a drill would be set up; they take effect at boot
NO_GNSS_SWITCHES = FAILSAFE_LOGGING | {"RC7_OPTION": 65, "RC8_OPTION": 90}
# Optical flow and a rangefinder as ArduPilot's autotest simulates them
# (Tools/autotest/default_params/copter-optflow.parm), as source set 2
OPTICAL_FLOW = {
    "SIM_FLOW_ENABLE": 1, "FLOW_TYPE": 10, "RNGFND1_TYPE": 1, "RNGFND1_MIN": 0,
    "RNGFND1_MAX": 40.0, "RNGFND1_PIN": 0, "RNGFND1_SCALING": 12.12,
    "EK3_SRC2_POSXY": 0, "EK3_SRC2_VELXY": 5, "EK3_SRC2_POSZ": 1, "EK3_SRC2_VELZ": 0,
    "EK3_SRC2_YAW": 1,
}  # fmt: skip
WIND = {"SIM_WIND_SPD": 6, "SIM_WIND_DIR": 90, "SIM_WIND_TURB": 1}
# A wind of 5 m/s from the west at every height (SIM_WIND_T 1: no profile), gusting
STEADY_WIND = {"SIM_WIND_SPD": 5, "SIM_WIND_DIR": 270, "SIM_WIND_T": 1, "SIM_WIND_TURB": 1}
WIND_EXPECTED = [
    "GUIDED take-off to 20 m; 6 s legs at 3, 6 and 9 m/s on eight courses, the nose 0, 30"
    " or 60 degrees off the course; then a hover facing eight ways, 5 s each; LAND",
    "streamed messages capped at 10 Hz (LOG_FILE_RATEMAX): XKF1 at 10 Hz a core",
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
            name="copter-quad-fastnotch",
            vehicle="copter",
            frame="quad +",
            description=(
                "Hover in gusty wind with per-motor ESC RPM notches updated at loop rate,"
                " raw IMU and fast harmonic notch logging during the hover: the notch's own"
                " inputs are logged at every update, so the filters can be replayed exactly."
            ),
            expected=[
                "INS_RAW_LOG_OPT = 9 and LOG_BITMASK bits 19 and 21 during the hover",
                "about 10 s of hover at 4 m in gusty wind, the motors correcting for it",
                "8 notches: 4 motors x 2 harmonics, each following its own motor",
                "FTN logged at every notch update (400 Hz) during the hover",
                "the filter model matches the logged post-filter gyro when it follows FTN",
            ],
            fly=copter_hover(10, altitude=4, raw_in_hover=True, hover_bits=LOG_IMU_RAW_FAST_NOTCH),
            boot_params=VIBRATING
            | {
                "INS_HNTCH_ENABLE": 1,
                "INS_HNTCH_MODE": 3,
                "INS_HNTCH_REF": 1,
                "INS_HNTCH_FREQ": 120,
                "INS_HNTCH_BW": 30,
                "INS_HNTCH_HMNCS": 3,
                "INS_HNTCH_ATT": 40,
                "INS_HNTCH_OPTS": 6,  # bit 1: a notch per motor; bit 2: update at loop rate
                "SIM_WIND_SPD": 2,
                "SIM_WIND_TURB": 2,
            },
            speedup=RAW_SPEEDUP,
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-tune-default",
            vehicle="copter",
            frame="quad +",
            description=(
                "The tune-check profile on the default quad, shortened to keep the log small:"
                " AltHold at 5 m, 6 doublets on roll and 6 on pitch at 40% stick, with RATE"
                " and the rate PIDs logged every loop during them."
            ),
            expected=[
                "LOG_BITMASK bits 0 and 12 during the doublets: RATE and PIDR/P/Y at 400 Hz",
                "6 doublets on roll and on pitch in ALT_HOLD, 0.4 s each way and 1 s centred",
                "every doublet counted: no saturation, no mode change, well above 2 m",
                "roll and pitch tracking error from 6 inputs each, with intervals; no yaw inputs",
                "about 41 s of steady hover in ALT_HOLD: the short doublets stay inside the"
                " hover limits (under 10 degrees of tilt and 1 m/s)",
            ],
            fly=tune_check(6, axes=("ch1", "ch2")),
            boot_params={"LOG_DISARMED": 0, "SIM_ESC_TELEM": 0},  # no ESC log: a smaller file
            speedup=RAW_SPEEDUP,  # the logger drops the loop-rate messages at the default
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-tune-low",
            vehicle="copter",
            frame="quad +",
            description=(
                "The same shortened tune-check profile as copter-quad-tune-default, with the"
                " roll and pitch rate gains (P, I, D) at 0.4 of their defaults: a soft rate"
                " loop."
            ),
            expected=[
                "LOG_BITMASK bits 0 and 12 during the doublets: RATE, ANG and PIDR/P/Y at 400 Hz",
                "6 doublets on roll and on pitch in ALT_HOLD, 0.4 s each way and 1 s centred",
                "roll and pitch tracking error clearly above the default quad's",
                "roll and pitch rate-loop bandwidth near 2.5 Hz, measured with the step response;"
                " the sensitivity peak lies above what the doublets measure",
                "about 41 s of steady hover in ALT_HOLD: the short doublets stay inside the"
                " hover limits (under 10 degrees of tilt and 1 m/s)",
            ],
            fly=tune_check(6, axes=("ch1", "ch2")),
            boot_params={"LOG_DISARMED": 0, "SIM_ESC_TELEM": 0} | SOFT_RATE_GAINS,
            speedup=RAW_SPEEDUP,
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-autotune-saved",
            vehicle="copter",
            frame="quad +",
            description=(
                "Roll AutoTune on the quad with the soft rate gains of copter-quad-tune-low:"
                " AltHold at 10 m, AUTOTUNE until 'AutoTune: Success', then landed and disarmed"
                " still in AutoTune, so the firmware saves the tuned gains."
            ),
            expected=[
                "EV AUTOTUNE_INITIALISED, then AUTOTUNE_SUCCESS and AUTOTUNE_SAVEDGAINS",
                "ATUN rows for roll only (AUTOTUNE_AXES 1), from rate D up to angle P up",
                "roll rate P about doubled from the soft 0.054 (to about 0.118); rate D lowered"
                " to about 0.0004, under AUTOTUNE_MIN_D after the 25% back-off",
                "no ESC telemetry (SIM_ESC_TELEM 0): the desync check has nothing to read",
                "LOG_FILE_RATEMAX 25 for a small log: streamed messages at 25 Hz",
                "steady hover windows: 5 s in ALT_HOLD; 8 s in AUTOTUNE; 8 s in AUTOTUNE"
                " (between twitches)",
            ],
            fly=autotune(finish=True),
            boot_params=AUTOTUNE_LOGGING | {"AUTOTUNE_AXES": 1} | SOFT_RATE_GAINS,
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-autotune-aborted",
            vehicle="copter",
            frame="quad +",
            description=(
                "Roll AutoTune on the default quad, stopped after 25 s by switching back to"
                " AltHold, then LAND: the firmware restores the original gains."
            ),
            expected=[
                "EV AUTOTUNE_INITIALISED, then AUTOTUNE_OFF; no AUTOTUNE_SUCCESS or SAVEDGAINS",
                "ATUN rows for roll only, the tune stopped part-way",
                "the gains at the end of the log are the gains at its start",
                "LOG_FILE_RATEMAX 25 for a small log: streamed messages at 25 Hz",
                "steady hover windows: 20 s in ALT_HOLD; 7 s in AUTOTUNE (between twitches)",
            ],
            fly=autotune(finish=False),
            boot_params=AUTOTUNE_LOGGING | {"AUTOTUNE_AXES": 1},
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
            name="copter-heavylift-tune",
            vehicle="copter",
            frame="octo coaxial X8",
            description=(
                "The tune-check profile on the heavy-lift X8 without payload, shortened to keep"
                " the log small: AltHold at 5 m, 3 doublets each on roll, pitch and yaw at 40%"
                " stick, with RATE logged every loop and ESC RPM from all eight motors."
            ),
            expected=[
                "LOG_BITMASK bit 0 during the doublets: RATE at 400 Hz (no PID messages)",
                "ESC RPM from all eight motors, about 100 Hz each",
                "each motor's RPM follows its command with no lag beyond the simulator's slew"
                " limit and the log's timing: SITL models no motor or rotor dynamics",
                "about 33 s of steady hover in ALT_HOLD: the short doublets stay inside the"
                " hover limits",
            ],
            fly=tune_check(3, axes=("ch1", "ch2", "ch4"), bits=LOG_RATE_ONLY),
            boot_params=HEAVY_LIFT_TIMING,
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
        Scenario(
            name="copter-quad-fs-gcs",
            vehicle="copter",
            frame="quad +",
            description=(
                "GUIDED take-off to 5 m with the ground station's heartbeats on, then none:"
                " the GCS failsafe (FS_GCS_ENABLE 1) returns and lands."
            ),
            expected=[
                "ERR FAILSAFE_GCS about 5 s after MAV.mgs stops advancing (FS_GCS_TIMEOUT 5)",
                "MODE RTL with reason GCS_FAILSAFE in the same loop: as configured, on time",
                "RTL lands and disarms",
                "steady hover windows: 14 s in GUIDED",
            ],
            fly=gcs_loss,
            params={"FS_GCS_ENABLE": 1},
            boot_params=FAILSAFE_LOGGING,
            faults=[{"kind": "other", "description": "ground-station link lost (heartbeats stop)"}],
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-fs-battery",
            vehicle="copter",
            frame="quad +",
            description=(
                "GUIDED take-off to 10 m, then the simulated pack to 10.5 V and 8.5 V: the"
                " low stage's RTL, then the critical stage's LAND."
            ),
            expected=[
                "ERR FAILSAFE_BATT 2.1 s after BAT.Volt drops under BATT_LOW_VOLT 11"
                " (BATT_LOW_TIMER 2, checked at 10 Hz): MODE RTL, reason BATTERY_FAILSAFE",
                "a second ERR FAILSAFE_BATT 2.1 s after BAT.Volt drops under BATT_CRT_VOLT 9:"
                " MODE LAND",
                "LAND lands and disarms",
                "steady hover windows: 8 s in GUIDED; 6 s in RTL",
            ],
            fly=battery_stages,
            params={
                "BATT_LOW_VOLT": 11,
                "BATT_CRT_VOLT": 9,
                "BATT_LOW_TIMER": 2,
                "BATT_FS_LOW_ACT": 2,
                "BATT_FS_CRT_ACT": 1,
            },
            boot_params=FAILSAFE_LOGGING,
            faults=[
                {
                    "kind": "other",
                    "description": "the pack run down to low, then critical",
                    "parameters": ["SIM_BATT_VOLTAGE"],
                }
            ],
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-fs-radio",
            vehicle="copter",
            frame="quad +",
            description=(
                "GUIDED take-off to 5 m, then the receiver's failsafe output (throttle 950 µs,"
                " under FS_THR_VALUE 975) with FS_THR_ENABLE 4, SmartRTL or RTL, and SmartRTL"
                " unavailable (SRTL_POINTS 0): the failsafe falls back to RTL."
            ),
            expected=[
                "RCIN.C3 under FS_THR_VALUE, then ERR FAILSAFE_RADIO within three frames",
                "MODE RTL with reason RADIO_FAILSAFE: a fallback, SmartRTL could not start",
                "RTL lands and disarms",
                "steady hover windows: 10 s in GUIDED",
            ],
            fly=radio_loss,
            params={"FS_THR_ENABLE": 4},
            boot_params=FAILSAFE_LOGGING | {"SRTL_POINTS": 0},
            faults=[
                {
                    "kind": "other",
                    "description": "RC link lost (receiver failsafe throttle)",
                    "parameters": ["SIM_RC_FAIL"],
                },
                {
                    "kind": "parameter-error",
                    "description": "SmartRTL unavailable",
                    "parameters": ["SRTL_POINTS"],
                },
            ],
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-fs-off",
            vehicle="copter",
            frame="quad +",
            description=(
                "GUIDED take-off to 5 m with the ground station's heartbeats on, then none,"
                " then the receiver's failsafe output, with both failsafes off"
                " (FS_GCS_ENABLE 0, FS_THR_ENABLE 0): nothing reacts; LAND by command."
            ),
            expected=[
                "MAV.mgs stops advancing and the throttle drops under FS_THR_VALUE while armed",
                "no ERR FAILSAFE_GCS or FAILSAFE_RADIO",
                "LAND by command (reason GCS_COMMAND), then disarm",
                "steady hover windows: 14 s in GUIDED",
            ],
            fly=failsafes_off,
            params={"FS_GCS_ENABLE": 0, "FS_THR_ENABLE": 0},
            boot_params=FAILSAFE_LOGGING,
            faults=[
                {"kind": "other", "description": "ground-station link lost (heartbeats stop)"},
                {
                    "kind": "other",
                    "description": "RC link lost (receiver failsafe throttle)",
                    "parameters": ["SIM_RC_FAIL"],
                },
                {
                    "kind": "parameter-error",
                    "description": "GCS and radio failsafes off",
                    "parameters": ["FS_GCS_ENABLE", "FS_THR_ENABLE"],
                },
            ],
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-gnss-jam",
            vehicle="copter",
            frame="quad +",
            description=(
                "GUIDED take-off to 15 m, LOITER, then the simulated receiver jammed for 25 s"
                " (SIM_GPS1_JAM 1): the fix flickers, satellites fall to 3 and the position"
                " wanders by hundreds of metres; the EKF failsafe lands the aircraft."
            ),
            expected=[
                "GPS.Status under 3 and GPS.NSats 3 in bursts, GPA.HAcc up to hundreds of m",
                "XKF4 SP and SV past 100 (a test ratio past 1) while the estimator rejects GNSS",
                "ERR GPS 2 (glitch), ERR FAILSAFE_EKFINAV, MODE LAND by EKF_FAILSAFE",
                "the GNSS analyzer: one event, jamming (no u-blox status in SITL)",
                "steady hover windows: 21 s in LOITER",
            ],
            fly=gnss_jam,
            params={"RC_OVERRIDE_TIME": -1},
            boot_params=FAILSAFE_LOGGING,
            faults=[
                {"kind": "gnss", "description": "receiver jammed", "parameters": ["SIM_GPS1_JAM"]}
            ],
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-gnss-step",
            vehicle="copter",
            frame="quad +",
            description=(
                "GUIDED take-off to 15 m, LOITER, then GNSS moved 60 m north at once for 20 s"
                " with the signal healthy (SIM_GPS1_GLTCH_X), then back."
            ),
            expected=[
                "XKF4 SP near 1600 (a test ratio of 16): the estimator rejects the jump",
                "ERR GPS 2 (glitch); after about 10 s the estimator resets onto GNSS",
                "the same again when GNSS jumps back",
                "the GNSS analyzer: spoofing, the estimator rejecting GNSS",
                "steady hover windows: 48 s in LOITER",
            ],
            fly=gnss_step,
            params={"RC_OVERRIDE_TIME": -1},
            boot_params=FAILSAFE_LOGGING,
            faults=[
                {
                    "kind": "gnss",
                    "description": "a 60 m GNSS position step",
                    "parameters": ["SIM_GPS1_GLTCH_X"],
                }
            ],
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-gnss-ramp",
            vehicle="copter",
            frame="quad +",
            description=(
                "GUIDED take-off to 15 m, LOITER, then GNSS dragged 80 m north over 60 s with"
                " the signal healthy (SIM_GPS1_GLTCH_X), held for 10 s, then let go."
            ),
            expected=[
                "XKF4 SP 30 to 60 (test ratios 0.3 to 0.6): the estimator follows the drift",
                "XKF3 IVN one-sided near 0.4 m/s while it drifts",
                "when GNSS is let go, an 80 m jump the estimator rejects, then a reset",
                "the GNSS analyzer: spoofing the estimator followed, then a position jump",
                "steady hover windows: 108 s in LOITER",
            ],
            fly=gnss_ramp,
            params={"RC_OVERRIDE_TIME": -1},
            boot_params=FAILSAFE_LOGGING,
            faults=[
                {
                    "kind": "gnss",
                    "description": "GNSS dragged 80 m over 60 s",
                    "parameters": ["SIM_GPS1_GLTCH_X"],
                }
            ],
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-nognss-drill",
            vehicle="copter",
            frame="quad +",
            description=(
                "GUIDED take-off to 15 m, LOITER in a 6 m/s wind, then GNSS switched off with"
                " the GPS-disable switch (aux 65) for 20 s, with no other position source."
            ),
            expected=[
                "AUXF 65 high, GPS.Status 1 while it is",
                "XKF4.SS: using GPS clears about 4 s in, the absolute position about 7 s in",
                "ERR FAILSAFE_EKFINAV and MODE LAND by EKF_FAILSAFE; the aircraft drifts downwind",
                "the no-GNSS analyzer: a drill, the position given up; the GNSS analyzer: nothing",
                "steady hover windows: 22 s in LOITER",
            ],
            fly=nognss_drill,
            params={"RC_OVERRIDE_TIME": -1} | WIND,
            boot_params=NO_GNSS_SWITCHES,
            faults=[{"kind": "gnss", "description": "GNSS switched off for a drill"}],
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-nognss-flow",
            vehicle="copter",
            frame="quad +",
            description=(
                "GUIDED take-off to 10 m, LOITER in a 6 m/s wind with optical flow and a"
                " rangefinder as EKF source set 2: set 2 on its switch (aux 90), GNSS off"
                " (aux 65) for 30 s, GNSS back, then set 1."
            ),
            expected=[
                "Using EKF Source Set 2; EKF3 fusing optical flow",
                "XKF4.SS keeps the relative position all through the drill; no failsafe",
                "within about a metre of GNSS when it returns",
                "the no-GNSS analyzer: a drill, the position kept",
                "steady hover windows: 63 s in LOITER",
            ],
            fly=nognss_flow,
            params={"RC_OVERRIDE_TIME": -1} | WIND,
            boot_params=NO_GNSS_SWITCHES | OPTICAL_FLOW,
            faults=[{"kind": "gnss", "description": "GNSS switched off for a drill"}],
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-wind",
            vehicle="copter",
            frame="quad +",
            description=(
                "Legs at 3, 6 and 9 m/s on eight courses and a turning hover, at 20 m in a"
                " gusting 5 m/s wind from the west at every height (SIM_WIND_T 1)."
            ),
            expected=[
                *WIND_EXPECTED,
                "the wind analyzer: about 5 m/s from 270 degrees, the drag fitted from the legs",
                "steady hover windows: 7 s in GUIDED; 7 s in GUIDED; 7 s in GUIDED; 7 s in GUIDED",
            ],
            fly=wind_legs,
            params=STEADY_WIND,
            boot_params=FAILSAFE_LOGGING,
            **COPTER,  # type: ignore[arg-type]
        ),
        Scenario(
            name="copter-quad-wind-calm",
            vehicle="copter",
            frame="quad +",
            description="The same legs and turning hover in still air (SIM_WIND_SPD 0).",
            expected=[
                *WIND_EXPECTED,
                "the wind analyzer: under 0.5 m/s, with the drag of the windy flight",
                "steady hover windows: 57 s in GUIDED",
            ],
            fly=wind_legs,
            params={"SIM_WIND_SPD": 0},
            boot_params=FAILSAFE_LOGGING,
            **COPTER,  # type: ignore[arg-type]
        ),
    ]
}
