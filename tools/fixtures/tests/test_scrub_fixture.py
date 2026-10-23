"""Tests for the fixture scrubber, on logs built here so no simulator is needed."""

import hashlib
import struct
import sys
from pathlib import Path

import pytest
from pymavlink import DFReader, mavutil
from pymavlink.dialects.v20 import ardupilotmega as mavlink

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scrub_fixture as sf

HOME_LAT, HOME_LNG = -35.3632621, 149.1652374
SHIFT = sf.Shift(lat=21.5, lng=-73.25)


# --- a tiny DataFlash log -----------------------------------------------------

DF_SIZES = {"Q": 8, "b": 1, "B": 1, "Z": 64, "N": 16, "n": 4, "L": 4, "i": 4, "f": 4, "d": 8}
DF_PACK = {"Q": "Q", "b": "b", "B": "B", "Z": "64s", "N": "16s", "n": "4s", "L": "i", "i": "i"}
DF_PACK |= {"f": "f", "d": "d"}


class DfLog:
    def __init__(self) -> None:
        self.data = bytearray()
        self.formats: dict[str, tuple[int, str]] = {}
        self._fmt(128, "FMT", "BBnNZ", "Type,Length,Name,Format,Columns")
        self._fmt(129, "UNIT", "QbZ", "TimeUS,Id,Label")
        self._fmt(130, "MULT", "Qbd", "TimeUS,Id,Mult")
        self._fmt(131, "FMTU", "QBNN", "TimeUS,FmtType,UnitIds,MultIds")
        for uid, label in [("s", "s"), ("D", "deglatitude"), ("U", "deglongitude"), ("m", "m")]:
            self.write("UNIT", 0, ord(uid), label.encode())
        for mid, mult in [("-", 0.0), ("F", 1e-6), ("G", 1e-7), ("0", 1.0)]:
            self.write("MULT", 0, ord(mid), mult)

    def _fmt(self, type_: int, name: str, fmt: str, columns: str) -> None:
        length = 3 + sum(DF_SIZES[c] for c in fmt)
        self.formats[name] = (type_, fmt)
        payload = struct.pack(
            "<BB4s16s64s", type_, length, name.encode(), fmt.encode(), columns.encode()
        )
        self.data += b"\xa3\x95\x80" + payload

    def define(self, type_: int, name: str, fmt: str, columns: str, units: str, mults: str):
        self._fmt(type_, name, fmt, columns)
        self.write("FMTU", 0, type_, units.encode(), mults.encode())

    def write(self, name: str, *values: object) -> None:
        type_, fmt = self.formats[name]
        packed = struct.pack("<" + "".join(DF_PACK[c] for c in fmt), *values)
        self.data += b"\xa3\x95" + bytes([type_]) + packed


def e7(deg: float) -> int:
    return round(deg * 1e7)


@pytest.fixture
def bin_log(tmp_path: Path) -> Path:
    log = DfLog()
    log.define(140, "GPS", "QLLf", "TimeUS,Lat,Lng,Alt", "sDUm", "FGG0")
    log.define(141, "QQQQ", "Qffd", "TimeUS,A,B,Lat", "sDUm", "F---")  # names say nothing
    log.define(142, "INTP", "Qii", "TimeUS,X,Y", "sDU", "FGG")  # plain ints with a multiplier
    log.define(143, "PARM", "QNff", "TimeUS,Name,Value,Default", "s---", "F---")
    log.define(144, "MSG", "QZ", "TimeUS,Message", "s-", "F-")
    log.write("MSG", 1, b"CubeOrange 0040002D 3131510B 34393338")
    log.write("MSG", 2, b"ArduCopter V4.7.0 (1a2b3c4d)")
    log.write("PARM", 3, b"SIM_OPOS_LAT", HOME_LAT, HOME_LAT)
    log.write("PARM", 4, b"SIM_OPOS_LNG", HOME_LNG, HOME_LNG)
    log.write("PARM", 5, b"COMPASS_DEC", 0.2, 0.0)
    log.write("PARM", 6, b"ATC_RAT_RLL_P", 0.135, 0.135)
    log.write("GPS", 10, 0, 0, 0.0)  # no fix yet
    for i in range(5):
        log.write("GPS", 20 + i, e7(HOME_LAT + i * 1e-5), e7(HOME_LNG + i * 1e-5), 584.0 + i)
    log.write("QQQQ", 30, HOME_LAT, HOME_LNG, 12.5)
    log.write("INTP", 40, e7(HOME_LAT), e7(HOME_LNG))
    path = tmp_path / "in" / "flight.bin"
    path.parent.mkdir()
    path.write_bytes(bytes(log.data))
    return path


