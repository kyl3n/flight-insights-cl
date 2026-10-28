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

from scenarios import (
    CALLISTO,
    COPTER,
    LOG_IMU_RAW,
    RAW_SPEEDUP,
    SCENARIOS,
    VIBRATING,
    Scenario,
    SetBits,
    autotune,
    buildup_card,
    copter_hover,
    land_and_disarm,
    speed_sweep,
    sticks,
    tune_check,
)

if TYPE_CHECKING:
    from run_scenario import Vehicle

ML = mavutil.mavlink
STICK = 125  # µs from centre: a quarter of full deflection, "moderate stick inputs"


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
class Work:
    """A flight an action's own work takes (AutoTune, one axis a pack), on a fresh pack,
    with the settings its procedure's text has a person make for that flight only."""

    name: str
    params: Mapping[str, float]
    fly: Callable[[Vehicle], None]
    speedup: int | None = None


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
    # The airframe's earlier flights, before the weaker setup's.
    before: tuple[Callable[[Vehicle], None], ...] = ()
    # An action whose work is flights of its own (AutoTune): the weaker setup and the
    # test flight flown light and carrying `loaded_kg` (two payload bands), and the work
    # between them. Its grade is the rehearsal's.
    loaded_kg: float = 0.0
    work: tuple[Work, ...] = ()
    # The take-off weight entered at each import, where the plan needs it (the speed
    # build-up is for one weight).
    takeoff_kg: float | None = None
    # How many plans must offer each expected finding, each a step further than the
    # last (a build-up's steps, each graded by the next flight before the next).
    times: int = 1


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
# Drag fusion on, with the values ArduPilot's own SITL tests use for this quad
# (Tools/autotest/default_params/copter-wind-estimates.parm at Copter-4.7.0): the
# dead-reckoning failsafe can only act once the estimator knows the wind.
WIND_ESTIMATES = {"EK3_DRAG_BCOEF_X": 9.5, "EK3_DRAG_BCOEF_Y": 9.5, "EK3_DRAG_MCOEF": 0.082}
QUAD_SETTINGS = Scenario(
    name="rehearse-quad-settings",
    vehicle="copter",
    frame="quad +",
    description="Quad with drag fusion on and the vibration and dead-reckoning failsafes off.",
    expected=["the failsafes the firmware ships on, found off"],
    fly=copter_hover(10, altitude=4),
    params={"FS_VIBE_ENABLE": 0, "FS_DR_ENABLE": 0, **WIND_ESTIMATES},
    speedup=RAW_SPEEDUP,  # its plan logs raw IMU for the test flight
    **COPTER,  # type: ignore[arg-type]
)

# Callisto with its roll and pitch rate gains at 0.4 of the model's: a rate loop AutoTune
# can improve. Otherwise set up as a person following the app would have it: hover
# learning on (a payload that varies), and an earlier flight logging raw IMU, so the
# filter checks have what they need and AutoTune is the stage on offer.
SOFT_CALLISTO = {
    f"ATC_RAT_{axis}_{term}": round(value * 0.4, 6)
    for axis in ("RLL", "PIT")
    for term, value in (("P", 0.455), ("I", 0.455), ("D", 0.00375))
}
HEAVYLIFT_SOFT = Scenario(
    name="rehearse-heavylift-soft",
    vehicle="copter",
    frame="octo coaxial X8",
    description="Heavy-lift X8 with soft roll and pitch rate gains, flown through the tune check.",
    expected=["roll and pitch tracking measured; AutoTune offered"],
    fly=tune_check(6, axes=("ch1", "ch2"), altitude=8, hands_off=15),
    params=SOFT_CALLISTO | {"MOT_HOVER_LEARN": 2},
    boot_params={"LOG_DISARMED": 0, "SIM_ESC_TELEM": 0},
    **CALLISTO,  # type: ignore[arg-type]
)
AUTOTUNE_LOG = {"LOG_FILE_RATEMAX": 25}  # SITL's own SIM2 at loop rate would fill the log

