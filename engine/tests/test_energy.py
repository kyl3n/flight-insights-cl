"""The speed sweep, on flights made to a known answer: straight legs flown both ways count
as one speed each, whatever the wind along them; legs flown one way, turning, low or
short do not."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import flight, table

from flight_insights.analyzers.energy import ENERGY, legs, levels, pairs, tiles
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.wind import ekf
from flight_insights.knowledge import load_pack

REG = Registry.load(load_pack())
RULES: dict[str, Any] = dict(REG.rules["energy"]["sweep"])
HZ = 10  # XKF1 as LOG_FILE_RATEMAX 10 logs it
ARM = 1_000_000

Piece = tuple[float, float, float, float]  # seconds, north m/s, east m/s, climb m/s


def made(pieces: Sequence[Piece], height: float = 20.0, ramp: float = 3.0):  # type: ignore[no-untyped-def]
    """A flight from the ground: a climb to `height`, then each piece at a constant ground
    velocity, joined by `ramp` s of straight-line change in velocity (as a turnaround),
    then disarmed. XKF1 from one core at 10 Hz."""
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
    return flight({"XKF1": x1}, ARM, times[-1])


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
    [ev] = ENERGY.run(f, REG).evidence
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