def read_df(path: Path) -> list[dict[str, object]]:
    reader = DFReader.DFReader_binary(str(path), zero_time_base=False)
    out = []
    while (m := reader.recv_msg()) is not None:
        out.append({"type": m.get_type()} | {f: getattr(m, f) for f in m.get_fieldnames()})
    return out


def test_bin_positions_move_by_the_shift_and_nothing_else_changes(
    bin_log: Path, tmp_path: Path
) -> None:
    [out] = sf.scrub([bin_log], tmp_path / "out", SHIFT)
    before, after = read_df(bin_log), read_df(out)
    assert [m["type"] for m in before] == [m["type"] for m in after]

    gps = [(a, b) for a, b in zip(before, after, strict=True) if a["type"] == "GPS"]
    assert gps[0][1]["Lat"] == 0 and gps[0][1]["Lng"] == 0  # "no position" stays zero
    for a, b in gps[1:]:
        assert b["Lat"] == pytest.approx(a["Lat"] + SHIFT.lat, abs=2e-7)
        assert b["Lng"] == pytest.approx(SHIFT.apply("lng", a["Lng"]), abs=2e-7)
        assert b["Alt"] == a["Alt"]

    parm = {m["Name"]: m for m in after if m["type"] == "PARM"}
    assert parm["SIM_OPOS_LAT"]["Value"] == pytest.approx(HOME_LAT + SHIFT.lat, abs=1e-5)
    assert parm["SIM_OPOS_LNG"]["Default"] == pytest.approx(HOME_LNG + SHIFT.lng, abs=1e-5)
    assert parm["COMPASS_DEC"]["Value"] == 0
    assert parm["ATC_RAT_RLL_P"]["Value"] == pytest.approx(0.135)

    msgs = [m["Message"] for m in after if m["type"] == "MSG"]
    assert msgs == ["CubeOrange 00000000 00000000 00000000", "ArduCopter V4.7.0 (1a2b3c4d)"]


def test_units_drive_the_scrub_not_names(bin_log: Path, tmp_path: Path) -> None:
    [out] = sf.scrub([bin_log], tmp_path / "out", SHIFT)
    [q] = [m for m in read_df(out) if m["type"] == "QQQQ"]
    [i] = [m for m in read_df(out) if m["type"] == "INTP"]
    assert q["A"] == pytest.approx(HOME_LAT + SHIFT.lat, abs=1e-5)  # unit deglatitude, any name
    assert q["B"] == pytest.approx(HOME_LNG + SHIFT.lng, abs=1e-5)
    assert q["Lat"] == 12.5  # named Lat, but its unit is metres
    assert i["X"] == e7(HOME_LAT + SHIFT.lat)
    assert i["Y"] == e7(HOME_LNG + SHIFT.lng)


def test_no_original_position_survives_in_any_encoding(bin_log: Path, tmp_path: Path) -> None:
    sentinels = sf.plan_file(bin_log).sentinels()
    assert sf.find_leaks(bin_log.read_bytes(), sentinels) > 0  # the search does find them
    [out] = sf.scrub([bin_log], tmp_path / "out", SHIFT)
    assert sf.find_leaks(out.read_bytes(), sentinels) == 0


def test_random_shift_is_used_when_none_given(bin_log: Path, tmp_path: Path) -> None:
    [out] = sf.scrub([bin_log], tmp_path / "out")
    [gps] = [m for m in read_df(out) if m["type"] == "GPS" and m["Lat"]][:1]
    assert abs(gps["Lat"] - HOME_LAT) >= sf.MIN_SHIFT_DEG


