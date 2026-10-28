"""The energy analyzer on flights made to a known answer. The speed sweep: straight legs
flown both ways count as one speed each, whatever the wind along them; legs flown one way,
turning, low or short do not. A flight's energy: the packs' energy, the distance over the
ground, energy per km in all and at cruise, and the energy by phase adding up to the whole."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import flight, table

from flight_insights.analyzers.battery_power import supply
from flight_insights.analyzers.energy import ENERGY, flight_energy, legs, levels, pairs, tiles
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.wind import ekf
from flight_insights.knowledge import load_pack

REG = Registry.load(load_pack())
RULES: dict[str, Any] = dict(REG.rules["energy"]["sweep"])
HZ = 10  # XKF1 as LOG_FILE_RATEMAX 10 logs it
ARM = 1_000_000
V = {b"unit": b"V", b"multiplier": b"1.0"}
A = {b"unit": b"A", b"multiplier": b"1.0"}

Piece = tuple[float, float, float, float]  # seconds, north m/s, east m/s, climb m/s


def made(
    pieces: Sequence[Piece],
    height: float = 20.0,
    ramp: float = 3.0,  # type: ignore[no-untyped-def]
    amps: float | None = None,
    volts: float = 50.0,
):
    """A flight from the ground: a climb to `height`, then each piece at a constant ground
    velocity, joined by `ramp` s of straight-line change in velocity (as a turnaround),
    then disarmed. XKF1 from one core at 10 Hz; with `amps`, one pack monitor drawing that
    current at `volts` throughout, at the same rows."""
    t: list[float] = []
    vn: list[float] = []
    ve: list[float] = []
    vd: list[float] = []
    pd: list[float] = []
    now, down = 0.0, 0.0
    step = 1.0 / HZ

    def emit(n: float, e: float, climb: float) -> None:
        nonlocal now, down
        t.append(now)
        vn.append(n)
        ve.append(e)
        vd.append(-climb)
        pd.append(down)
        down -= climb * step
        now += step

    for _ in range(round(2 * HZ)):
        emit(0, 0, 0)
    climb = 2.0
    for _ in range(round(height / climb * HZ)):
        emit(0, 0, climb)
    last = (0.0, 0.0)
    for seconds, n, e, c in pieces:
        for k in range(round(ramp * HZ)):
            f = (k + 1) / (ramp * HZ)
            emit(last[0] + f * (n - last[0]), last[1] + f * (e - last[1]), 0)
        for _ in range(round(seconds * HZ)):
            emit(n, e, c)
        last = (n, e)
    times = [ARM + round(x * 1e6) for x in t]
    yaw = [math.degrees(math.atan2(e, n)) % 360 for n, e in zip(vn, ve, strict=True)]
    n_rows = len(times)
    x1 = table(times, {
        "C": (pa.uint8(), [0] * n_rows),
        "Roll": (pa.float32(), [0.0] * n_rows),
        "Pitch": (pa.float32(), [0.0] * n_rows),
        "Yaw": (pa.float32(), yaw),
        "VN": (pa.float32(), vn),
        "VE": (pa.float32(), ve),
        "VD": (pa.float32(), vd),
        "PD": (pa.float32(), pd),
    })  # fmt: skip
    if amps is None:
        return flight({"XKF1": x1}, ARM, times[-1])
    bat = table(times, {
        "Inst": (pa.uint8(), [0] * n_rows),
        "Volt": (pa.float32(), [volts] * n_rows),
        "Curr": (pa.float32(), [amps] * n_rows),
    }, {"Volt": V, "Curr": A})  # fmt: skip
    return flight({"XKF1": x1, "BAT": bat}, ARM, times[-1], params={"BATT_MONITOR": 4})


def sweep(speeds: Sequence[float], wind_along: float = 0.0, hold: float = 20.0) -> list[Piece]:
    """Legs east and back west at each speed. With `wind_along`, the pilot holds the
    airspeed, so the ground speed is that much higher east and lower west."""
    out: list[Piece] = [(20.0, 0.0, 0.0, 0.0)]  # the hover
    for v in speeds:
        out += [(hold, 0.0, v + wind_along, 0.0), (hold, 0.0, -(v - wind_along), 0.0)]
    return [*out, (5.0, 0.0, 0.0, 0.0)]


def measured(f: Any) -> dict[str, float]:
    r = ENERGY.run(f, REG)
    assert r.skipped is None
    return {m.metric: m.value for m in r.metrics}


def test_each_speed_flown_both_ways_counts_once() -> None:
    f = made(sweep([4.0, 6.0, 8.0, 10.0]))
    m = measured(f)
    assert m["energy.sweep_levels"] == 4
    assert m["energy.sweep_fastest_ms"] == pytest.approx(10.0, abs=1e-4)
    [ev] = [x for x in ENERGY.run(f, REG).evidence if x.id == "energy:sweep"]
    assert ev.values["legs"] == 8 and ev.values["pairs"] == 4
    assert ev.values["slowest_ms"] == pytest.approx(4.0, abs=0.01)


def test_the_wind_along_the_legs_cancels() -> None:
    """A pilot holding 4 to 10 m/s of airspeed in 2.5 m/s of wind along the legs flies 6.5
    and 1.5 m/s over the ground: each pair still reads as its airspeed."""
    m = measured(made(sweep([4.0, 6.0, 8.0, 10.0], wind_along=2.5)))
    assert m["energy.sweep_levels"] == 4
    assert m["energy.sweep_fastest_ms"] == pytest.approx(10.0, abs=1e-4)


def test_legs_one_way_only_are_no_sweep() -> None:
    pieces: list[Piece] = [(20.0, 0.0, 0.0, 0.0)]
    for v in (4.0, 6.0, 8.0, 10.0):
        pieces += [(20.0, 0.0, v, 0.0), (5.0, 0.0, 0.0, 0.0)]  # east, stop, east again
    m = measured(made(pieces))
    assert m["energy.sweep_levels"] == 0 and "energy.sweep_fastest_ms" not in m


def test_one_speed_flown_many_times_is_one_speed() -> None:
    m = measured(made(sweep([8.0, 8.0, 8.5, 8.0])))
    assert m["energy.sweep_levels"] == 1


def test_low_or_short_legs_do_not_count() -> None:
    assert measured(made(sweep([4.0, 6.0, 8.0, 10.0]), height=3.0))["energy.sweep_levels"] == 0
    assert measured(made(sweep([4.0, 6.0, 8.0, 10.0], hold=6.0)))["energy.sweep_levels"] == 0


def test_a_turning_track_is_no_leg() -> None:
    """A circle at 6 m/s turning 10 deg/s: steady speed, but never straight."""
    rate = math.radians(10.0)
    e = {
        "t": np.arange(0, 120, 0.1),
    }
    e["t"] = (ARM + e["t"] * 1e6).astype(np.int64)
    k = np.arange(len(e["t"])) * 0.1
    e["VN"] = 6.0 * np.cos(rate * k)
    e["VE"] = 6.0 * np.sin(rate * k)
    e["VD"] = np.zeros_like(k)
    e["Roll"] = e["Pitch"] = np.zeros_like(k)
    e["PD"] = np.full_like(k, -20.0)
    e["PD"][0] = 0.0  # take-off height
    found = tiles(e, ARM, int(e["t"][-1]), RULES)
    assert found == []


def test_a_pair_needs_the_next_leg_to_head_back_soon() -> None:
    """East, then a long hover, then west: too far apart to be one speed flown both ways."""
    pieces: list[Piece] = [(20.0, 0.0, 6.0, 0.0), (80.0, 0.0, 0.0, 0.0), (20.0, 0.0, -6.0, 0.0)]
    f = made(pieces)
    e = ekf(f)
    assert e is not None
    found = legs(tiles(e, f.arm_us, f.disarm_us, RULES), RULES)
    assert len(found) == 2 and pairs(found, RULES) == []
    assert levels(pairs(found, RULES), RULES) == []


def test_skipped_for_other_vehicles() -> None:
    f = made(sweep([4.0, 6.0]))
    plane = flight({}, f.arm_us, f.disarm_us, fingerprint={"vehicle": "plane"})
    assert ENERGY.run(plane, REG).skipped


def test_a_flights_energy_and_distance_are_their_known_answers() -> None:
    """1 kW throughout, a climb, a hover, then 6 m/s east and back west: the energy is the
    power times the time, the distance the speeds times their times, and the phases add up
    to the whole."""
    f = made(sweep([6.0]), amps=20.0, volts=50.0)
    e = ekf(f)
    packs = supply(f, REG.rules["batteries"])
    assert e is not None and packs is not None
    fe = flight_energy(e, packs, f.arm_us, f.disarm_us, REG.rules["energy"]["phases"], 1.0)
    seconds = (f.disarm_us - f.arm_us) / 1e6
    assert fe.used_wh == pytest.approx(1000.0 * seconds / 3600, rel=1e-6)
    assert fe.used_mah == pytest.approx(20.0 * seconds / 3.6, rel=1e-6)
    speed = np.hypot(e["VN"], e["VE"])
    assert fe.flown_m == pytest.approx(float(np.trapezoid(speed, e["t"] / 1e6)), rel=1e-9)
    # two 20 s legs at 6 m/s, and three 3 s ramps (up, the turnaround, down) averaging 3 m/s
    assert fe.flown_m == pytest.approx(2 * 20 * 6.0 + 3 * 3 * 3.0, rel=0.01)
    assert sum(fe.phase_wh.values()) == pytest.approx(fe.used_wh, rel=1e-12)
    assert fe.phase_s["level"] >= 30 and fe.phase_s["hover"] >= 18 and fe.phase_s["climb"] > 0
    # in level flight at 6 m/s, 1 kW is 1000 / 6 J per metre: 46.3 Wh per km
    assert fe.cruise_wh_per_km == pytest.approx(1000.0 / 6.0 / 3.6, rel=1e-3)
    m = measured(f)
    assert m["energy.used_wh"] == pytest.approx(fe.used_wh)
    assert m["energy.distance_m"] == round(fe.flown_m)
    assert m["energy.wh_per_km"] == pytest.approx(fe.wh_per_km)
    assert m["energy.cruise_wh_per_km"] == pytest.approx(fe.cruise_wh_per_km)
    flight_ev = [ev for ev in ENERGY.run(f, REG).evidence if ev.id == "energy:flight"]
    assert flight_ev[0].values["flown_m"] == round(fe.flown_m)
    assert isinstance(flight_ev[0].values["flown_m"], int)


def test_a_hover_gives_no_energy_per_km() -> None:
    m = measured(made([(60.0, 0.0, 0.0, 0.0)], amps=20.0))
    assert m["energy.used_wh"] > 0 and m["energy.distance_m"] < 10
    assert "energy.wh_per_km" not in m and "energy.cruise_wh_per_km" not in m


def test_no_battery_monitor_gives_the_sweep_alone() -> None:
    r = ENERGY.run(made(sweep([4.0, 6.0])), REG)
    assert [ev.id for ev in r.evidence] == ["energy:sweep"]
    assert "energy.used_wh" not in {m.metric for m in r.metrics}
