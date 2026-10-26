"""The local track: east, north and up metres from the first fix, read on this laptop
through the positions key, holding no coordinates and refusing to leave the analysis."""

from __future__ import annotations

import copy
import json
import math
import pickle
from pathlib import Path

import numpy as np
import pytest

from flight_insights.analyzers import FlightData
from flight_insights.analyzers.local_track import (
    WGS84_A,
    WGS84_E2,
    LocalTrack,
    enu,
    from_coordinates,
)
from flight_insights.analyzers.model import Evidence
from flight_insights.knowledge import load_pack

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"


def arr(*v: float) -> np.ndarray[tuple[int], np.dtype[np.float64]]:
    return np.array(v, np.float64)


def test_metres_east_north_and_up_on_the_ellipsoid() -> None:
    east, north, up = enu(arr(0.0, 0.0, 0.001, 0.0), arr(10.0, 10.001, 10.0, 10.0),
                          arr(50.0, 50.0, 50.0, 60.0))  # fmt: skip
    # 0.001 degree of longitude on the equator, and of latitude there (meridian radius)
    assert east[1] == pytest.approx(WGS84_A * math.radians(0.001), abs=0.01)
    assert north[2] == pytest.approx(WGS84_A * (1 - WGS84_E2) * math.radians(0.001), abs=0.01)
    assert up[3] == pytest.approx(10.0, abs=1e-6)
    assert abs(north[1]) < 0.01 and abs(east[2]) < 0.01


def test_a_degree_of_longitude_shrinks_with_latitude() -> None:
    east, _, _ = enu(arr(60.0, 60.0), arr(20.0, 20.001), arr(0.0, 0.0))
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(math.radians(60)) ** 2)
    assert east[1] == pytest.approx(n * math.cos(math.radians(60)) * math.radians(0.001), abs=0.01)


def track() -> LocalTrack:
    t = np.array([0, 1, 2, 3], np.int64) * 1_000_000
    lat, lon = arr(0.0, -27.48, -27.4801, -27.4802), arr(0.0, 152.99, 152.99, 152.99)
    found = from_coordinates(t, lat, lon, arr(0.0, 30.0, 31.0, 32.0))
    assert found is not None
    return found


def test_no_position_and_the_first_fix_is_the_origin() -> None:
    t = track()
    assert len(t) == 3  # (0, 0) is "no position" and is left out
    assert t.distance_m()[0] == 0.0 and t.height_m()[0] == 0.0
    assert t.distance_m()[2] == pytest.approx(22.2, abs=0.2)  # 0.0002 degree of latitude
    assert t.height_m().tolist() == pytest.approx([0.0, 1.0, 2.0], abs=1e-3)
    assert from_coordinates(np.array([0]), arr(1.0), arr(2.0), arr(3.0)) is None


def test_the_track_refuses_to_leave_the_analysis() -> None:
    t = track()
    for leave in (pickle.dumps, copy.copy, copy.deepcopy, json.dumps):
        with pytest.raises(TypeError):
            leave(t)
    assert repr(t) == str(t) == "LocalTrack(3 samples)"
    assert not hasattr(t, "__dict__")  # nothing hidden to copy out either


def test_evidence_takes_scalars_never_an_array_or_a_track() -> None:
    Evidence("e", "fact", "ok", values={"distance_m": 12.5, "at_s": [1.0, 2.0]})
    for bad in (np.array([1.0, 2.0]), track()):
        with pytest.raises(TypeError, match="must be a scalar"):
            Evidence("e", "fact", "no", values={"x": bad})


def test_a_sitl_flight_read_through_the_positions_key(tmp_path: Path) -> None:
    from test_imports import airframe

    from flight_insights.imports.commit import Decision, commit
    from flight_insights.imports.propose import propose
    from flight_insights.imports.scan import scan
    from flight_insights.store.cache import ColumnCache
    from flight_insights.store.db import open_store
    from flight_insights.store.keys import StoreKeys

    pack, keys = load_pack(), StoreKeys.generate()
    with open_store(tmp_path / "t.db", keys, "e" * 32) as store:
        cache = ColumnCache(tmp_path / "cache", keys)
        batch = scan(store, pack, bytes(32), [FIXTURES / "copter-quad-hover.bin"])
        af = airframe(store, pack)
        [res] = commit(store, pack, cache, batch, propose(store, pack, batch),
                       [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
        flight = FlightData.load(store, cache, pack, res.flights[0])
        found = flight.local_track()
        assert isinstance(found, LocalTrack) and len(found) > 100
        armed = (found.times_us >= flight.arm_us) & (found.times_us <= flight.disarm_us)
        assert 8.0 < float(np.max(found.height_m()[armed])) < 12.0  # the 10 m hover
        assert float(np.max(found.distance_m()[armed])) < 5.0  # held over its take-off point
        pos = flight.table("POS")
        assert pos is not None and "Lat" not in pos.column_names  # analysis reads never see it
