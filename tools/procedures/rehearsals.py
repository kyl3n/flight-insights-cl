"""The procedures rehearsed in SITL (spec: Testing and validation, "Procedure rehearsal").

Each rehearsal is a weaker setup the app should improve: the simulator boots with it,
flies a flight that shows the problem, and the app's plan for that flight is then
carried out exactly as its procedure says. The test flight is the one the procedure
asks for; the rest of the rehearsal (the files, refreshes, reboots, checks, revert and
undo) comes from the plan itself, never from here.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pymavlink import mavutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures" / "sitl"))

from run_scenario import ScenarioFailed
from scenarios import (
    CALLISTO,
    COPTER,
    LOG_IMU_RAW,
    RAW_SPEEDUP,
    SCENARIOS,
    VIBRATING,
    Scenario,
    copter_hover,
    land_and_disarm,
)

if TYPE_CHECKING:
    from run_scenario import Vehicle

ML = mavutil.mavlink
STICK = 125  # µs from centre: a quarter of full deflection, "moderate stick inputs"


def sticks(v: Vehicle, seconds: float, channels: Mapping[str, int]) -> None:
    """Hold sticks for `seconds` of simulated time. The override is sent again every
    0.2 s, well inside RC_OVERRIDE_TIME, so it never lapses to the simulated radio."""
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


def hover_then_sticks(
    hover: float, stick_seconds: float, altitude: float
) -> Callable[[Vehicle], None]:
    """The filter test flight: take off, hover, then moderate stick inputs on every
    axis in LOITER (each way in turn on roll, pitch and yaw, then centred), then land."""

    def fly(v: Vehicle) -> None:
        v.mode("GUIDED")
        v.arm()
        v.command(ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, altitude)
        v.wait_altitude(altitude - 0.5)
        v.hold(hover)
        centre = {"ch1": 1500, "ch2": 1500, "ch3": 1500, "ch4": 1500}
        v.rc(**centre)
        v.mode("LOITER")
        moves = [(ch, 1500 + d) for ch in ("ch1", "ch2", "ch4") for d in (STICK, -STICK)]
        each = stick_seconds / (len(moves) + 1)
        for ch, pwm in moves:
            sticks(v, each, centre | {ch: pwm})
        sticks(v, each, centre)
        land_and_disarm(v)

    return fly


@dataclass(frozen=True)
class Rehearsal:
    name: str
    description: str
    setup: Scenario  # the weaker setup, and the flight that shows it
    answers: Mapping[str, Any]  # the platform setup's answers
    expects: tuple[str, ...]  # findings the weaker setup must raise, with changes on offer
    test_flight: Callable[[Vehicle], None]  # what the procedure says to fly
    # Plans carried out, one test flight each, as a person works through the stages:
    # a problem in an earlier stage (logging dropouts, say) is fixed first.
    rounds: int = 3


# A quad with motor vibration and ESC telemetry, whose harmonic notch is off: the
# user's own example. The plan switches the notch on, refreshes, loads its settings,
# reboots, and turns on raw IMU and fast harmonic notch logging for the test flight.
QUAD_NO_NOTCH = Scenario(
    name="rehearse-quad-no-notch",
    vehicle="copter",
    frame="quad +",
    description="Quad with motor vibration and ESC RPM, harmonic notch off.",
    expected=["a motor peak in the pre-filter gyro, with no notch on it"],
    fly=copter_hover(10, altitude=4, raw_in_hover=True, hover_bits=LOG_IMU_RAW),
    boot_params=VIBRATING | {"INS_HNTCH_ENABLE": 0},
    speedup=RAW_SPEEDUP,
    **COPTER,  # type: ignore[arg-type]
)

# Failsafes the firmware ships on, found off, on a quad that otherwise flies normally.
# They are set after boot, not as boot defaults, so the log still records the firmware's
# own defaults for the plan to go back to.
QUAD_SETTINGS = Scenario(
    name="rehearse-quad-settings",
    vehicle="copter",
    frame="quad +",
    description="Quad with the vibration and dead-reckoning failsafes off.",
    expected=["the failsafes the firmware ships on, found off"],
    fly=copter_hover(10, altitude=4),
    params={"FS_VIBE_ENABLE": 0, "FS_DR_ENABLE": 0},
    **COPTER,  # type: ignore[arg-type]
)

REHEARSALS: dict[str, Rehearsal] = {
    r.name: r
    for r in [
        Rehearsal(
            name="quad-notch-on",
            description=(
                "Switch the harmonic notch on, following ESC RPM, with raw IMU and fast"
                " harmonic notch logging for the test flight."
            ),
            setup=QUAD_NO_NOTCH,
            answers={"payload": "fixed", "esc_firmware": "BLHeli_32", "battery_chemistry": "LiPo"},
            expects=("filter.no_notch",),
            test_flight=hover_then_sticks(60, 30, altitude=4),
        ),
        Rehearsal(
            name="heavylift-rpm-notch",
            description=(
                "A heavy-lift X8 whose notch follows throttle though ESC RPM arrives,"
                " flown loaded, with hover learning off on a variable payload."
            ),
            setup=replace(SCENARIOS["copter-heavylift-loaded"], name="rehearse-heavylift"),
            answers={"payload": "variable", "esc_firmware": "BLHeli_32"},
            expects=("filter.better_source", "option.hover_learn.available"),
            test_flight=hover_then_sticks(60, 30, altitude=5),
        ),
        Rehearsal(
            name="quad-failsafes",
            description="Failsafes the firmware ships on, found off, go back to their defaults.",
            setup=QUAD_SETTINGS,
            answers={"payload": "fixed", "esc_firmware": "BLHeli_32", "battery_chemistry": "LiPo"},
            expects=("params.vibe_failsafe_off", "params.dead_reckoning_failsafe_off"),
            test_flight=copter_hover(30, altitude=10),
        ),
    ]
}

__all__ = ["CALLISTO", "REHEARSALS", "Rehearsal"]