# Callisto set up as the app's earlier stages leave it, so the speed build-up (navigation)
# is the stage on offer: the rate loop logged for the tune check (with navigation tuning
# off, as a log trimmed for size may have it, so the step's test-flight logging has a bit
# to switch on and its revert file one to take out again), one log a flight, the
# roll and pitch rate P inside their documented range (0 to 0.35 at Copter-4.7.0; the
# model's own 0.455 is above it), hover learning on at the learned hover throttle, the
# battery failsafe set for its 12 cells (thresholds a cell: low 3.6 V, critical 3.5 V,
# arming 3.69 V; Low returns home, Critical lands; the low charge kept for the way home
# and 20% reserve), and a cruise speed under the 12 m/s its speed card verifies (the
# model's own WP_SPD is 15). An earlier flight logs raw IMU, so the filter checks have
# what they need.
READY_CALLISTO = {
    "LOG_BITMASK": SetBits(
        1 << 0, "default, bit 0 (fast attitude) set, bit 5 (NTUN) cleared", 1 << 5
    ),
    "LOG_FILE_DSRMROT": 1,
    "WP_SPD": 10,
    "ATC_RAT_RLL_P": 0.35,
    "ATC_RAT_PIT_P": 0.35,
    "MOT_HOVER_LEARN": 2,
    "MOT_THST_HOVER": 0.376,
    "BATT_LOW_VOLT": 43.2,
    "BATT_CRT_VOLT": 42.0,
    "BATT_ARM_VOLT": 44.3,
    "BATT_FS_LOW_ACT": 2,
    "BATT_FS_CRT_ACT": 1,
    "BATT_LOW_MAH": 13200,
    "SIM_WIND_SPD": 0,
}
HEAVYLIFT_READY = Scenario(
    name="rehearse-heavylift-ready",
    vehicle="copter",
    frame="octo coaxial X8",
    description=(
        "Heavy-lift X8 set up as the earlier stages leave it, flown through the speed card"
        " (legs east and back west at 3, 6, 9 and 12 m/s) in still air."
    ),
    expected=["12 m/s verified at 32.5 kg; the build-up's first step offered"],
    fly=speed_sweep((3.0, 6.0, 9.0, 12.0)),
    params=READY_CALLISTO,  # type: ignore[arg-type]
    boot_params={"LOG_DISARMED": 0, "SIM_ESC_TELEM": 0},
    **CALLISTO,  # type: ignore[arg-type]
)
# Its setup answers: the airframe (Callisto's wheelbase and props), its 12-cell 44 Ah pack
# (Callisto.json battCapacityAh), its weights, and the speed goal "fastest safe". The ESC
# firmware is left unanswered, so DShot stays a question rather than a setup-stage offer.
READY_ANSWERS = {
    "wheelbase_mm": 1325, "prop_diameter_in": 30, "cells": 12, "capacity_mah": 44000,
    "battery_chemistry": "LiPo", "empty_kg": 32.5, "typical_takeoff_kg": 32.5,
    "max_takeoff_kg": 45, "max_current_a": 300, "reserve_pct": 20, "pack_mass_kg": 8,
    "payload": "fixed", "speed_goal": "fastest",
}  # fmt: skip

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
        Rehearsal(
            name="heavylift-build-up",
            description=(
                "The speed build-up on a heavy-lift X8 whose speed card verified 12 m/s:"
                " two steps, each WP_SPD one step past the verified speed with the logging"
                " its checks read, flown on the step's card (an out-and-back line in AUTO),"
                " and confirmed by that flight before the next is offered."
            ),
            setup=HEAVYLIFT_READY,
            answers=READY_ANSWERS,
            expects=("speed.build_up_step",),
            test_flight=buildup_card(),
            rounds=4,
            before=(copter_hover(15, altitude=5, raw_in_hover=True),),
            takeoff_kg=32.5,
            times=2,
        ),
        Rehearsal(
            name="heavylift-autotune",
            description=(
                "The gate's flights on a heavy-lift X8 with soft rate gains: tune checks"
                " light and loaded, the plan's rate filters and back-off loaded, AutoTune"
                " roll then pitch on fresh packs, and the tune checks again."
            ),
            setup=HEAVYLIFT_SOFT,
            answers={"payload": "variable", "prop_diameter_in": 30, "esc_firmware": "BLHeli_32"},
            expects=("tune.autotune_offer",),
            test_flight=HEAVYLIFT_SOFT.fly,
            before=(copter_hover(15, altitude=5, raw_in_hover=True),),
            loaded_kg=8.0,
            work=(
                Work("AutoTune roll", AUTOTUNE_LOG | {"AUTOTUNE_AXES": 1}, autotune(True), 5),
                Work("AutoTune pitch", AUTOTUNE_LOG | {"AUTOTUNE_AXES": 2}, autotune(True), 5),
            ),
        ),
    ]
}

__all__ = ["CALLISTO", "REHEARSALS", "Rehearsal", "Work"]
