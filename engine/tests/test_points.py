"""The power model's points: on estimator data made to a known answer, the wind comes off
the ground velocity, tiles are averaged per phase and speed, and tiles in a landing, just
after a mode change or with power wandering are left out; in ArduPilot 4.7.0 SITL, a flight
in a 5 m/s wind reads its hover as 5 m/s of airspeed, with the simulator's air and the
weight as weighed at import."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from test_imports import airframe

from flight_insights.analyzers import Registry, run_analysis
from flight_insights.analyzers.battery_power import Supply
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.performance.points import FlightPoints, flight_points, smoothed, tile_points
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

REG = Registry.load(load_pack())
RULES: dict[str, Any] = dict(REG.rules["performance"]["points"])
PHASES: dict[str, Any] = dict(REG.rules["energy"]["phases"])
FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
ARM = 10_000_000
Array = np.ndarray[Any, Any]
Piece = tuple[float, float, float, float]  # seconds, north m/s, east m/s, watts


def made(pieces: list[Piece], height: float = 20.0) -> tuple[dict[str, Array], Supply, int]:
    """10 Hz from arming, already at `height`: each piece a constant ground velocity and
    power, the packs' power noisy by `noise` of itself."""
    t: list[float] = []
    vn: list[float] = []
    ve: list[float] = []
    watts: list[float] = []
    now = 0.0
    for seconds, n, e, p in pieces:
        for _ in range(round(seconds * 10)):
            t.append(now)
            vn.append(n)
            ve.append(e)
            watts.append(p)
            now += 0.1
    times = (ARM + np.array(t) * 1e6).astype(np.int64)
    zeros = np.zeros(len(times))
    pd = np.full(len(times), -height)
    pd[0] = 0.0  # on the ground at arming
    e = {"t": times, "VN": np.array(vn), "VE": np.array(ve), "VD": zeros, "PD": pd,
         "Roll": zeros, "Pitch": zeros}  # fmt: skip
    power = np.array(watts)
    packs = Supply(times, np.full(len(times), 50.0), power / 50.0, (power / 50.0)[None, :],
                   power, (0,), np.array([np.nan]), "packs")  # fmt: skip
    return e, packs, int(times[-1])


def constant(rho: float = 1.2) -> Callable[[Array, Array], Array]:
    return lambda a, b: np.full(len(a), rho)


def blowing(north: float, east: float) -> Callable[[Array], Array]:
    """A wind going (north, east) m/s, the whole flight."""
    return lambda centres: np.tile([north, east], (len(centres), 1))


def points(pieces: list[Piece], wind: Callable[[Array], Array] | None = None,
           modes: list[tuple[int, int, str]] | None = None, **kw: Any) -> Any:  # fmt: skip
    e, packs, end = made(pieces, **kw)
    return tile_points(e, packs, constant(), wind or blowing(0, 0), ARM, end, modes or [],
                       RULES, PHASES)  # fmt: skip


def test_the_wind_comes_off_the_ground_velocity() -> None:
    """Air going east at 2 m/s: 6 m/s east over the ground is 4 m/s through the air, 6 m/s
    west is 8, and a hover is 2."""
    found, left = points([(20, 0, 0, 900), (20, 0, 6, 1000), (20, 0, -6, 1100)],
                         wind=blowing(0, 2.0))  # fmt: skip
    assert not left
    by = {(p.phase, round(p.airspeed)): p for p in found}
    assert by[("hover", 2)].power == pytest.approx(900)
    assert by[("level", 4)].power == pytest.approx(1000)
    assert by[("level", 8)].power == pytest.approx(1100)
    assert sum(p.seconds for p in found) == pytest.approx(58.0)  # 29 whole 2 s tiles