def test_position_hidden_in_text_fails_and_writes_nothing(tmp_path: Path) -> None:
    log = DfLog()
    log.define(140, "GPS", "QLL", "TimeUS,Lat,Lng", "sDU", "FGG")
    log.define(144, "MSG", "QZ", "TimeUS,Message", "s-", "F-")
    log.write("GPS", 1, e7(HOME_LAT), e7(HOME_LNG))
    log.write("MSG", 2, f"home {HOME_LAT:.7f}".encode())
    path = tmp_path / "leaky.bin"
    path.write_bytes(bytes(log.data))
    with pytest.raises(sf.ScrubError, match="original position"):
        sf.scrub([path], tmp_path / "out", SHIFT)
    assert not (tmp_path / "out" / "leaky.bin").exists()


def test_format_l_without_a_geographic_unit_is_refused(tmp_path: Path) -> None:
    log = DfLog()
    log.define(140, "ODD", "QL", "TimeUS,Thing", "sm", "F0")
    log.write("ODD", 1, 5)
    path = tmp_path / "odd.bin"
    path.write_bytes(bytes(log.data))
    with pytest.raises(sf.ScrubError, match="format L"):
        sf.scrub([path], tmp_path / "out", SHIFT)


def test_junk_bytes_are_zeroed_and_skipped(bin_log: Path, tmp_path: Path) -> None:
    data = bin_log.read_bytes()
    cut = data.index(b"\xa3\x95\x8c")  # first GPS message
    bin_log.write_bytes(data[:cut] + b"\x00\x13\x37" + data[cut:])
    [out] = sf.scrub([bin_log], tmp_path / "out", SHIFT)
    assert sf.plan_file(out).skipped_bytes == 3
    assert out.read_bytes()[cut : cut + 3] == bytes(3)


PLANT_LAT, PLANT_LNG = HOME_LAT + 0.25, HOME_LNG - 0.25  # in no message pymavlink can decode


def assert_no_trace_of_plant(src: Path, out: Path) -> None:
    planted = [PLANT_LAT, PLANT_LNG]
    assert sf.find_leaks(src.read_bytes(), planted) > 0  # the search does find them
    assert sf.find_leaks(out.read_bytes(), planted) == 0
    assert len(out.read_bytes()) == len(src.read_bytes())
    assert [m["type"] for m in read_df(src)] == [m["type"] for m in read_df(out)]


def test_a_gps_message_cut_off_at_the_end_is_blanked(bin_log: Path, tmp_path: Path) -> None:
    log = DfLog()
    log.define(140, "GPS", "QLLf", "TimeUS,Lat,Lng,Alt", "sDUm", "FGG0")
    log.write("GPS", 1, e7(HOME_LAT), e7(HOME_LNG), 584.0)
    log.write("GPS", 2, e7(PLANT_LAT), e7(PLANT_LNG), 590.0)
    path = tmp_path / "cut.bin"
    path.write_bytes(bytes(log.data[:-2]))  # the last message loses part of its Alt
    [out] = sf.scrub([path], tmp_path / "out", SHIFT)
    assert sf.plan_file(path).skipped_bytes == 3 + 8 + 4 + 4 + 2
    assert_no_trace_of_plant(path, out)
    assert out.read_bytes().endswith(bytes(3 + 8 + 4 + 4 + 2))


def test_junk_between_messages_with_a_position_in_it_is_blanked(
    bin_log: Path, tmp_path: Path
) -> None:
    data = bin_log.read_bytes()
    cut = data.index(b"\xa3\x95\x8d")  # the QQQQ message, after the GPS samples
    junk = (
        b"\x00"
        + struct.pack("<ii", e7(PLANT_LAT), e7(PLANT_LNG))
        + struct.pack("<fd", PLANT_LAT, PLANT_LNG)
        + f"{PLANT_LAT:.7f},{PLANT_LNG:.7f}".encode()
    )
    bin_log.write_bytes(data[:cut] + junk + data[cut:])
    [out] = sf.scrub([bin_log], tmp_path / "out", SHIFT)
    assert sf.plan_file(out).skipped_bytes == len(junk)
    assert_no_trace_of_plant(bin_log, out)


