"""The speed build-up against worked answers. The verified speed is the fastest fast
segment long enough, at the same weight or heavier, with every watched metric in target;
the next step is 1 m/s or 10% past it. At 13 m/s with frame drag k2 = 0.02 /m the lean
needed solves `g sin(theta) = k2 (V cos(theta))^2`: 18.1 deg. Each margin, failing alone,
is the limit that binds; a required prediction that cannot be made holds the step back."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

from flight_insights.analyzers import Registry
from flight_insights.analyzers.wind import Fit
from flight_insights.knowledge import load_pack
from flight_insights.performance.envelope import Fast, Inputs, next_speed, step, verified
from flight_insights.performance.fit import Model
from flight_insights.performance.thermal import Thermal
from flight_insights.performance.top_speed import Basis

REG = Registry.load(load_pack())
RULES = REG.rules["performance"]
DISC = 4 * math.pi * (22 * 0.0254 / 2) ** 2
RHO = 1.2
DRAG = Model(avionics=800.0, induced=0.0, flight=1.0, profile=0.0, growth=0.0, drag_area=0.5,
             disc_area=DISC, rms=0.0)  # fmt: skip
BASIS = Basis(Fit(0.0, 0.02, np.zeros(2), np.zeros(2), True, 0.0), 30.0, RHO, 0.5)
INPUTS = Inputs(DRAG, BASIS, {"ATC_ANGLE_MAX": 45.0}, lowest_volts=40.0, current_a=200.0)


def fast(fid: str, speed: float, seconds: float = 30.0, kg: float | None = 30.0,
         vibe: tuple[float, str] = (10.0, "target"),
         output: tuple[float, str] = (60.0, "target"), attitude: float = 0.3) -> Fast:  # fmt: skip
    return Fast(fid, speed, seconds, kg, {"fast.vibe_p95": vibe, "fast.output_max": output,
                                          "fast.lean_share": (40.0, "target"),
                                          "fast.attitude_error_deg": (attitude, None),
                                          "fast.velocity_error_ms": (0.2, None)})  # fmt: skip


HISTORY = [
    fast("a", 10.0, output=(60.0, "target")),
    fast("b", 12.0, kg=31.0, vibe=(12.0, "target"), output=(65.0, "target")),
    fast("short", 14.0, seconds=8.0),
    fast("light", 15.0, kg=25.0),
    fast("rough", 16.0, vibe=(40.0, "caution")),
    fast("unweighed", 17.0, kg=None),
]


def test_the_verified_speed_and_the_next_step() -> None:
    v = verified(HISTORY, 30.0, RULES)
    assert v is not None and v.flight_id == "b"
    assert next_speed(12.0, RULES) == pytest.approx(13.0)
    assert next_speed(5.0, RULES) == pytest.approx(5.5)
    assert verified(HISTORY, 40.0, RULES) is None  # nothing flown that heavy


def test_a_step_whose_margins_all_hold_is_offered() -> None:
    s = step(HISTORY, INPUTS, 30.0, RHO, REG)
    assert s.offered and s.binding is None and s.next_ms == pytest.approx(13.0)
    checks = {c.name: c for c in s.checks}
    assert checks["lean"].value == pytest.approx(100 * 18.12 / 45, abs=0.1)
    assert checks["thrust"].limit == pytest.approx(math.degrees(math.acos(0.5 / 0.8)))
    power = 800.0 + 0.5 * RHO * 0.5 * 13.0**3
    assert checks["current"].value == pytest.approx(power / 40.0)
    # through a (10 m/s), b (12) and the rough flight (16): long enough and heavy enough
    vibe = np.polyval(np.polyfit([10.0, 12.0, 16.0], [10.0, 12.0, 40.0], 1), 13.0)
    assert checks["vibration"].value == pytest.approx(vibe) and checks["vibration"].how == "trend"
    assert checks["output"].value == pytest.approx(np.polyval(np.polyfit([10.0, 12.0, 16.0],
                                                   [60.0, 65.0, 60.0], 1), 13.0))  # fmt: skip
    assert checks["attitude"].limit == pytest.approx(1.5 * 0.3)  # b's, the verified speed's
    assert checks["velocity"].holds
    assert s.missing == ("esc_temp",)  # no ESC telemetry: not required


@pytest.mark.parametrize(
    ("inputs", "history", "binds"),
    [
        (replace(INPUTS, params={"ATC_ANGLE_MAX": 20.0}), HISTORY, "lean"),
        (replace(INPUTS, basis=replace(BASIS, throttle=0.78)), HISTORY, "thrust"),
        (replace(INPUTS, current_a=30.0), HISTORY, "current"),
        (replace(INPUTS, params={"ATC_ANGLE_MAX": 45.0, "MOT_BAT_CURR_MAX": 30.0}), HISTORY,
         "current"),
        (replace(INPUTS, thermals=(Thermal(0, 60.0, 1.0, 25.0, 0.0, 20.0),), esc_limit_c=100.0),
         HISTORY, "esc_temp"),
        (INPUTS, [fast("a", 10.0, vibe=(20.0, "target")), fast("b", 12.0, vibe=(28.0, "target"))],
         "vibration"),
        # the attitude error grows fast past the verified speed: 0.3 deg there, 1.5 at 14 m/s
        (INPUTS, [fast("a", 10.0, attitude=0.2), fast("b", 12.0, attitude=0.3),
                  fast("c", 14.0, vibe=(35.0, "caution"), attitude=1.5)], "attitude"),
    ],
)  # fmt: skip
def test_each_margin_failing_alone_binds(inputs: Inputs, history: list[Fast], binds: str) -> None:
    s = step(history, inputs, 30.0, RHO, REG)
    assert not s.offered and s.binding == binds


def test_a_required_prediction_missing_holds_the_step_back() -> None:
    s = step(HISTORY, replace(INPUTS, basis=replace(BASIS, throttle=None)), 30.0, RHO, REG)
    assert not s.offered and s.binding == "thrust" and "thrust" in s.missing
    none = step([], INPUTS, 30.0, RHO, REG)
    assert not none.offered and none.binding == "verified" and none.next_ms is None


FIXED_NARROW = {"INS_HNTCH_ENABLE": 1, "INS_HNTCH_MODE": 0, "INS_HNTCH_FREQ": 70.0,
                "INS_HNTCH_BW": 2.0, "INS_HNTCH_HMNCS": 1}  # fmt: skip
STEP_HZ = 70.0 * math.sqrt(1 / math.cos(math.radians(18.12)))  # 13 m/s leans 18.1 deg: 71.8 Hz


def notched(params: dict[str, float], motor: tuple[float, float] | None = (70.0, 30.0)) -> Inputs:
    return replace(INPUTS, params={**INPUTS.params, **params}, motor_hz=motor)


@pytest.mark.parametrize(
    ("params", "reach", "holds"),
    [
        (FIXED_NARROW, 71.0, False),  # covers 69-71 Hz in hover; the step's 71.8 Hz is past it
        ({**FIXED_NARROW, "INS_HNTCH_BW": 20.0}, 80.0, True),
        # REF 0 in a tracking mode sits at FREQ, as a fixed notch does
        ({**FIXED_NARROW, "INS_HNTCH_MODE": 3, "INS_HNTCH_REF": 0.0}, 71.0, False),
        # the in-flight FFT finds the motors up to FFT_MAXHZ
        (
            {
                **FIXED_NARROW,
                "INS_HNTCH_MODE": 4,
                "INS_HNTCH_REF": 1.0,
                "FFT_MINHZ": 50.0,
                "FFT_MAXHZ": 71.0,
            },
            71.0,
            False,
        ),
        ({**FIXED_NARROW, "INS_HNTCH_MODE": 4, "INS_HNTCH_REF": 1.0}, 450.0, True),  # the default
    ],
)
def test_the_notch_reaches_the_motors_at_the_step(params: dict[str, float], reach: float,
                                                   holds: bool) -> None:  # fmt: skip
    s = step(HISTORY, notched(params), 30.0, RHO, REG)
    c = {c.name: c for c in s.checks}["notch"]
    assert c.value == pytest.approx(STEP_HZ, rel=1e-3) and c.limit == pytest.approx(reach)
    assert c.holds is holds and c.how == "model"
    assert s.offered is holds and s.binding == (None if holds else "notch")


@pytest.mark.parametrize(
    "params",
    [
        {**FIXED_NARROW, "INS_HNTCH_MODE": 3, "INS_HNTCH_REF": 1.0},  # follows ESC telemetry
        {**FIXED_NARROW, "INS_HNTCH_MODE": 1, "INS_HNTCH_REF": 0.3},  # follows the throttle
        {**FIXED_NARROW, "INS_HNTCH_FREQ": 150.0, "INS_HNTCH_BW": 20.0},  # a frame resonance's
        {**FIXED_NARROW, "INS_HNTCH_ENABLE": 0},
    ],
)
def test_no_notch_check_where_no_static_notch_covers_the_motors(params: dict[str, float]) -> None:
    s = step(HISTORY, notched(params), 30.0, RHO, REG)
    assert "notch" not in {c.name for c in s.checks} and s.offered


def test_the_motors_frequency_carried_to_the_weight_and_unknown_without_it() -> None:
    # measured in hover at 25 kg: at 30 kg the motors turn sqrt(30 / 25) faster again
    s = step(HISTORY, notched({**FIXED_NARROW, "INS_HNTCH_BW": 40.0}, (70.0, 25.0)), 30.0, RHO, REG)
    c = {c.name: c for c in s.checks}["notch"]
    assert c.value == pytest.approx(STEP_HZ * math.sqrt(30 / 25), rel=1e-3)
    # no hover frequency measured: the check is missing, and it does not hold the step back
    s = step(HISTORY, notched(FIXED_NARROW, None), 30.0, RHO, REG)
    assert "notch" in s.missing and s.offered
