"""A tlog's clock against the vehicle's (spec: Link and ground-station analysis, "Pairing
and alignment"): the station's clock fitted from the least delay, boots told apart, the
pairing checked against the onboard attitude, packets lost counted, and each flight's
sessions kept and read on its own clock."""

# pymavlink ships without type information.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import shutil
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pymavlink.dialects.v20 import ardupilotmega as m

from flight_insights.analyzers import FlightData
from flight_insights.analyzers.run import run_analysis
from flight_insights.imports import Decision, commit, propose, scan
from flight_insights.imports.commit import Result
from flight_insights.knowledge import Pack, load_pack
from flight_insights.store import Store, StoreKeys, open_store
from flight_insights.store.cache import ColumnCache
from flight_insights.tlog import read_tlog
from flight_insights.tlog.align import BootRecords, boot_utc_us, boots, confirm, fit, losses
from flight_insights.tlog.sessions import cache_key, needs_refresh, sessions

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
STATION_AT_BOOT = 1_700_000_000_000_000 + 3_217_000  # µs: the station's clock at boot 0
DRIFT = 50e-6


def records(
    sent: np.ndarray[Any, Any], delay: np.ndarray[Any, Any], drift: float = DRIFT
) -> BootRecords:
    recorded = STATION_AT_BOOT + (1.0 + drift) * sent + delay
    return BootRecords(1, 1, sent, recorded, (0, len(sent) - 1), [])


def test_the_station_clock_is_recovered_from_the_least_delay() -> None:
    """A planted offset of 3.217 s and drift of 50 ppm, over 15 min of packets at 10 Hz
    with a link delay of at least 8 ms and a 12 s stall that held every packet back."""
    rng = np.random.default_rng(1)
    sent = np.sort(rng.uniform(0, 900e6, 9000))
    delay = 8_000 + rng.exponential(15_000, len(sent))
    delay[(sent > 300e6) & (sent < 312e6)] += 900_000
    clock = fit(records(sent, delay))
    assert clock is not None
    assert abs(clock.offset_us - STATION_AT_BOOT) < 20_000  # within 20 ms: the least delay
    assert abs(clock.drift_ppm - 50.0) < 0.5
    assert clock.stalls >= 1 and clock.windows >= 85
    assert clock.spread_ms < 2.0 and 8.0 < clock.delay_ms[0] < 30.0
    # Mapping back: a packet recorded with the least delay lands on its own sent time.
    assert abs(float(clock.to_boot(clock.to_station(123e6))) - 123e6) < 1.0


def test_a_short_session_fits_its_offset_without_drift() -> None:
    sent = np.linspace(0, 8e6, 40)  # under one window
    clock = fit(records(sent, np.full(len(sent), 5_000.0), drift=0.0))
    assert clock is not None and clock.drift_ppm == 0.0 and clock.windows == 1
    assert abs(clock.offset_us - (STATION_AT_BOOT + 5_000)) < 1.0
    assert fit(records(np.array([1.0]), np.array([0.0]))) is None


# --- tlogs written the way a station records them ----------------------------------------


def write(path: Path, packets: list[tuple[float, tuple[int, int], int, Any]]) -> Path:
    """(station µs, (sysid, compid), sequence number, message) records as a tlog."""
    mav = m.MAVLink(None)
    out = bytearray()
    for t, (sysid, compid), seq, msg in packets:
        mav.srcSystem, mav.srcComponent, mav.seq = sysid, compid, seq % 256
        out += struct.pack(">Q", int(t)) + msg.pack(mav)
    path.write_bytes(bytes(out))
    return path


def vehicle_stream(
    boot_s: float, station_at_boot: float, start_seq: int = 0, seconds: float = 60.0
) -> list[tuple[float, tuple[int, int], int, Any]]:
    """A vehicle's heartbeat, attitude and system time, 10 Hz, from one boot."""
    hb = m.MAVLink_heartbeat_message(
        m.MAV_TYPE_QUADROTOR, m.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0, 0, 3
    )
    out: list[tuple[float, tuple[int, int], int, Any]] = []
    seq = start_seq
    for k in range(int(seconds * 10)):
        boot_ms = int(boot_s * 1000 + k * 100)
        at = station_at_boot + boot_ms * 1000 + 6_000
        msgs: list[Any] = [
            m.MAVLink_attitude_message(boot_ms, 0.1 * np.sin(k / 7), 0.05, 0, 0, 0, 0)
        ]
        if k % 10 == 0:
            msgs += [hb, m.MAVLink_system_time_message(int(1.79e15) + boot_ms * 1000, boot_ms)]
        for msg in msgs:
            out.append((at, (1, 1), seq, msg))
            seq += 1
    return out