def test_tiles_average_per_phase_and_airspeed_bin_weighted_by_time() -> None:
    found, _ = points([(20, 0, 5.8, 1000), (40, 0, 6.2, 1300)])
    [level] = [p for p in found if p.phase == "level"]
    assert level.tiles == 29
    assert level.airspeed == pytest.approx((9 * 5.8 + 20 * 6.2) / 29, abs=0.02)
    assert level.power == pytest.approx((9 * 1000 + 20 * 1300) / 29, rel=0.01)


def test_a_landing_and_the_settling_after_a_mode_change_are_left_out() -> None:
    switch = ARM + 20_000_000
    land = ARM + 40_000_000
    found, left = points([(60, 0, 0, 900)],
                         modes=[(ARM - 5_000_000, switch, "GUIDED"), (switch, land, "LOITER"),
                                (land, ARM + 61_000_000, "LAND")])  # fmt: skip
    assert left["mode"] == pytest.approx(18.0)  # the tiles from 40 s to the last whole one
    assert left["mode change"] == pytest.approx(4.0)  # the two tiles starting in its 3 s
    assert sum(p.seconds for p in found) == pytest.approx(58.0 - 22.0)


def test_power_that_wanders_is_left_out() -> None:
    e, packs, end = made([(30, 0, 6, 1000)])
    k = np.arange(len(packs.power))
    wild = np.where((k // 10) % 2 == 0, 600.0, 1400.0)  # a 1 s swing, 40% of its mean
    packs = Supply(packs.t, packs.volt, wild / 50, (wild / 50)[None, :], wild, (0,),
                   packs.resistance, "packs")  # fmt: skip
    found, left = tile_points(e, packs, constant(), blowing(0, 0), ARM, end, [], RULES, PHASES)
    assert found == [] and left["power not steady"] == pytest.approx(28.0)


def test_the_wind_is_a_running_median_of_the_windows() -> None:
    t = np.arange(0, 120, 2.0) * 1e6
    winds = np.tile([0.0, 3.0], (len(t), 1))
    winds[10] = [0.0, 30.0]  # one window off
    winds[40:] = [0.0, -3.0]  # the wind turns at 80 s
    at = smoothed(t.astype(np.int64), winds, 30.0)
    got = at(np.array([20e6, 50e6, 110e6]))
    assert got[:, 1] == pytest.approx([3.0, 3.0, -3.0])


@pytest.fixture(scope="module")
def windy(tmp_path_factory: pytest.TempPathFactory) -> FlightPoints:
    pack = load_pack()
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("points")
    store = open_store(folder / "p.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    batch = scan(store, pack, bytes(32), [FIXTURES / "copter-quad-wind.bin"])
    af = airframe(store, pack, "W")
    [res] = commit(store, pack, cache, batch, propose(store, pack, batch),
                   [Decision(batch[0].id, airframe_id=af, takeoff_kg=1.6)])  # fmt: skip
    run_analysis(store, cache, pack, res.flights[0])
    got = flight_points(store, cache, pack, res.flights[0])
    assert not isinstance(got, str), got
    return got


def test_the_simulators_wind_comes_off_its_hover(windy: FlightPoints) -> None:
    """SITL blows 5 m/s from 270: holding still, the quad flies at 5 m/s through the air."""
    assert windy.wind_from == "flight"
    hover = [p for p in windy.points if p.phase == "hover"]
    seconds = sum(p.seconds for p in hover)
    airspeed = sum(p.airspeed * p.seconds for p in hover) / seconds
    assert seconds >= 40 and airspeed == pytest.approx(5.0, abs=0.3)


def test_the_simulators_points_span_its_legs_in_its_own_air(windy: FlightPoints) -> None:
    level = [p for p in windy.points if p.phase == "level"]
    assert max(p.airspeed for p in level) - min(p.airspeed for p in level) >= 8.0
    # the simulator's standard atmosphere at its barometer: its home is above the sea
    assert all(1.10 < p.density < 1.225 for p in windy.points)
    assert windy.mass_kg == 1.6 and windy.mass_from == "weighed at import"
    assert windy.left_out.get("mode", 0) > 0  # its landing