def test_every_position_sample_is_searched_not_just_the_first(tmp_path: Path) -> None:
    log = DfLog()
    log.define(140, "GPS", "QLL", "TimeUS,Lat,Lng", "sDU", "FGG")
    log.define(144, "MSG", "QZ", "TimeUS,Message", "s-", "F-")
    log.write("GPS", 1, e7(HOME_LAT), e7(HOME_LNG))
    log.write("GPS", 2, e7(PLANT_LAT), e7(PLANT_LNG))
    log.write("MSG", 3, f"land {PLANT_LAT:.7f}".encode())  # the second sample, as text
    path = tmp_path / "leaky.bin"
    path.write_bytes(bytes(log.data))
    with pytest.raises(sf.ScrubError, match="original position"):
        sf.scrub([path], tmp_path / "out", SHIFT)


def test_leak_search_matches_a_plain_substring_search() -> None:
    degs = [HOME_LAT, HOME_LNG, PLANT_LAT, -7.5, 0.123456]
    blob = b"xx" + b"".join(sf.leak_patterns(PLANT_LAT)) + b"\x01" + sf.leak_patterns(-7.5)[0]
    for offset in range(9):  # every alignment
        data = bytes(offset) + blob + b"-149.16"
        expected = len({pat for d in degs for pat in sf.leak_patterns(d) if pat in data})
        assert sf.find_leaks(data, degs) == expected > 0


def test_a_lone_four_byte_match_is_chance_but_a_position_is_a_leak() -> None:
    """Among millions of 4-byte windows a lone int32 or float32 turns up by chance;
    a latitude beside a longitude, a double or decimal text does not."""
    lat, lng = e7(PLANT_LAT), e7(PLANT_LNG)
    lone = b"\x07" * 10 + struct.pack("<i", lat) + b"\x07" * 100 + struct.pack("<f", PLANT_LNG)
    assert sf.find_position_leaks(lone, [PLANT_LAT], [PLANT_LNG]) == 0
    for leak in (
        struct.pack("<iI", lat, 7) + struct.pack("<i", lng),  # stored as a position is
        struct.pack("<ff", PLANT_LNG, PLANT_LAT),  # either order
        struct.pack("<d", PLANT_LAT),  # eight exact bytes
        f"at {PLANT_LNG:.7f}".encode(),  # text
    ):
        for offset in range(5):
            data = bytes(offset) + b"\x07" * 40 + leak + b"\x07" * 40
            assert sf.find_position_leaks(data, [PLANT_LAT], [PLANT_LNG]) > 0, leak


# --- tlogs ----------------------------------------------------------------------


def tlog_bytes(messages: list[tuple[mavlink.MAVLink_message, int]]) -> bytes:
    mav = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
    out = bytearray()
    for i, (msg, sysid) in enumerate(messages):
        mav.srcSystem = sysid
        mav.seq = i % 256
        out += struct.pack(">Q", 1_700_000_000_000_000 + i * 1000) + msg.pack(mav)
    return bytes(out)


@pytest.fixture
def tlog(tmp_path: Path) -> Path:
    m = mavlink
    lat, lng = e7(HOME_LAT), e7(HOME_LNG)
    global_frame = m.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT
    msgs = [
        (m.MAVLink_heartbeat_message(2, 3, 0, 0, 0, 3), 1),
        (m.MAVLink_gps_raw_int_message(1, 0, 0, 0, 0, 65535, 65535, 0, 65535, 0), 1),
        (m.MAVLink_gps_raw_int_message(2, 3, lat, lng, 584000, 100, 100, 0, 0, 10), 1),
        (m.MAVLink_global_position_int_message(3, lat, lng, 584000, 0, 0, 0, 0, 35300), 1),
        (m.MAVLink_home_position_message(lat, lng, 584000, 0, 0, 0, [1, 0, 0, 0], 0, 0, 0), 1),
        (m.MAVLink_param_value_message(b"SIM_OPOS_LAT", HOME_LAT, 9, 1000, 1), 1),
        (m.MAVLink_param_value_message(b"COMPASS_DEC", 0.2, 9, 1000, 2), 1),
        (m.MAVLink_param_value_message(b"ATC_RAT_RLL_P", 0.135, 9, 1000, 3), 1),
        (m.MAVLink_statustext_message(6, b"CubeOrange 0040002D 3131510B 34393338"), 1),
        (m.MAVLink_autopilot_version_message(0, 0, 0, 0, 0, *[[0] * 8] * 3, 0, 0, 77, [9] * 18), 1),
        (
            m.MAVLink_mission_item_int_message(
                1, 1, 1, global_frame, m.MAV_CMD_NAV_WAYPOINT, 0, 1, 0, 0, 0, 0, lat, lng, 20
            ),
            255,
        ),
        (
            m.MAVLink_mission_item_int_message(
                1, 1, 2, m.MAV_FRAME_LOCAL_NED, m.MAV_CMD_NAV_WAYPOINT, 0, 1, 0, 0, 0, 0, 5, 7, 2
            ),
            255,
        ),
        (
            m.MAVLink_command_long_message(
                1, 1, m.MAV_CMD_DO_SET_HOME, 0, 0, 0, 0, 0, HOME_LAT, HOME_LNG, 584
            ),
            255,
        ),
    ]
    path = tmp_path / "tlog-in" / "flight.tlog"
    path.parent.mkdir()
    path.write_bytes(tlog_bytes(msgs))
    return path


