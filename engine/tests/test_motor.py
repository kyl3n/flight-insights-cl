"""The motor and ESC as one drive (performance/motor.py), against made-up motors with known
constants: the power balance holds exactly, a table made from a motor gives its constants
back, the full-throttle step gives back a Kv the maker rounded, and the fastest a motor
turns a propeller matches a brute-force search."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from flight_insights.analyzers import Registry
from flight_insights.knowledge import load_pack
from flight_insights.performance.bench import Row
from flight_insights.performance.motor import (
    RPM_TO_RAD,
    Motor,
    from_datasheet,
    from_table,
    from_tables,
)

RULES: dict[str, Any] = dict(Registry.load(load_pack()).rules["performance"])
TRUE = Motor(kv=170.0, r0=0.060, r2=2e-6, i00=0.6, i01=0.002, i02=1e-6)
CQ = 1.0e-5  # a 22-inch propeller's torque, N m per (rad/s)^2: about 1 N m at 3000 rpm


def prop(omega: Any) -> Any:
    return CQ * np.asarray(omega, float) ** 2


def table(m: Motor, volts: float = 48.0, noise: float = 0.0, top: float = 1.0,
          seed: int = 3) -> list[Row]:  # fmt: skip
    """A bench table the motor `m` and the propeller give: each throttle step's speed is
    where the voltage the motor needs is that share of the supply's top duty."""
    rng = np.random.default_rng(seed)
    out: list[Row] = []
    for pct in (40, 50, 60, 70, 80, 90, 100):
        duty = pct / 100 * top * float(RULES["motor"]["full_duty"])
        w = m.top_speed(prop, volts, duty)
        q = float(prop(w))
        p = float(m.power(q, w)) * (1 + rng.normal(0, noise))
        out.append(Row(float(pct), 1000.0 * pct, p, rpm=w / RPM_TO_RAD, torque_nm=q,
                       current_a=p / volts, volts=volts))  # fmt: skip
    return out


def test_the_power_drawn_is_the_shaft_power_and_the_losses() -> None:
    q, w = np.array([0.4, 1.0, 2.5]), np.array([200.0, 330.0, 520.0])
    iron, copper = TRUE.losses(q, w)
    assert TRUE.power(q, w) == pytest.approx(q * w + iron + copper)
    eff = TRUE.efficiency(q, w)
    assert np.all((eff > 0.7) & (eff < 1.0))
    # first order, from a datasheet: the textbook efficiency (1 - i0/i)(1 - iR/v)
    m = from_datasheet(kv=170.0, resistance_mohm=60.0, no_load_a=0.6)
    i, v = m.current(1.0, 330.0), m.volts(1.0, 330.0)
    assert float(m.efficiency(1.0, 330.0)) == pytest.approx(
        float((1 - 0.6 / i) * (1 - i * 0.06 / v))
    )


def test_a_table_gives_the_motors_constants_back() -> None:
    rows = table(TRUE)
    got = from_table(rows, kv=170.0, rules=RULES)
    assert got is not None and got.source == "table" and got.steps == 7
    # Kv, R and the no-load current trade off against each other in the power a table
    # holds, so the constants come back near, and the power the drive draws closely
    assert got.kv_from == "full throttle" and got.kv == pytest.approx(170.0, rel=0.02)
    assert got.rms is not None and got.rms < 1e-3
    # between the steps and 10% faster than the fastest
    speeds = [r.rpm * RPM_TO_RAD for r in rows if r.rpm]
    w = np.linspace(min(speeds), 1.1 * max(speeds), 25)
    assert got.power(prop(w), w) == pytest.approx(TRUE.power(prop(w), w), rel=5e-3)
    # and the fastest it turns the propeller at another supply voltage
    assert got.top_speed(prop, 44.0, 0.98) == pytest.approx(
        TRUE.top_speed(prop, 44.0, 0.98), rel=5e-3
    )


def test_with_measurement_noise_the_power_stays_close() -> None:
    got = from_table(table(TRUE, noise=0.01), kv=170.0, rules=RULES)
    assert got is not None and got.rms is not None and got.rms < 0.02
    w = np.linspace(320.0, 700.0, 9)  # the table's speeds
    assert got.power(prop(w), w) == pytest.approx(TRUE.power(prop(w), w), rel=0.02)


def test_the_full_throttle_step_corrects_a_rounded_kv() -> None:
    # the maker prints 180 for a motor that turns as 170: the top step gives it back
    got = from_table(table(TRUE), kv=180.0, rules=RULES)
    assert got is not None and got.kv == pytest.approx(170.0, rel=0.02)
    # a table that stops short of full throttle keeps the Kv given
    short = [r for r in table(TRUE) if r.throttle_pct < 90]
    kept = from_table(short, kv=180.0, rules=RULES)
    assert kept is not None and kept.kv_from == "given" and kept.kv == 180.0


def test_a_table_without_speed_and_torque_gives_no_motor() -> None:
    plain = [Row(r.throttle_pct, r.thrust_g, r.power_w) for r in table(TRUE)]
    assert from_table(plain, kv=170.0, rules=RULES) is None
    assert from_table(table(TRUE)[:3], kv=170.0, rules=RULES) is None  # too few steps


def test_the_fastest_the_motor_turns_a_propeller() -> None:
    w = TRUE.top_speed(prop, 46.0, 0.98)
    grid = np.linspace(1.0, TRUE.k * 46.0, 200_001)
    need = TRUE.volts(prop(grid), grid)
    brute = grid[np.flatnonzero(need <= 0.98 * 46.0)[-1]]
    assert w == pytest.approx(brute, abs=0.01)
    assert w < TRUE.k * 46.0 * 0.98  # under load, slower than the unloaded speed
    assert math.isclose(float(TRUE.volts(prop(w), w)), 0.98 * 46.0, rel_tol=1e-6)


def test_one_motor_from_its_tables_on_two_propellers() -> None:
    # the same motor on a smaller propeller: half the torque at a speed
    def small(omega: Any) -> Any:
        return 0.5 * prop(omega)

    rows: list[Row] = []
    for pct in (40, 55, 70, 85, 100):
        w = TRUE.top_speed(small, 48.0, pct / 100 * 0.98)
        q = float(small(w))
        rows.append(Row(float(pct), 500.0 * pct, float(TRUE.power(q, w)), rpm=w / RPM_TO_RAD,
                        torque_nm=q, volts=48.0))  # fmt: skip
    both = from_tables([table(TRUE), rows], kv=175.0, rules=RULES)
    assert both is not None and both.steps == 12 and both.kv == pytest.approx(170.0, rel=0.02)
    for load in (prop, small):  # either propeller's power, across both tables' speeds
        w = np.linspace(300.0, 900.0, 13)
        assert both.power(load(w), w) == pytest.approx(TRUE.power(load(w), w), rel=5e-3)
