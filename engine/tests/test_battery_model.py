"""The battery model on packs made to a known answer: the discharge's arithmetic, each way
it stops (the cutoff, the firmware's voltage and capacity failsafes, the reserve, the
pack's power limit), and a pack's capacity and resistance recovered from a flight."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from flight_insights.knowledge import load_pack
from flight_insights.performance.battery import (
    Chemistry,
    Failsafe,
    Pack,
    chemistry,
    discharge,
    fit_pack,
    lowpass,
    usable,
    usable_curve,
)

RULES: dict[str, Any] = dict(load_pack().json("rules/ocv.json"))
LIPO = chemistry(RULES, "LiPo")
FLAT = Chemistry("flat", np.array([0.0, 1.0]), np.array([3.7, 3.7]), 3.0, True, True)
SLOPE = Chemistry("slope", np.array([0.0, 1.0]), np.array([2.8, 4.2]), 3.0, True, True)


def test_the_curves_read_both_ways() -> None:
    for soc in (0.1, 0.35, 0.5, 0.8, 0.99):
        assert float(LIPO.soc_at(LIPO.ocv(soc))) == pytest.approx(soc, abs=1e-9)
    assert float(LIPO.ocv(1.0)) == pytest.approx(4.173)
    lihv = chemistry(RULES, "LiHV")
    assert float(lihv.ocv(1.0)) == pytest.approx(4.35) and not lihv.measured_shape
    assert not chemistry(RULES, "LiFePO4").charge_from_voltage
    assert chemistry(RULES, "NiMH").name == "LiPo"  # unknown: the measured curve


def test_a_flat_pack_without_resistance_gives_its_energy_at_any_power() -> None:
    pack = Pack(FLAT, 6, 10.0, 0.0)  # 22.2 V x 10 Ah = 222 Wh
    d = discharge(pack, 1.0, 444.0, dt=0.5)
    assert d.stopped_by == "empty"
    assert d.energy_wh == pytest.approx(222.0, rel=1e-3)
    run = discharge(pack, 1.0, 444.0, None, 111.0, dt=0.5)
    assert run.stopped_by == "reserve"
    assert run.seconds == pytest.approx(111.0 / 444.0 * 3600, abs=1.0)
    assert run.charge_ah == pytest.approx(111.0 / 22.2, rel=1e-3)


def test_resistance_costs_energy_and_more_so_at_high_power() -> None:
    """At P from E with R, I = (E - sqrt(E^2 - 4RP)) / 2R: a loss of I^2 R."""
    pack = Pack(FLAT, 6, 10.0, 0.05)
    e = 22.2
    for p in (300.0, 1500.0):
        amps = (e - np.sqrt(e * e - 4 * 0.05 * p)) / (2 * 0.05)
        run = discharge(pack, 1.0, p, None, 50.0, dt=0.1)
        assert run.charge_ah / (run.seconds / 3600) == pytest.approx(amps, rel=1e-6)
    light = usable(pack, 1.0, 300.0, None, 0.0, dt=1.0)
    heavy = usable(pack, 1.0, 1500.0, None, 0.0, dt=1.0)
    assert heavy.energy_wh < light.energy_wh


def test_the_cutoff_stops_a_sloping_pack() -> None:
    pack = Pack(SLOPE, 4, 5.0, 0.02)
    d = discharge(pack, 1.0, 200.0, dt=1.0)
    assert d.stopped_by == "cutoff"
    e = 4 * float(SLOPE.ocv(d.end_soc))
    amps = (e - np.sqrt(e * e - 4 * 0.02 * 200.0)) / (2 * 0.02)
    assert (e - amps * 0.02) / 4 == pytest.approx(3.0, abs=0.01)


def test_the_capacity_failsafe_counts_from_zero_like_the_firmware() -> None:
    """The firmware counts charge from start-up against BATT_CAPACITY, whatever the
    pack's real charge: a half-charged pack still flies to CAPACITY - LOW_MAH."""
    pack = Pack(FLAT, 6, 10.0, 0.0)
    fs = Failsafe(capacity_mah=10000, low_mah=2500)
    full = discharge(pack, 1.0, 222.0, fs, dt=1.0)
    assert full.stopped_by == "capacity_failsafe"
    assert full.charge_ah == pytest.approx(7.5, abs=0.07)


def test_the_curve_says_what_ends_a_flight_at_each_load() -> None:
    """A capacity failsafe set below the pack ends every load before the reserve; without
    it the reserve does; above what the pack can give, the power."""
    pack = Pack(LIPO, 12, 30.0, 0.02)
    early = usable_curve(pack, 1.0, Failsafe(capacity_mah=3000, low_mah=450), 0.2, 1000, 6000, 6)
    assert early.stopped(3100.0) == "capacity_failsafe"
    late = usable_curve(pack, 1.0, Failsafe(capacity_mah=30000), 0.2, 1000, 6000, 6)
    assert late.stopped(3100.0) == "reserve" and late.stopped(1e6) == "power"