def read_tlog(path: Path) -> list[mavlink.MAVLink_message]:
    conn = mavutil.mavlink_connection(str(path), dialect="ardupilotmega")
    out = []
    while (msg := conn.recv_match()) is not None:
        out.append(msg)
    return out


def test_tlog_positions_move_and_everything_else_survives(tlog: Path, tmp_path: Path) -> None:
    [out] = sf.scrub([tlog], tmp_path / "out", SHIFT)
    before, after = read_tlog(tlog), read_tlog(out)
    assert [m.get_type() for m in before] == [m.get_type() for m in after]
    assert [m._timestamp for m in before] == [m._timestamp for m in after]
    assert [m.get_srcSystem() for m in before] == [m.get_srcSystem() for m in after]
    assert [m.get_seq() for m in before] == [m.get_seq() for m in after]

    by_type: dict[str, list[mavlink.MAVLink_message]] = {}
    for msg in after:
        by_type.setdefault(msg.get_type(), []).append(msg)
    no_fix, fix = by_type["GPS_RAW_INT"]
    assert (no_fix.lat, no_fix.lon) == (0, 0)
    assert fix.lat == e7(HOME_LAT + SHIFT.lat)
    assert fix.lon == e7(SHIFT.apply("lng", HOME_LNG))
    assert by_type["GLOBAL_POSITION_INT"][0].lat == e7(HOME_LAT + SHIFT.lat)
    assert by_type["HOME_POSITION"][0].longitude == e7(HOME_LNG + SHIFT.lng)

    params = {p.param_id: p.param_value for p in by_type["PARAM_VALUE"]}
    assert params["SIM_OPOS_LAT"] == pytest.approx(HOME_LAT + SHIFT.lat, abs=1e-5)
    assert params["COMPASS_DEC"] == 0
    assert params["ATC_RAT_RLL_P"] == pytest.approx(0.135)

    assert by_type["STATUSTEXT"][0].text == "CubeOrange 00000000 00000000 00000000"
    version = by_type["AUTOPILOT_VERSION"][0]
    assert version.uid == 0 and list(version.uid2) == [0] * 18

    global_item, local_item = by_type["MISSION_ITEM_INT"]
    assert global_item.x == e7(HOME_LAT + SHIFT.lat)
    assert (local_item.x, local_item.y) == (5, 7)  # local frame: metres, not a position
    cmd = by_type["COMMAND_LONG"][0]
    assert cmd.param5 == pytest.approx(HOME_LAT + SHIFT.lat, abs=1e-5)
    assert cmd.param7 == 584

    sentinels = sf.plan_file(tlog).sentinels()
    assert sf.find_leaks(out.read_bytes(), sentinels) == 0


# --- parameter files -------------------------------------------------------------