def test_lost_and_reordered_packets_are_counted_per_sender(tmp_path: Path) -> None:
    stream = vehicle_stream(0, STATION_AT_BOOT)
    dropped = {5, 6, 7, 100, 400}  # by position in the stream: 5 packets never arrived
    kept = [p for i, p in enumerate(stream) if i not in dropped]
    kept[200], kept[201] = kept[201], kept[200]  # two packets swapped on the way
    gcs = m.MAVLink_heartbeat_message(m.MAV_TYPE_GCS, m.MAV_AUTOPILOT_INVALID, 0, 0, 0, 3)
    kept += [(STATION_AT_BOOT + k * 1e6, (255, 190), k, gcs) for k in range(5)]
    log = read_tlog(write(tmp_path / "a.tlog", kept))
    [vehicle] = [s for s in log.sources if s.sysid == 1]
    station = next(s for s in log.sources if s.sysid == 255)
    # The swap is one packet late, which fills the gap it left: no loss of its own.
    assert vehicle.messages == len(stream) - 5 and vehicle.out_of_order == 1
    assert vehicle.lost == 5
    assert station.lost == 0 and station.out_of_order == 0
    # Within a boot, packets are put back in the order the station recorded them.
    [b] = boots(log.tables, [(1, 1)])
    assert losses(log.tables, 1, 1, b.records) == (len(stream) - 5, 5, 1)


def test_a_reboot_starts_a_new_boot_with_its_own_clock(tmp_path: Path) -> None:
    first = vehicle_stream(0, STATION_AT_BOOT, seconds=30)
    second = vehicle_stream(0, STATION_AT_BOOT + 45e6, start_seq=len(first), seconds=30)
    log = read_tlog(write(tmp_path / "b.tlog", first + second))
    found = boots(log.tables, [(1, 1)])
    assert len(found) == 2
    for b, at in zip(found, (STATION_AT_BOOT, STATION_AT_BOOT + 45e6), strict=True):
        clock = fit(b)
        assert clock is not None and abs(clock.offset_us - (at + 6_000)) < 1.0
        assert boot_utc_us(b) == int(1.79e15)  # each boot's own UTC at its instant
    assert found[0].records[1] < found[1].records[0]


def test_the_pairing_is_confirmed_at_the_lag_the_attitudes_agree() -> None:
    t = np.arange(0, 60e6, 2_500.0)  # onboard: 400 Hz
    roll, pitch = 10 * np.sin(t / 1.3e6), 6 * np.cos(t / 2.1e6)
    ts = np.arange(2e6, 58e6, 250_000.0)  # the station: 4 Hz, its boot times 120 ms late
    late = 120_000.0
    station = (ts, 10 * np.sin((ts - late) / 1.3e6), 6 * np.cos((ts - late) / 2.1e6))
    found = confirm(station, (t, roll, pitch))
    assert found is not None and found.r > 0.99 and abs(found.lag_ms + 120) <= 5
    flat = (ts, np.zeros(len(ts)), np.zeros(len(ts)))
    assert confirm(flat, (t, roll, pitch)) is None  # level throughout: nothing to match


# --- sessions, end to end --------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[Store, Pack, ColumnCache, str]]:
    from test_imports import LAPTOP, airframe

    pack, keys = load_pack(), StoreKeys.generate()
    with open_store(tmp_path / "fleet.db", keys, LAPTOP) as store:
        yield store, pack, ColumnCache(tmp_path / "cache", keys), airframe(store, pack)


def imported(world: tuple[Store, Pack, ColumnCache, str], *paths: Path) -> list[Result]:
    from test_imports import KEY

    store, pack, cache, af = world
    batch = scan(store, pack, KEY, list(paths))
    proposals = propose(store, pack, batch)
    decisions = [Decision(i.id, airframe_id=af if i.kind == "dataflash" else None) for i in batch]
    return commit(store, pack, cache, batch, proposals, decisions)


