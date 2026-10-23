"""Ground-station tlogs: decoding, senders, parameter traffic and position tagging."""

# pymavlink ships without type information.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import contextlib
import struct
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pymavlink import mavutil
from pymavlink.dialects.v20 import ardupilotmega as m

from flight_insights.tlog import TlogError, param_downloads, param_writes, read_tlog
from flight_insights.tlog.positions import row_is_position

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
FIXTURE_TLOGS = sorted(FIXTURES.rglob("*.tlog"))
VEHICLE, GCS = (1, 1), (255, 190)
T0 = 1_700_000_000_000_000


def tlog(path: Path, messages: list[tuple[int, tuple[int, int], Any]], junk: bytes = b"") -> Path:
    """Write (offset in µs, (sysid, compid), message) records as a tlog."""
    mav = m.MAVLink(None)
    out = bytearray()
    for i, (dt, (sysid, compid), msg) in enumerate(messages):
        mav.srcSystem, mav.srcComponent, mav.seq = sysid, compid, i % 256
        out += struct.pack(">Q", T0 + dt) + msg.pack(mav)
        if i == 11:  # before the last heartbeat, which it costs; nothing else is lost
            out += junk
    path.write_bytes(bytes(out))
    return path


def heartbeat(gcs: bool) -> Any:
    if gcs:
        return m.MAVLink_heartbeat_message(m.MAV_TYPE_GCS, m.MAV_AUTOPILOT_INVALID, 0, 0, 0, 3)
    return m.MAVLink_heartbeat_message(
        m.MAV_TYPE_QUADROTOR, m.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0, 0, 3
    )


def param_value(name: str, value: float, index: int, count: int = 3) -> Any:
    return m.MAVLink_param_value_message(name.encode(), value, 9, count, index)


def param_set(name: str, value: float) -> Any:
    return m.MAVLink_param_set_message(1, 1, name.encode(), value, 9)


@pytest.fixture
def session(tmp_path: Path) -> Path:
    gwp = m.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT
    return tlog(
        tmp_path / "session.tlog",
        [
            (0, VEHICLE, heartbeat(gcs=False)),
            (10, GCS, heartbeat(gcs=True)),
            # a full download on connection
            (1000, VEHICLE, param_value("ATC_RAT_RLL_P", 0.135, 0)),
            (1100, VEHICLE, param_value("BATT_CAPACITY", 5000, 1)),
            (1200, VEHICLE, param_value("INS_HNTCH_BW", 40, 2)),
            # edits: taken, answered with another value, and never answered
            (2_000_000, GCS, param_set("ATC_RAT_RLL_P", 0.15)),
            (2_050_000, VEHICLE, param_value("ATC_RAT_RLL_P", 0.15, 0)),
            (3_000_000, GCS, param_set("INS_HNTCH_BW", 400)),
            (3_040_000, VEHICLE, param_value("INS_HNTCH_BW", 200, 2)),
            (4_000_000, GCS, param_set("BATT_CAPACITY", 6000)),
            (9_000_000, VEHICLE, m.MAVLink_mission_current_message(3)),
            (
                9_100_000,
                GCS,
                m.MAVLink_mission_item_int_message(
                    1, 1, 0, gwp, m.MAV_CMD_NAV_WAYPOINT, 0, 1, 0, 0, 0, 0, 10, 20, 30
                ),
            ),
            (9_500_000, VEHICLE, heartbeat(gcs=False)),
        ],
        junk=b"\x00\x01\x02 not a packet",
    )


# --- committed fixtures -------------------------------------------------------------


def pymavlink_count(path: Path) -> int:
    conn = mavutil.mavlink_connection(str(path), dialect="ardupilotmega")
    n = 0
    while (msg := conn.recv_msg()) is not None:
        n += msg.get_type() != "BAD_DATA"
    conn.close()
    return n


