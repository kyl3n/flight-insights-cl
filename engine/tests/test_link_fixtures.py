"""The link analyzer on ArduCopter 4.7.0 SITL flights imported with their tlogs
(tests/fixtures/sitl/4.7.0/copter-quad-fs-*): a clean downlink, the ground station's
silence against FS_GCS_TIMEOUT, and FS_THR_VALUE against the stick's lowest throttle with
the receiver's own failsafe throttle left out."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from test_imports import airframe

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.links import LINKS
from flight_insights.analyzers.model import Result
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
NAMES = ("copter-quad-fs-gcs", "copter-quad-fs-radio", "copter-quad-fs-off")


@pytest.fixture(scope="module")
def judged(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Result, int]]:
    """Each flight's link result, and the lowest throttle its receiver sent at all."""
    pack = load_pack()
    reg = Registry.load(pack)
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("links")
    out: dict[str, tuple[Result, int]] = {}
    with open_store(folder / "links.db", keys, "e" * 32) as store:
        cache = ColumnCache(folder / "cache", keys)
        for name in NAMES:
            paths = [FIXTURES / f"{name}.bin", FIXTURES / f"{name}.tlog"]
            batch = scan(store, pack, bytes(32), paths)
            af = airframe(store, pack, name)
            decisions = [
                Decision(i.id, airframe_id=af if i.kind == "dataflash" else None) for i in batch
            ]
            onboard, station = commit(
                store, pack, cache, batch, propose(store, pack, batch), decisions
            )
            assert (onboard.status, station.status) == ("imported", "linked")
            flight = FlightData.load(store, cache, pack, onboard.flights[0])
            result = LINKS.run(flight, reg)
            assert result.skipped is None
            rcin = flight.table("RCIN")
            assert rcin is not None
            out[name] = (result, int(np.min(rcin.column("C3").to_numpy())))
    return out


def metric(r: Result, name: str) -> tuple[float, str | None] | None:
    found = [m for m in r.metrics if m.metric == name]
    return (found[0].value, found[0].zone) if found else None


@pytest.mark.parametrize("name", NAMES)
def test_the_simulators_downlink_loses_nothing(
    judged: dict[str, tuple[Result, int]], name: str
) -> None:
    r, _ = judged[name]
    assert metric(r, "link.downlink_loss") == (0.0, "target")
    longest = metric(r, "link.downlink_outage")
    assert longest is not None and longest[0] < 0.5 and longest[1] == "target"
    assert not [f for f in r.findings if f.code.startswith("link.downlink")]


def test_the_lost_ground_station_is_silent_past_its_failsafes_wait(
    judged: dict[str, tuple[Result, int]],
) -> None:
    r, _ = judged["copter-quad-fs-gcs"]
    found = metric(r, "link.gcs_silence_share")
    assert found is not None
    share, zone = found
    assert zone == "out" and share > 1  # longer than FS_GCS_TIMEOUT: the failsafe's to judge
    assert "link.gcs_silence" not in {f.code for f in r.findings}


def test_the_receivers_failsafe_throttle_is_not_the_sticks_lowest(
    judged: dict[str, tuple[Result, int]],
) -> None:
    r, lowest_sent = judged["copter-quad-fs-radio"]
    assert lowest_sent < 975  # the receiver's failsafe throttle, flagged in RCI2
    assert metric(r, "link.rc_fs_thr_margin") == (25.0, "target")  # 1000 µs, the stick's own
    assert metric(r, "link.rc_receiver_failsafes") == (0.0, "target")  # SITL sets no flag


def test_no_throttle_margin_with_the_throttle_failsafe_off(
    judged: dict[str, tuple[Result, int]],
) -> None:
    r, _ = judged["copter-quad-fs-off"]
    assert metric(r, "link.rc_fs_thr_margin") is None