def test_the_voltage_failsafe_waits_its_timer() -> None:
    pack = Pack(SLOPE, 4, 5.0, 0.0)
    fs = Failsafe(low_volt=4 * 3.5, timer_s=10.0)
    d = discharge(pack, 1.0, 100.0, fs, dt=1.0)
    assert d.stopped_by == "voltage_failsafe"
    crossing = discharge(pack, 1.0, 100.0, None, None, dt=1.0)
    # the time the voltage first went under, from a run that only stops at it
    under = discharge(pack, 1.0, 100.0, Failsafe(low_volt=4 * 3.5, timer_s=0.0), dt=1.0)
    assert d.seconds == pytest.approx(under.seconds + 10.0, abs=1.0)
    assert crossing.seconds > d.seconds


def test_the_sag_compensated_voltage_trips_later() -> None:
    pack = Pack(SLOPE, 4, 5.0, 0.05)
    raw = discharge(pack, 1.0, 300.0, Failsafe(low_volt=14.4, timer_s=0.0))
    rested = discharge(pack, 1.0, 300.0, Failsafe(low_volt=14.4, timer_s=0.0, resting=True))
    assert raw.stopped_by == rested.stopped_by == "voltage_failsafe"
    assert rested.seconds > raw.seconds


def test_the_reserve_is_the_larger_of_the_failsafe_and_the_share() -> None:
    pack = Pack(LIPO, 12, 22.0, 0.02)
    loose = Failsafe(capacity_mah=22000, low_mah=1000)  # keeps under 5%
    tight = Failsafe(capacity_mah=22000, low_mah=8000)  # keeps over a third
    to_cutoff = usable(pack, 1.0, 3000.0, None, 0.0)
    share = usable(pack, 1.0, 3000.0, loose, 0.2)
    assert share.stopped_by == "reserve"
    assert share.energy_wh == pytest.approx(0.8 * to_cutoff.energy_wh, rel=0.01)
    held = usable(pack, 1.0, 3000.0, tight, 0.2)
    assert held.stopped_by == "capacity_failsafe" and held.energy_wh < share.energy_wh


def test_more_power_than_the_pack_can_give_stops_at_once() -> None:
    pack = Pack(FLAT, 6, 10.0, 0.5)  # E^2 / 4R = 246 W
    assert discharge(pack, 1.0, 300.0).stopped_by == "power"
    assert discharge(pack, 1.0, 100.0, Failsafe(current_limit_a=2.0)).stopped_by == "current"


def flown(q: float, r0: float, r1: float, cells: int = 12, z0: float = 0.97,
          seconds: float = 900.0, noise: float = 0.0) -> tuple[Any, ...]:  # fmt: skip
    """A flight on a known LiPo pack: hover current with steps (climbs, legs), 10 Hz."""
    rng = np.random.default_rng(3)
    t = np.arange(0, seconds, 0.1)
    amps = 40 + 15 * np.sign(np.sin(2 * np.pi * t / 47)) + 5 * np.sin(2 * np.pi * t / 13)
    used = np.concatenate([[0.0], np.cumsum(np.diff(t) * (amps[1:] + amps[:-1]) / 2)]) / 3600
    slow = lowpass(t, amps, float(RULES["fit"]["tau_s"]))
    volts = cells * LIPO.ocv(z0 - used / q) - r0 * amps - r1 * slow
    volts = volts + rng.normal(0, noise, len(t))
    rest = cells * float(LIPO.ocv(z0)) - 0.5 * r0  # 0.5 A of avionics at rest
    return t, volts, amps, rest, 0.5


def test_a_packs_capacity_and_resistance_come_back_from_a_flight() -> None:
    t, v, i, rest, rest_a = flown(22.0, 0.035, 0.012, noise=0.02)
    fit = fit_pack(t, v, i, rest, rest_a, 12, LIPO, 22.0 * 1.1, RULES)  # rated 10% high
    assert fit.capacity_ah == pytest.approx(22.0, rel=0.02)
    assert fit.resistance == pytest.approx(0.035, rel=0.05)
    assert fit.slow_resistance == pytest.approx(0.012, rel=0.15)
    assert fit.start_soc == pytest.approx(0.97, abs=0.005)
    assert fit.rms_v < 0.05


def test_a_short_flight_gives_the_resistance_only() -> None:
    t, v, i, rest, rest_a = flown(22.0, 0.035, 0.012, seconds=120.0)
    fit = fit_pack(t, v, i, rest, rest_a, 12, LIPO, 22.0, RULES)
    assert fit.capacity_ah is None
    assert fit.resistance == pytest.approx(0.035, rel=0.05)
