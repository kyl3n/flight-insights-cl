"""The rate loop's health in ordinary flight: the I term at its limit, the slew limiter,
D-term noise and oscillation in steady hover, each on flights whose answer is known."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import flight, table

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.model import Result
from flight_insights.analyzers.rate_health import RATE_HEALTH
from flight_insights.knowledge import load_pack

HZ = 400.0
ARM, DISARM = 1_000_000, 91_000_000
HOVER = (20_000_000, 80_000_000)  # the fingerprint's steady hover
DEG_S = {b"unit": b"deg/s", b"multiplier": b"1"}
METRE = {b"unit": b"m", b"multiplier": b"1"}
Signal = Callable[[np.ndarray[Any, Any]], np.ndarray[Any, Any]]


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def _t(hz: float = HZ) -> np.ndarray[Any, Any]:
    return np.arange(ARM, DISARM, int(1e6 / hz), dtype=np.int64)


def _zero(s: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    return np.zeros(len(s))


def health_flight(
    i: Signal = _zero,
    d: Signal = _zero,
    dmod: Signal | None = None,
    rate: Signal = _zero,
    target: Signal = _zero,
    pid_hz: float = HZ,
    motor_hz: float | None = None,
    vehicle: str = "copter",
    **params: float,
) -> FlightData:
    """Roll carries the signals (of time in seconds); pitch and yaw are still."""
    t = _t(pid_hz)
    s = (t - ARM) / 1e6
    zero = np.zeros(len(t))
    pids: dict[str, pa.Table] = {}
    for name in ("PIDR", "PIDP", "PIDY"):
        roll = name == "PIDR"
        columns: dict[str, tuple[pa.DataType, Any]] = {
            "Tar": (pa.float32(), zero.tolist()),
            "Act": (pa.float32(), zero.tolist()),
            "I": (pa.float32(), (i(s) if roll else zero).tolist()),
            "D": (pa.float32(), (d(s) if roll else zero).tolist()),
            "Dmod": (pa.float32(), (dmod(s) if roll and dmod else np.ones(len(t))).tolist()),
            "SRate": (pa.float32(), zero.tolist()),
            "Flags": (pa.uint8(), [0] * len(t)),
        }
        pids[name] = table(t.tolist(), columns)
    tr = _t()
    sr = (tr - ARM) / 1e6
    zr = np.zeros(len(tr)).tolist()
    rates = table(
        tr.tolist(),
        {"RDes": (pa.float32(), target(sr).tolist()), "R": (pa.float32(), rate(sr).tolist()),
         "PDes": (pa.float32(), zr), "P": (pa.float32(), zr),
         "YDes": (pa.float32(), zr), "Y": (pa.float32(), zr)},
        {k: DEG_S for k in ("RDes", "R", "PDes", "P", "YDes", "Y")},
    )  # fmt: skip
    climb = [0, ARM + 2_000_000, ARM + 2_500_000, DISARM]
    tables = {
        **pids,
        "RATE": rates,
        "MODE": table([0], {"ModeNum": (pa.uint8(), [2])}),
        "CTUN": table(climb, {"Alt": (pa.float32(), [0.0, 0.0, 12.0, 12.0])}, {"Alt": METRE}),
    }
    fingerprint: dict[str, Any] = {
        "vehicle": vehicle,
        "board": {"hal": "ChibiOS"},
        "hover_windows": [{"start_us": HOVER[0], "end_us": HOVER[1]}],
    }
    if motor_hz is not None:
        fingerprint["motor_hz_at_hover"] = {"value": motor_hz}
    defaults = {
        "SCHED_LOOP_RATE": 400.0, "ATC_RAT_RLL_IMAX": 0.5, "ATC_RAT_PIT_IMAX": 0.5,
        "ATC_RAT_YAW_IMAX": 0.5, "ATC_RAT_RLL_D": 0.0036, "ATC_RAT_PIT_D": 0.0036,
        "ATC_RAT_YAW_D": 0.0, "ATC_RAT_RLL_SMAX": 0.0, "ATC_RAT_PIT_SMAX": 0.0,
        "ATC_RAT_YAW_SMAX": 0.0,
    }  # fmt: skip
    return flight(tables, ARM, DISARM, params=defaults | params, fingerprint=fingerprint)


def roll(r: Result, kind: str) -> Any:
    return next(m for m in r.metrics if m.metric == f"tune.{kind}_roll")


def codes(r: Result) -> set[str]:
    return {f.code for f in r.findings}


def noise(seed: int, scale: float) -> Signal:
    return lambda s: np.random.default_rng(seed).normal(0, scale, len(s))


# --- the I term -------------------------------------------------------------------------------


def test_an_i_term_pinned_at_its_limit_is_a_finding(reg: Registry) -> None:
    """Pinned at IMAX for the last fifth of the flying time: an off-centre load."""
    out = RATE_HEALTH.run(health_flight(i=lambda s: np.where(s > 72.0, 0.5, 0.1)), reg)
    share = roll(out, "i_at_limit")
    assert share.value == pytest.approx(100 * 18 / 87.5, abs=0.5)  # flying from 2.5 s
    assert share.zone == "caution"
    [finding] = [f for f in out.findings if f.code == "tune.i_limit"]
    assert finding.params["axes"] == ["roll"] and finding.params["axis"] == "roll"
    assert [e.id for e in finding.evidence] == ["rate_i:roll"]


def test_an_i_term_inside_its_limit_says_so_and_asks_nothing(reg: Registry) -> None:
    out = RATE_HEALTH.run(health_flight(i=lambda s: 0.3 + 0.1 * np.sin(s)), reg)
    assert roll(out, "i_at_limit").value == 0.0 and "tune.i_limit" not in codes(out)
    ev = next(e for e in out.evidence if e.id == "rate_i:roll")
    assert ev.values["median_share"] == pytest.approx(0.6, abs=0.05)


def test_ten_hz_pids_still_show_the_i_term(reg: Registry) -> None:
    """The I term moves slowly; Copter's default 10 Hz PID logging is enough for it."""
    out = RATE_HEALTH.run(health_flight(i=lambda s: np.full(len(s), 0.5), pid_hz=10.0), reg)
    assert roll(out, "i_at_limit").value == pytest.approx(100.0)