@pytest.mark.parametrize("path", FIXTURE_TLOGS, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_fixture_tlogs_decode_every_message(path: Path) -> None:
    log = read_tlog(path)
    assert sum(t.num_rows for t in log.tables.values()) == pymavlink_count(path)
    assert log.bad_records == 0
    assert [s.role for s in log.sources] == ["vehicle"]
    assert not log.station_sent()  # the runner records only what the vehicle sent


@pytest.mark.parametrize("path", FIXTURE_TLOGS[:2], ids=lambda p: str(p.relative_to(FIXTURES)))
def test_the_download_on_connection_matches_the_saved_parameter_file(path: Path) -> None:
    [download] = param_downloads(read_tlog(path))
    saved = dict(line.split(",", 1) for line in path.with_suffix(".param").read_text().splitlines())
    assert download.values.keys() == saved.keys()
    for name, value in download.values.items():
        if name.endswith(("_LAT", "_LNG", "_LON")):
            # the scrubber shifted these as text in the file, as float32 in the tlog
            assert value == pytest.approx(float(saved[name]), abs=1e-4)
        else:
            assert float(f"{value:.7g}") == float(saved[name]), name


# --- senders, parameters, positions ----------------------------------------------------


def test_senders_and_their_roles(session: Path) -> None:
    log = read_tlog(session)
    roles = {(s.sysid, s.compid): s.role for s in log.sources}
    assert roles == {VEHICLE: "vehicle", GCS: "ground station"}
    assert log.station_sent()
    assert log.bad_records > 0  # the junk before the last heartbeat


def test_parameter_writes_are_paired_with_the_vehicles_answer(session: Path) -> None:
    writes = {w.name: w for w in param_writes(read_tlog(session))}
    assert writes["ATC_RAT_RLL_P"].result == "applied"
    assert writes["ATC_RAT_RLL_P"].sender == GCS
    assert writes["INS_HNTCH_BW"].result == "different"
    assert writes["INS_HNTCH_BW"].reply_value == 200
    assert writes["BATT_CAPACITY"].result == "no reply"


def test_a_full_download_is_found(session: Path) -> None:
    [download] = param_downloads(read_tlog(session))
    assert download.source == VEHICLE
    assert download.values == pytest.approx(
        {"ATC_RAT_RLL_P": 0.135, "BATT_CAPACITY": 5000, "INS_HNTCH_BW": 40}
    )
    assert download.end_us - download.start_us == 200


def test_tables_keep_record_columns_apart_from_message_fields(session: Path) -> None:
    log = read_tlog(session)
    current = log.tables["MISSION_CURRENT"].to_pylist()[0]
    assert current["seq"] == 3  # the message's own field
    assert current["src_sysid"] == 1 and current["tlog_time_us"] == T0 + 9_000_000


def test_positions_are_tagged_from_the_message_definitions(session: Path) -> None:
    log = read_tlog(session)
    schema = log.tables["MISSION_ITEM_INT"].schema
    meta = {f.name: (f.metadata or {}).get(b"position") for f in schema}
    assert meta["x"] == meta["y"] == b"by_command"
    assert meta["z"] is None


@pytest.mark.parametrize(
    ("message", "row", "expected"),
    [
        ("MISSION_ITEM_INT", {"command": m.MAV_CMD_NAV_WAYPOINT, "frame": 6}, True),
        ("MISSION_ITEM_INT", {"command": m.MAV_CMD_NAV_WAYPOINT, "frame": 1}, False),
        ("MISSION_ITEM_INT", {"command": m.MAV_CMD_DO_CHANGE_SPEED, "frame": 6}, False),
        ("COMMAND_LONG", {"command": m.MAV_CMD_DO_SET_HOME}, True),
        ("COMMAND_LONG", {"command": m.MAV_CMD_COMPONENT_ARM_DISARM}, False),
        ("GLOBAL_POSITION_INT", {}, False),
    ],
)
def test_command_rows_decide_whether_x_and_y_are_a_position(
    message: str, row: dict[str, int], expected: bool
) -> None:
    assert row_is_position(message, row) is expected


def test_every_command_with_a_location_is_a_position_in_a_global_frame() -> None:
    """The definitions' has_location flag decides, as well as the param5/6 labels:
    DO_SET_ROI and NAV_ROI label them only "x" and "y"."""
    located = [k for k, e in m.enums["MAV_CMD"].items() if getattr(e, "has_location", False)]
    assert {m.MAV_CMD_DO_SET_ROI, m.MAV_CMD_NAV_ROI} <= set(located)
    for command in located:
        for message in ("MISSION_ITEM_INT", "MISSION_ITEM", "COMMAND_INT"):
            row = {"command": command, "frame": m.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT}
            assert row_is_position(message, row), (message, command)
        assert row_is_position("COMMAND_LONG", {"command": command}), command


def test_rtcm_corrections_are_tagged_as_positions(tmp_path: Path) -> None:
    """RTCM 1005/1006 carry the base station's position, so the payload is one."""
    path = tlog(
        tmp_path / "rtk.tlog",
        [
            (0, VEHICLE, heartbeat(gcs=False)),
            (10, GCS, m.MAVLink_gps_rtcm_data_message(0, 3, bytes(180))),
            (20, GCS, m.MAVLink_gps_inject_data_message(1, 1, 3, bytes(110))),
        ],
    )
    log = read_tlog(path)
    for name in ("GPS_RTCM_DATA", "GPS_INJECT_DATA"):
        meta = {f.name: (f.metadata or {}).get(b"position") for f in log.tables[name].schema}
        assert meta["data"] == b"1" and meta["len"] is None, name


def test_a_download_needs_every_index_from_zero_to_count(tmp_path: Path) -> None:
    """Three replies to a three-parameter set are not a download if one is index 5."""
    path = tlog(
        tmp_path / "gaps.tlog",
        [
            (0, VEHICLE, heartbeat(gcs=False)),
            (1000, VEHICLE, param_value("ATC_RAT_RLL_P", 0.135, 0)),
            (1100, VEHICLE, param_value("BATT_CAPACITY", 5000, 1)),
            (1200, VEHICLE, param_value("INS_HNTCH_BW", 40, 5)),
        ],
    )
    assert param_downloads(read_tlog(path)) == []


# --- untrusted input ---------------------------------------------------------------------


def test_an_impossible_timestamp_is_kept_as_null(tmp_path: Path) -> None:
    path = tlog(tmp_path / "t.tlog", [(0, VEHICLE, heartbeat(gcs=False))] * 3)
    data = bytearray(path.read_bytes())
    data[0:8] = b"\xff" * 8  # the first record's time: 2**64 - 1 microseconds
    path.write_bytes(bytes(data))
    log = read_tlog(path)
    assert log.bad_times == 1
    assert log.tables["HEARTBEAT"].column("tlog_time_us").to_pylist()[0] is None


REAL = FIXTURE_TLOGS[0].read_bytes()[:30_000]


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(st.tuples(st.integers(0, len(REAL) - 1), st.integers(0, 255)), max_size=40),
    st.integers(0, len(REAL)),
)
def test_damaged_tlogs_never_crash_the_reader(flips: list[tuple[int, int]], cut: int) -> None:
    data = bytearray(REAL)
    for at, value in flips:
        data[at] = value
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fuzz.tlog"
        path.write_bytes(bytes(data[:cut]))
        try:
            log = read_tlog(path)
        except TlogError:
            return  # rejected cleanly
        param_writes(log)
        param_downloads(log)


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.binary(max_size=2048))
def test_random_bytes_never_crash_the_reader(data: bytes) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fuzz.tlog"
        path.write_bytes(data)
        with contextlib.suppress(TlogError):  # rejected cleanly
            read_tlog(path)
