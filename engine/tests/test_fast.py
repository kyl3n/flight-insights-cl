"""The fast segment and what is watched in it. On a flight made to a known answer (legs at
6 and 12 m/s both ways in still air), only the 12 m/s legs are
fast, and each watched value reads what was planted there: the lean, the motors' output,
the attitude's error, a dip below the target height, the current and the sag, clipping, a
hot ESC and a thrust-loss warning. On ArduPilot's simulator the heavy lifter's sweep tops
at 12 m/s in still air, and the quad's, holding its ground speed both ways in a 1.5 m/s
wind, flies 9.5 m/s through the air on its upwind legs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import FakeCache, flight, table
from test_energy import ARM, made
from test_imports import airframe

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.fast import FAST
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

REG = Registry.load(load_pack())
FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
DEG = {b"unit": b"deg", b"multiplier": b"1.0"}
M = {b"unit": b"m", b"multiplier": b"1.0"}
V = {b"unit": b"V", b"multiplier": b"1.0"}
A = {b"unit": b"A", b"multiplier": b"1.0"}
C = {b"unit": b"degC", b"multiplier": b"1.0"}
ACC = {b"unit": b"m/s/s", b"multiplier": b"1.0"}
LEGS = [(20.0, 0.0, 0.0, 0.0), (20.0, 0.0, 6.0, 0.0), (20.0, 0.0, -6.0, 0.0),
        (20.0, 0.0, 12.0, 0.0), (20.0, 0.0, -12.0, 0.0), (5.0, 0.0, 0.0, 0.0)]  # fmt: skip
PARAMS = {"BATT_MONITOR": 4.0, "ATC_ANGLE_MAX": 30.0, "MOT_PWM_MIN": 1000.0,
          "MOT_PWM_MAX": 2000.0, "MOT_SPIN_MAX": 0.95,
          **{f"SERVO{k}_FUNCTION": 32.0 + k for k in range(1, 5)}}  # fmt: skip


def planted() -> FlightData:
    """The legs, with each watched value planted as a function of the ground speed."""
    base = made(LEGS)
    tables: dict[str, pa.Table] = dict(cast(FakeCache, base.cache).tables)
    x1 = tables["XKF1"]
    times = np.asarray(x1.column("TimeUS").to_numpy(), np.int64)
    speed = np.hypot(x1.column("VN").to_numpy(), x1.column("VE").to_numpy())
    fast = speed > 11.0
    n = len(times)
    first_fast = int(times[np.flatnonzero(fast)[5]])
    dip = fast & (times > first_fast) & (times < first_fast + 3_000_000)
    amps = 10.0 + 0.5 * speed**2  # 82 A at 12 m/s
    t: list[int] = times.tolist()

    def col(kind: pa.DataType, values: Any) -> tuple[pa.DataType, list[Any]]:
        return kind, np.broadcast_to(np.asarray(values), (n,)).tolist()

    pitch = -2.0 * speed  # 24 deg at 12 m/s: 80% of the 30 deg limit
    tables |= {
        "ATT": table(t, {"DesRoll": col(pa.float32(), 0.0), "Roll": col(pa.float32(), 0.0),
                         "DesPitch": col(pa.float32(), pitch + 0.5),
                         "Pitch": col(pa.float32(), pitch)},
                     {k: DEG for k in ("DesRoll", "Roll", "DesPitch", "Pitch")}),
        # 1780 at 12 m/s
        "RCOU": table(t, {f"C{k}": col(pa.uint16(), (1300 + 40 * speed).astype(int))
                          for k in range(1, 5)}),
        "CTUN": table(t, {"DAlt": col(pa.float32(), 20.0),
                          "Alt": col(pa.float32(), np.where(dip, 18.5, 20.0))},
                      {"DAlt": M, "Alt": M}),
        "BAT": table(t, {"Inst": col(pa.uint8(), 0), "Volt": col(pa.float32(), 50.0 - 0.02 * amps),
                         "VoltR": col(pa.float32(), 50.0), "Curr": col(pa.float32(), amps)},
                     {"Volt": V, "VoltR": V, "Curr": A}),
        # one clip a second while fast
        "VIBE": table(t, {"IMU": col(pa.uint8(), 0), "VibeX": col(pa.float32(), 5.0 + speed),
                          "VibeY": col(pa.float32(), 3.0), "VibeZ": col(pa.float32(), 4.0),
                          "Clip": col(pa.uint32(), np.cumsum(fast & (np.arange(n) % 10 == 0)))},
                      {k: ACC for k in ("VibeX", "VibeY", "VibeZ")}),
        "ESC": table(t, {"Instance": col(pa.uint8(), 0), "Temp": col(pa.float32(), 40.0 + speed)},
                     {"Temp": C}),
        "ERR": table([first_fast], {"Subsys": (pa.uint8(), [25]), "ECode": (pa.uint8(), [1])}),
    }  # fmt: skip
    return flight(tables, ARM, t[-1], params=PARAMS)


def values(r: Any) -> dict[str, float]:
    assert r.skipped is None
    return {m.metric: m.value for m in r.metrics}


def test_only_the_fastest_legs_are_watched_and_each_reads_what_was_planted() -> None:
    f = planted()
    r = FAST.run(f, REG)
    m = values(r)
    [seg] = [e for e in r.evidence if e.id == "fast:segment"]
    assert seg.values["wind_ms"] is None or seg.values["wind_ms"] < 0.1  # still air
    assert m["fast.speed_ms"] == pytest.approx(12.0, abs=0.01)
    assert 30.0 <= m["fast.seconds"] <= 40.0  # the two 20 s legs, less their ramps
    assert m["fast.lean_share"] == pytest.approx(80.0, abs=0.5)
    assert m["fast.output_max"] == pytest.approx(78.0, abs=0.2)  # 1780 of 1000..2000
    assert m["fast.saturation_pct"] == 0.0
    assert m["fast.attitude_error_deg"] == pytest.approx(0.5, abs=1e-3)
    assert m["fast.height_loss_m"] == pytest.approx(1.5, abs=1e-3)
    assert m["fast.current_max_a"] == pytest.approx(82.0, abs=0.1)
    assert m["fast.sag_v"] == pytest.approx(0.02 * 82.0, abs=1e-3)
    assert m["fast.vibe_p95"] == pytest.approx(17.0, abs=0.01)
    assert 25 <= m["fast.clips"] <= 40  # about one a second while fast
    assert m["fast.esc_temp_max_c"] == pytest.approx(52.0, abs=0.01)
    assert m["fast.thrust_loss"] == 1.0
    zones = {x.metric: x.zone for x in r.metrics}
    assert zones["fast.height_loss_m"] == "caution" and zones["fast.thrust_loss"] == "out"
    assert zones["fast.lean_share"] == "caution" and zones["fast.vibe_p95"] == "target"
    # the slow legs and the hover are not read: their lean and current are lower
    assert all(e.segment is not None and e.segment.kind == "fast" for e in r.evidence)


def test_without_its_drag_measured_a_flight_is_read_by_ground_speed() -> None:
    """One speed on one heading cannot tell the drag from the wind."""
    f = made([(40.0, 0.0, 12.0, 0.0)])
    r = FAST.run(f, REG)
    [seg] = [e for e in r.evidence if e.id == "fast:segment"]
    assert seg.values["speed_from"] == "ground" and seg.values["wind_ms"] is None
    assert values(r)["fast.speed_ms"] == pytest.approx(12.0, abs=0.01)


def test_a_slow_or_short_flight_has_no_fast_segment() -> None:
    slow = made([(20.0, 0.0, 0.0, 0.0), (30.0, 0.0, 3.0, 0.0), (5.0, 0.0, 0.0, 0.0)])
    assert FAST.run(slow, REG).skipped is not None
    short = made([(20.0, 0.0, 0.0, 0.0), (6.0, 0.0, 10.0, 0.0), (20.0, 0.0, 4.0, 0.0)])
    assert "s of flight near its fastest" in str(FAST.run(short, REG).skipped)


@pytest.fixture(scope="module")
def sitl(tmp_path_factory: pytest.TempPathFactory) -> dict[str, FlightData]:
    pack = load_pack()
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("fast")
    store = open_store(folder / "fast.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    out: dict[str, FlightData] = {}
    for name in ("copter-heavylift-sweep", "copter-quad-sweep", "copter-quad-hover"):
        batch = scan(store, pack, bytes(32), [FIXTURES / f"{name}.bin"])
        af = airframe(store, pack, name)
        [res] = commit(store, pack, cache, batch, propose(store, pack, batch),
                       [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
        out[name] = FlightData.load(store, cache, pack, res.flights[0])
    return out


def test_the_heavy_lifter_tops_its_sweep_at_12_ms_in_still_air(sitl: dict[str, FlightData]) -> None:
    r = FAST.run(sitl["copter-heavylift-sweep"], REG)
    m = values(r)
    [seg] = [e for e in r.evidence if e.id == "fast:segment"]
    assert seg.values["speed_from"] == "air" and seg.values["wind_ms"] < 0.3
    assert m["fast.speed_ms"] == pytest.approx(12.0, abs=0.3)
    assert m["fast.seconds"] >= 30.0
    assert m["fast.height_loss_m"] < 0.5 and m["fast.thrust_loss"] == 0.0
    assert m["fast.saturation_pct"] == 0.0 and m["fast.lean_share"] < 80.0
    assert m["fast.current_max_a"] > 0 and "fast.esc_temp_max_c" not in m  # simulator


def test_holding_ground_speed_into_the_wind_is_the_fastest_through_the_air(
    sitl: dict[str, FlightData],
) -> None:
    """The quad's card flies 8 m/s over the ground both ways in a 1.5 m/s wind along the
    legs: 9.5 m/s through the air upwind, 6.5 downwind; only the upwind legs are fast."""
    r = FAST.run(sitl["copter-quad-sweep"], REG)
    m = values(r)
    [seg] = [e for e in r.evidence if e.id == "fast:segment"]
    assert seg.values["speed_from"] == "air"
    assert seg.values["wind_ms"] == pytest.approx(1.5, abs=0.2)
    assert m["fast.speed_ms"] == pytest.approx(9.5, abs=0.3)


def test_a_hover_has_no_fast_segment(sitl: dict[str, FlightData]) -> None:
    assert FAST.run(sitl["copter-quad-hover"], REG).skipped is not None