# --- the slew limiter -------------------------------------------------------------------------


def test_the_slew_limiter_acting_is_a_finding(reg: Registry) -> None:
    def dmod(s: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
        return np.where((s > 40) & (s < 49), 0.6, 1.0)

    out = RATE_HEALTH.run(health_flight(dmod=dmod, ATC_RAT_RLL_SMAX=50.0), reg)
    assert roll(out, "slew_limited").value == pytest.approx(100 * 9 / 87.5, abs=0.5)
    [finding] = [f for f in out.findings if f.code == "tune.slew_limited"]
    assert finding.params["smax"] == 50.0


def test_with_smax_off_the_limiter_is_not_judged(reg: Registry) -> None:
    out = RATE_HEALTH.run(health_flight(dmod=lambda s: np.full(len(s), 0.5)), reg)
    assert not [m for m in out.metrics if m.metric.startswith("tune.slew_limited")]


# --- D-term noise -----------------------------------------------------------------------------


def test_d_term_noise_share_is_the_power_above_the_control_band(reg: Registry) -> None:
    """A 5 Hz control part and an 80 Hz noise part twice as large: 80% is noise."""

    def d(s: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
        return 0.01 * np.sin(2 * np.pi * 5 * s) + 0.02 * np.sin(2 * np.pi * 80 * s)

    out = RATE_HEALTH.run(health_flight(d=d), reg)
    assert roll(out, "d_noise").value == pytest.approx(80.0, abs=2.0)
    assert "tune.d_noise" in codes(out)


def test_a_clean_d_term_is_not_noise(reg: Registry) -> None:
    out = RATE_HEALTH.run(health_flight(d=lambda s: 0.01 * np.sin(2 * np.pi * 5 * s)), reg)
    assert roll(out, "d_noise").value < 5.0 and "tune.d_noise" not in codes(out)


def test_no_d_gain_no_d_noise(reg: Registry) -> None:
    out = RATE_HEALTH.run(health_flight(d=noise(1, 0.01), ATC_RAT_RLL_D=0.0), reg)
    assert not [m for m in out.metrics if m.metric == "tune.d_noise_roll"]


# --- oscillation in hover ---------------------------------------------------------------------


def test_an_oscillation_in_hover_is_found(reg: Registry) -> None:
    """5 deg/s at 12 Hz on top of noise: 3.5 deg/s RMS."""

    def rate(s: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
        return 5 * np.sin(2 * np.pi * 12 * s) + noise(2, 0.3)(s)

    out = RATE_HEALTH.run(health_flight(rate=rate), reg)
    [finding] = [f for f in out.findings if f.code == "tune.oscillation"]
    assert finding.params["hz"] == pytest.approx(12.0, abs=0.3)
    assert finding.params["dps"] == pytest.approx(3.5, abs=0.3)
    assert roll(out, "hover_unsteady").value == pytest.approx(3.5, abs=0.3)


def test_a_motor_harmonic_is_not_an_oscillation(reg: Registry) -> None:
    def rate(s: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
        return 5 * np.sin(2 * np.pi * 12 * s) + noise(2, 0.3)(s)

    out = RATE_HEALTH.run(health_flight(rate=rate, motor_hz=6.0), reg)  # 12 Hz: its 2nd
    assert "tune.oscillation" not in codes(out)


def test_stick_inputs_in_hover_are_not_read_as_oscillation(reg: Registry) -> None:
    """Doublets every 5 s through the hover, the rate following them: no finding."""

    def target(s: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
        phase = np.mod(s - 20.0, 5.0)
        lobe = np.where(phase < 1, 40.0, np.where(phase < 2, -40.0, 0.0))
        return np.where((s >= 20) & (s < 80), lobe, 0.0)

    out = RATE_HEALTH.run(health_flight(rate=target, target=target), reg)
    assert "tune.oscillation" not in codes(out)


# --- what the log must hold -------------------------------------------------------------------


def test_planes_are_not_asked(reg: Registry) -> None:
    out = RATE_HEALTH.run(health_flight(vehicle="plane"), reg)
    assert out.skipped == "the rate-loop health checks are for Copter"


def test_a_parameter_file_has_nothing_to_judge(reg: Registry) -> None:
    from dataclasses import replace

    out = RATE_HEALTH.run(replace(health_flight(), cache=None), reg)
    assert out.skipped == "a parameter file has no flight to measure"


def test_every_threshold_comes_from_the_rule_pack(reg: Registry) -> None:
    rules: Mapping[str, Any] = reg.rules["tune"]["health"]
    assert rules["calibrated"] is False and rules["sources"]
    assert rules["noise_band_low_hz"] == reg.rules["filters"]["noise_band_low_hz"]