def test_param_files_keep_everything_but_positions(tmp_path: Path) -> None:
    mp = tmp_path / "in" / "copter.param"
    qgc = tmp_path / "in" / "copter.params"
    mp.parent.mkdir()
    mp.write_text(
        "# saved by the test\n"
        "ATC_RAT_RLL_P,0.135\n"
        f"SIM_OPOS_LAT,{HOME_LAT}\n"
        f"SIM_OPOS_LNG {HOME_LNG}\n"
        "COMPASS_DEC,0.2\n"
    )
    qgc.write_text(
        "# Vehicle-Id Component-Id Name Value Type\n"
        "1\t1\tATC_RAT_RLL_P\t0.135\t9\n"
        f"1\t1\tSIM_OPOS_LAT\t{HOME_LAT}\t9\n"
        f"1\t1\tSIM_OPOS_LNG\t{HOME_LNG}\t9\n"
    )
    out_mp, out_qgc = sf.scrub([mp, qgc], tmp_path / "out", SHIFT)
    lines = out_mp.read_text().splitlines()
    assert lines[0] == "# saved by the test"
    assert lines[1] == "ATC_RAT_RLL_P,0.135"
    assert lines[2] == f"SIM_OPOS_LAT,{sf.format_param(HOME_LAT + SHIFT.lat)}"
    assert lines[3].startswith("SIM_OPOS_LNG ")
    assert lines[4] == "COMPASS_DEC,0"
    qlines = out_qgc.read_text().splitlines()
    assert qlines[1] == "1\t1\tATC_RAT_RLL_P\t0.135\t9"
    assert qlines[3] == f"1\t1\tSIM_OPOS_LNG\t{sf.format_param(HOME_LNG + SHIFT.lng)}\t9"
    for out in (out_mp, out_qgc):
        assert sf.find_leaks(out.read_bytes(), [HOME_LAT, HOME_LNG]) == 0


def test_one_shift_is_shared_by_a_session(bin_log: Path, tlog: Path, tmp_path: Path) -> None:
    out_bin, out_tlog = sf.scrub([bin_log, tlog], tmp_path / "out")
    gps_bin = next(m for m in read_df(out_bin) if m["type"] == "GPS" and m["Lat"])
    gps_tlog = next(m for m in read_tlog(out_tlog) if m.get_type() == "GPS_RAW_INT" and m.lat)
    assert gps_bin["Lat"] == pytest.approx(gps_tlog.lat * 1e-7, abs=2e-7)


# --- shift and manifest ----------------------------------------------------------


@pytest.mark.parametrize("lats", [[HOME_LAT], [-70.0, 70.0], [60.0, 75.0], []])
def test_random_shifts_keep_latitudes_in_range(lats: list[float]) -> None:
    for _ in range(200):
        shift = sf.choose_shift(lats)
        assert abs(shift.lat) >= sf.MIN_SHIFT_DEG
        assert sf.MIN_SHIFT_DEG <= abs(shift.lng) <= 180.0
        for lat in lats:
            assert abs(lat + shift.lat) <= sf.LAT_LIMIT_DEG + 1e-9


def test_manifest_records_hashes_relative_to_its_folder(tmp_path: Path) -> None:
    (tmp_path / "sitl").mkdir()
    f = tmp_path / "sitl" / "a.bin"
    f.write_bytes(b"abc")
    manifest = tmp_path / "SCRUBBED.sha256"
    sf.update_manifest(manifest, [f])
    sf.update_manifest(manifest, [f])  # idempotent
    assert manifest.read_text() == f"{hashlib.sha256(b'abc').hexdigest()}  sitl/a.bin\n"


def test_placeholders_near_null_island_stay_put(tmp_path: Path) -> None:
    """Before a fix, autopilots report positions a few metres from 0,0.

    Shifting those would land them on the shift itself and give it away.
    """
    log = DfLog()
    log.define(140, "GPS", "QLL", "TimeUS,Lat,Lng", "sDU", "FGG")
    log.write("GPS", 1, 980, 10)  # 1e-4 and 1e-6 degrees: no fix yet
    log.write("GPS", 2, e7(0.004), e7(36.8))  # a real place on the equator
    log.write("GPS", 3, e7(HOME_LAT), e7(HOME_LNG))
    path = tmp_path / "early.bin"
    path.write_bytes(bytes(log.data))
    [out] = sf.scrub([path], tmp_path / "out", SHIFT)
    placeholder, equator, home = [m for m in read_df(out) if m["type"] == "GPS"]
    assert (placeholder["Lat"], placeholder["Lng"]) == pytest.approx((980e-7, 10e-7))
    assert equator["Lat"] == pytest.approx(0.004 + SHIFT.lat, abs=2e-7)
    assert home["Lat"] == pytest.approx(HOME_LAT + SHIFT.lat, abs=2e-7)
