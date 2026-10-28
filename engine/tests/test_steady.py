"""Steady tiles and their phases: synthetic estimator data with known answers (each
tile's velocity, acceleration, turn rate, height and tilt; a tile with too few rows left
out), each phase from its rule, and the quad's speed sweep in ArduCopter 4.7.0 SITL read
as climb, hover, level legs and descent."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from test_imports import airframe

from flight_insights.analyzers import FlightData, Registry, steady
from flight_insights.analyzers.wind import ekf
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

RULES: dict[str, Any] = dict(Registry.load(load_pack()).rules["energy"]["phases"])
ARM = 50_000_000
Array = np.ndarray[Any, Any]
Velocity = float | Callable[[Array], Array]  # m/s, constant or of the time since arming
FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"


def flown(seconds: float, vn: Velocity, ve: Velocity = 0.0, vd: Velocity = 0.0,
          height: float = 20.0, roll: float = 0.0, pitch: float = 0.0
          ) -> dict[str, Any]:  # fmt: skip
    """10 Hz from arming."""
    k = np.arange(0, seconds, 0.1)
    e: dict[str, Any] = {"t": (ARM + k * 1e6).astype(np.int64)}
    for name, v in (("VN", vn), ("VE", ve), ("VD", vd)):
        e[name] = np.asarray(v(k) if callable(v) else np.full_like(k, v), np.float64)
    e["PD"] = np.full_like(k, -height)
    e["PD"][0] = 0.0  # on the ground at arming
    e["Roll"], e["Pitch"] = np.full_like(k, roll), np.full_like(k, pitch)
    return e


def speeding_up(k: Array) -> Array:
    return 3.0 + 0.6 * k


def tiles(e: dict[str, Any]) -> steady.Tiles:
    return steady.tiles(e, ARM, int(e["t"][-1]), RULES["tile_s"], RULES["min_samples"])


def test_each_tile_holds_its_mean_velocity_and_slope() -> None:
    e = flown(20.0, vn=lambda k: 2.0 + 0.25 * k, ve=-3.0, vd=0.4, roll=6.0, pitch=8.0)
    t = tiles(e)
    assert len(t) == 9  # 2 s tiles that end by the last row
    assert np.all(np.diff(t.start_us) == 2_000_000) and np.all(t.end_us - t.start_us == 2e6)
    assert t.accel[:, 0] == pytest.approx(np.full(9, 0.25))
    assert t.accel[:, 1:] == pytest.approx(np.zeros((9, 2)), abs=1e-9)
    assert t.velocity[:, 1] == pytest.approx(np.full(9, -3.0))
    assert t.climb == pytest.approx(np.full(9, -0.4))
    assert t.height[1:] == pytest.approx(np.full(8, 20.0))
    assert t.tilt == pytest.approx(np.full(9, math.degrees(
        math.acos(math.cos(math.radians(6.0)) * math.cos(math.radians(8.0))))))  # fmt: skip
    assert t.track[0] == pytest.approx(math.degrees(math.atan2(-3.0, t.velocity[0, 0])) + 360)
    assert np.all(t.lo < t.hi) and int(t.hi[-1]) <= len(e["t"])


def test_a_steady_turn_reads_its_rate() -> None:
    rate = math.radians(12.0)
    e = flown(30.0, vn=lambda k: 6.0 * np.cos(rate * k), ve=lambda k: 6.0 * np.sin(rate * k))
    t = tiles(e)
    assert t.track_rate == pytest.approx(np.full(len(t), 12.0), rel=1e-6)
    assert t.speed == pytest.approx(np.full(len(t), 6.0), rel=0.01)


def test_a_tile_with_too_few_rows_is_left_out() -> None:
    e = flown(10.0, vn=4.0)
    t_us = e["t"]
    keep = (t_us < ARM + 4_000_000) | (t_us >= ARM + 5_500_000)
    t = tiles({k: np.asarray(v)[keep] for k, v in e.items()})
    assert list((t.start_us - ARM) // 1_000_000) == [0, 2, 6]


def test_no_rows_while_armed_gives_no_tiles() -> None:
    e = flown(10.0, vn=4.0)
    assert len(steady.tiles(e, int(e["t"][-1]) + 1, int(e["t"][-1]) + 10**7, 2.0, 10)) == 0


@pytest.mark.parametrize(
    ("piece", "phase"),
    [
        ({"vn": 0.2}, "hover"),
        ({"vn": 6.0}, "level"),
        ({"vn": 6.0, "vd": -1.0}, "climb"),
        ({"vn": 0.0, "vd": 0.8}, "descent"),
        ({"vn": 1.5}, "manoeuvre"),  # between a hover and level speed
        ({"vn": 6.0, "vd": 0.4}, "manoeuvre"),  # sinking, but not a descent
        ({"vn": speeding_up}, "manoeuvre"),
        ({"vn": 6.0, "height": 3.0}, "low"),
    ],
)
def test_each_phase_by_its_rule(piece: dict[str, Any], phase: str) -> None:
    t = tiles(flown(12.0, **piece))
    assert set(steady.phases(t.take(np.arange(1, len(t))), RULES)) == {phase}


def test_a_turn_is_a_manoeuvre_however_steady() -> None:
    rate = math.radians(10.0)
    e = flown(20.0, vn=lambda k: 6.0 * np.cos(rate * k), ve=lambda k: 6.0 * np.sin(rate * k))
    assert set(steady.phases(tiles(e), RULES)[1:]) == {"manoeuvre"}


def test_the_quad_flying_the_sweep_climbs_hovers_flies_level_and_descends(
    tmp_path: Path,
) -> None:
    pack = load_pack()
    keys = StoreKeys.generate()
    store = open_store(tmp_path / "s.db", keys, "e" * 32)
    cache = ColumnCache(tmp_path / "cache", keys)
    batch = scan(store, pack, bytes(32), [FIXTURES / "copter-quad-sweep.bin"])
    af = airframe(store, pack, "Q")
    [res] = commit(store, pack, cache, batch, propose(store, pack, batch),
                   [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
    f = FlightData.load(store, cache, pack, res.flights[0])
    e = ekf(f)
    assert e is not None
    t = steady.tiles(e, f.arm_us, f.disarm_us, RULES["tile_s"], RULES["min_samples"])
    found = steady.phases(t, RULES)
    # take-off low, then a climb to 15 m, the 20 s hover, the legs, and the landing
    first = [str(x) for x in found[:8]]
    assert first[0] == "low" and "climb" in first and found[-1] == "low"
    count = Counter(str(x) for x in found)
    assert count["hover"] * RULES["tile_s"] >= 20 and count["descent"] >= 1
    level = found == "level"
    # the 4, 6 and 8 m/s legs each way (2 m/s, in a gusting 1.5 m/s wind, is not held level)
    speeds = np.round(t.speed[level])
    for v in (4.0, 6.0, 8.0):
        assert np.count_nonzero(np.abs(speeds - v) <= 1.5) >= 8