def copy(tmp_path: Path, name: str) -> tuple[Path, Path]:
    for ext in ("bin", "tlog"):
        shutil.copy(FIXTURES / f"{name}.{ext}", tmp_path / f"{name}.{ext}")
    return tmp_path / f"{name}.bin", tmp_path / f"{name}.tlog"


def test_a_flight_and_its_tlog_are_aligned_and_read_on_the_flight_clock(
    world: tuple[Store, Pack, ColumnCache, str], tmp_path: Path
) -> None:
    store, pack, cache, _ = world
    [onboard, station] = imported(world, *copy(tmp_path, "copter-quad-tune-default"))
    assert (onboard.status, station.status) == ("imported", "linked")
    flight = onboard.flights[0]
    [s] = sessions(store, flight)
    assert s["status"] == "aligned" and s["quality"]["r"] > 0.99
    assert abs(s["quality"]["lag_ms"]) <= 10 and s["lost"] == 0 and s["received"] > 10_000
    # The simulator ran at twice real time: its boot clock gains on the station's.
    assert s["drift_ppm"] == pytest.approx(-500_000, rel=0.01)
    assert s["quality"]["spread_ms"] < 5

    data = FlightData.load(store, cache, pack, flight)
    [(session, attitude)] = data.tlog("ATTITUDE", ["time_boot_ms", "roll"])
    assert session["id"] == s["id"]
    late = (
        np.asarray(attitude.column("TimeUS").to_numpy(), float)
        - np.asarray(attitude.column("time_boot_ms").to_numpy(), float) * 1000
    )
    # Each packet maps to when it was sent plus its delay, which is never negative.
    assert float(np.median(late)) < 10_000 and float(late.min()) > -10_000
    [(_, gps)] = data.tlog("GLOBAL_POSITION_INT")
    assert "lat" not in gps.column_names and "lon" not in gps.column_names  # never positions
    assert gps.num_rows > 0 and "relative_alt" in gps.column_names


def test_a_tlog_imported_before_its_flight_is_aligned_when_the_flight_arrives(
    world: tuple[Store, Pack, ColumnCache, str], tmp_path: Path
) -> None:
    store = world[0]
    bin_path, tlog_path = copy(tmp_path, "heli-hover")
    [station] = imported(world, tlog_path)
    assert station.status == "imported" and not station.flights
    [onboard] = imported(world, bin_path)
    [s] = sessions(store, onboard.flights[0])
    assert s["status"] == "aligned" and s["source_file_id"] == station.source_id


def test_sessions_are_worked_out_again_and_say_when_the_tlog_is_gone(
    world: tuple[Store, Pack, ColumnCache, str], tmp_path: Path
) -> None:
    store, pack, cache, _ = world
    bin_path, tlog_path = copy(tmp_path, "copter-quad-autotune-saved")
    [onboard, station] = imported(world, bin_path, tlog_path)
    flight, source = onboard.flights[0], station.source_id
    assert source is not None

    def forget() -> None:  # as a flight linked before sessions were kept
        with store.transaction():
            store.conn.execute("DELETE FROM tlog_session")
        cache.evict(cache_key(source))

    forget()
    assert needs_refresh(store, flight)
    run_analysis(store, cache, pack, flight, force=True)
    [s] = sessions(store, flight)
    assert s["status"] == "aligned"  # read again from where it was imported
    forget()
    tlog_path.unlink()
    run_analysis(store, cache, pack, flight, force=True)
    [gone] = sessions(store, flight)
    assert gone["status"] == "missing" and gone["offset_us"] is None


def test_a_tlog_from_another_boot_is_not_a_session(
    world: tuple[Store, Pack, ColumnCache, str], tmp_path: Path
) -> None:
    bin_path, _ = copy(tmp_path, "copter-quad-hover")
    _, other = copy(tmp_path, "copter-quad-tune-default")
    [onboard, station] = imported(world, bin_path, other)
    assert station.status == "imported"  # kept for its own flight, not linked here
    assert sessions(world[0], onboard.flights[0]) == []


def test_the_sessions_are_never_synced() -> None:
    from flight_insights.store.synced import LOCAL, SYNCED

    assert "tlog_session" in LOCAL and "tlog_session" not in SYNCED


def test_the_rpc_gives_a_flights_sessions(tmp_path: Path) -> None:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, load_pack())
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys())
    missing = call(server, out, 2, "flight.tlogs", flight_id="no-such-flight")
    assert "no such flight" in missing["error"]["message"]
