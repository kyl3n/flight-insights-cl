"""Validation, flights, modes, identity, SI values and the release enums."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest
from df_builder import LogBuilder

from flight_insights.dataflash import read_dataflash, si
from flight_insights.dataflash.log import DataflashLog
from flight_insights.knowledge import Pack, load_pack
from flight_insights.segment import (
    Boot,
    Flight,
    HoverRules,
    boot_identities,
    find_boots,
    flight_key,
    flights,
    hover_windows,
    mode_spans,
    timeline,
    validate,
)

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl"
FIXTURE_LOGS = sorted(FIXTURES.rglob("*.bin"))


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


def builder() -> LogBuilder:
    b = LogBuilder()
    b.fmt(0x90, "VER", "QBBBIZ", "TimeUS,Maj,Min,Pat,GH,FWS")
    b.fmt(0x91, "PARM", "QNff", "TimeUS,Name,Value,Default")
    b.fmt(0x92, "ARM", "QB", "TimeUS,ArmState")
    b.fmt(0x93, "EV", "QB", "TimeUS,Id")
    b.fmt(0x94, "MODE", "QMBB", "TimeUS,Mode,ModeNum,Rsn")
    b.fmt(0x95, "DSF", "QI", "TimeUS,Dp")
    b.fmt(0x96, "GYR", "QBQfff", "TimeUS,I,SampleUS,GyrX,GyrY,GyrZ")
    b.fmt(0x97, "IMU", "QBII", "TimeUS,I,EG,EA")
    b.fmt(0x98, "RTC", "QQB", "TimeUS,Epoch,SourceType")
    b.fmt(0x99, "GPS", "QBBIH", "TimeUS,I,Status,GMS,GWk")
    b.fmt(0x9A, "ATT", "Qff", "TimeUS,Roll,Pitch")
    return b


def read(b: LogBuilder, tmp_path: Path, name: str = "log.bin") -> DataflashLog:
    path = tmp_path / name
    path.write_bytes(b.bytes())
    return read_dataflash(path)


def heartbeat(b: LogBuilder, start_us: int, end_us: int, step_us: int = 100_000) -> None:
    for t in range(start_us, end_us + 1, step_us):
        b.write("ATT", t, 0.0, 0.0)


def boots_and_flights(df: DataflashLog, pack: Pack) -> tuple[list[Boot], list[Flight]]:
    boots = find_boots(timeline(df))
    return boots, flights(df, boots, pack, "copter", "4.7.2")


# --- validation ---------------------------------------------------------------------


def test_a_clean_fixture_has_no_issues() -> None:
    with read_dataflash(FIXTURES / "4.7.0" / "copter-quad-hover.bin") as df:
        v = validate(df)
    assert len(v.boots) == 1 and v.issues == [] and v.imu_samples_complete


def test_the_raw_imu_fixture_is_complete_but_for_the_loggers_own_drops() -> None:
    """Full-rate raw IMU at a low simulator speed-up: the sample clock jitters but no
    sample is lost, and the few messages the logger dropped at start-up are counted."""
    with read_dataflash(FIXTURES / "4.7.0" / "copter-quad-rawimu.bin") as df:
        v = validate(df)
        dsf = df.field(df.formats_named("DSF")[0], "Dp")
    dropped = sum(i.count for i in v.of_kind("logging dropout"))
    assert dropped == int(dsf.max()) > 0
    assert v.imu_samples_complete, v.of_kind("imu sample loss")


def test_lost_samples_are_counted_exactly(tmp_path: Path) -> None:
    b = builder()
    samples = [t for t in range(1_000_000, 1_100_000, 1000) if not 1_040_000 < t < 1_045_000]
    for t in samples:  # 4 samples missing between 1.040 s and 1.045 s
        b.write("GYR", t, 0, t, 0.0, 0.0, 0.0)
    with read(b, tmp_path) as df:
        v = validate(df)
    [loss] = v.of_kind("imu sample loss")
    assert (loss.count, loss.instance, loss.start_us, loss.end_us) == (4, 0, 1_040_000, 1_045_000)
    assert "about 4 samples missing" in loss.detail


def test_a_jittered_sample_clock_has_lost_nothing(tmp_path: Path) -> None:
    """SITL timestamps a sample when it is processed, as some drivers do with FIFO
    batches: steps of 833, 833 and 1334 us average a steady 1 kHz. Only a real hole
    counts, in samples of the clock's real period."""
    b = builder()
    t, steps = 1_000_000, [833, 833, 1334]
    for k in range(3000):
        b.write("GYR", t, 0, t, 0.0, 0.0, 0.0)
        t += steps[k % 3] + (50_000 if k == 1500 else 0)  # one 50 ms hole
    with read(b, tmp_path) as df:
        v = validate(df)
    [loss] = v.of_kind("imu sample loss")
    assert loss.count == 50


def test_raw_logging_switched_off_in_flight_is_not_sample_loss(tmp_path: Path) -> None:
    """Raw IMU logged for the steady hover only: LOG_BITMASK bit 19 switched on and off
    in flight leaves a gap in the clock that is not loss; a hole while on still is."""
    b = builder()
    raw = 1 << 19
    switches = {1_000_000: raw, 2_000_000: 0, 12_000_000: raw}  # off for ten seconds
    on = False
    for t in range(1_000_000, 13_000_000, 1000):
        if t in switches:
            b.write("PARM", t, b"LOG_BITMASK", float(switches[t]), 0.0)
            on = bool(switches[t])
        if t % 100_000 == 0:
            b.write("ATT", t, 0.0, 0.0)  # the rest of the log carries on
        if on and not 12_500_000 < t < 12_510_000:  # 9 samples lost while on
            b.write("GYR", t, 0, t, 0.0, 0.0, 0.0)
    with read(b, tmp_path) as df:
        v = validate(df)
    [loss] = v.of_kind("imu sample loss")
    assert (loss.count, loss.start_us) == (9, 12_500_000)


def test_a_logging_pause_between_flights_is_not_imu_sample_loss(tmp_path: Path, pack: Pack) -> None:
    """LOG_DISARMED=0 without rotating the file on disarm: two flights in one file with
    a minute of nothing between them. The raw-IMU clock jumps, but no sample was lost."""
    b = builder()
    for arm_us in (1_000_000, 62_000_000):
        b.write("EV", arm_us, 10)
        for t in range(arm_us, arm_us + 1_000_000, 1000):
            b.write("GYR", t, 0, t, 0.0, 0.0, 0.0)
        b.write("EV", arm_us + 1_000_000, 11)
    with read(b, tmp_path) as df:
        _, fl = boots_and_flights(df, pack)
        v = validate(df, [(f.boot, f.arm_us, f.disarm_us) for f in fl])
    assert len(fl) == 2
    assert (2_000_000, 62_000_000) in [(i.start_us, i.end_us) for i in v.of_kind("logging paused")]
    assert v.imu_samples_complete, v.of_kind("imu sample loss")


def test_dropouts_and_imu_errors_come_from_their_counters(tmp_path: Path) -> None:
    b = builder()
    for k, (dp, eg) in enumerate([(0, 0), (0, 0), (3, 0), (3, 2), (10, 2)]):
        b.write("DSF", 1_000_000 + k * 5_000_000, dp)
        b.write("IMU", 1_000_000 + k * 5_000_000, 0, eg, 0)
    with read(b, tmp_path) as df:
        v = validate(df)
    assert [i.count for i in v.of_kind("logging dropout")] == [3, 7]
    [errors] = v.of_kind("imu errors")
    assert errors.count == 2 and "gyro" in errors.detail


def test_gaps_while_armed_are_faults_and_while_disarmed_are_pauses(
    tmp_path: Path, pack: Pack
) -> None:
    b = builder()  # its UNIT and MULT messages are stamped 0
    heartbeat(b, 0, 2_000_000)
    b.write("EV", 2_000_000, 10)
    heartbeat(b, 2_000_000, 3_000_000)
    heartbeat(b, 5_000_000, 6_000_000)  # 2 s hole while armed
    b.write("EV", 6_000_000, 11)
    heartbeat(b, 20_000_000, 21_000_000)  # 14 s hole while disarmed
    with read(b, tmp_path) as df:
        _, fl = boots_and_flights(df, pack)
        v = validate(df, [(f.boot, f.arm_us, f.disarm_us) for f in fl])
    [gap] = v.of_kind("timestamp gap")
    [pause] = v.of_kind("logging paused")
    assert (gap.start_us, gap.end_us) == (3_000_000, 5_000_000)
    assert (pause.start_us, pause.end_us) == (6_000_000, 20_000_000)


def test_a_second_boot_in_one_file_is_found(tmp_path: Path, pack: Pack) -> None:
    b = builder()
    b.write("VER", 900_000, 4, 7, 2, 0xFB773567, b"ArduCopter V4.7.2")
    heartbeat(b, 1_000_000, 30_000_000, 1_000_000)
    b.write("EV", 31_000_000, 11)  # armed from the start of the log
    b.write("VER", 900_000, 4, 7, 2, 0xFB773567, b"ArduCopter V4.7.2")
    heartbeat(b, 1_000_000, 2_000_000, 1_000_000)  # time starts again: a new boot
    b.write("EV", 2_000_000, 10)
    heartbeat(b, 3_000_000, 5_000_000, 1_000_000)
    with read(b, tmp_path) as df:
        boots, fl = boots_and_flights(df, pack)
        v = validate(df)
        ids = boot_identities(df, boots)
    assert len(boots) == 2 and len(v.of_kind("clock reset")) == 1
    assert [(f.boot, f.start, f.end) for f in fl] == [
        (0, "log started armed", "disarmed"),
        (1, "armed", "log ended armed"),
    ]
    assert [f.number for f in fl] == [1, 2]
    assert [i.firmware for i in ids] == ["4.7.2", "4.7.2"]


# --- flights and modes --------------------------------------------------------------


def test_arm_and_ev_records_of_one_change_count_once(tmp_path: Path, pack: Pack) -> None:
    b = builder()
    changes = {5_000_000: True, 10_000_000: False, 20_000_000: True}
    for t in range(1_000_000, 40_000_001, 1_000_000):
        if t in changes:
            b.write("ARM", t, int(changes[t]))
            b.write("EV", t, 10 if changes[t] else 11)
        elif t == 30_000_000:
            b.write("ARM", t, 0)
        b.write("ATT", t, 0.0, 0.0)
    with read(b, tmp_path) as df:
        _, fl = boots_and_flights(df, pack)
    assert [(f.arm_us, f.disarm_us, f.duration_s) for f in fl] == [
        (5_000_000, 10_000_000, 5.0),
        (20_000_000, 30_000_000, 10.0),
    ]


def test_modes_take_their_names_from_the_release(tmp_path: Path, pack: Pack) -> None:
    b = builder()
    modes_at = {1_000_000: 4, 2_000_000: 4, 5_000_000: 9, 8_000_000: 99}
    for t in range(1_000_000, 10_000_001, 1_000_000):
        if t in modes_at:
            b.write("MODE", t, modes_at[t], modes_at[t], 2)
        b.write("ATT", t, 0.0, 0.0)
    with read(b, tmp_path) as df:
        boots = find_boots(timeline(df))
        modes = mode_spans(df, boots, pack, "copter", "4.7.2")
    assert [(m.name, m.start_us, m.end_us) for m in modes] == [
        ("GUIDED", 1_000_000, 5_000_000),  # entered twice, one span
        ("LAND", 5_000_000, 8_000_000),
        ("MODE_99", 8_000_000, 10_000_000),
    ]


@pytest.mark.parametrize("log", FIXTURE_LOGS, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_every_fixture_is_one_flight_with_the_modes_its_scenario_flew(
    pack: Pack, log: Path
) -> None:
    from flight_insights.history import log_firmware

    with read_dataflash(log) as df:
        vehicle, firmware = log_firmware(df)
        boots = find_boots(timeline(df))
        fl = flights(df, boots, pack, vehicle, firmware)
    [flight] = fl
    names = [m.name for m in flight.modes]
    expected = {
        "copter-quad-hover": ["GUIDED", "LAND"],
        "copter-quad-rawimu": ["GUIDED", "LAND"],
        "copter-quad-fastnotch": ["GUIDED", "LAND"],
        "copter-heavylift-light": ["GUIDED", "LAND"],
        "copter-heavylift-loaded": ["GUIDED", "LAND"],
        "heli-hover": ["GUIDED", "LAND"],
        "plane-circuit": ["AUTO", "RTL"],
        "quadplane-hover": ["QLOITER", "QLAND"],
    }[log.stem]
    assert names == expected
    assert flight.end == "disarmed" and flight.duration_s > 20


# --- identity -----------------------------------------------------------------------


def test_utc_comes_from_rtc_or_else_the_first_3d_fix(tmp_path: Path) -> None:
    b = builder()
    b.write("GPS", 3_000_000, 0, 1, 0, 0)  # no fix yet
    b.write("GPS", 4_000_000, 0, 3, 18_000, 2441)  # week 2441, 18 s into it
    with read(b, tmp_path, "gps.bin") as df:
        [gps] = boot_identities(df, find_boots(timeline(df)))
    week_start_utc_s = 315_964_800 + 2441 * 604_800 - 18
    assert gps.utc_source == "GPS"
    assert gps.boot_utc_us == (week_start_utc_s + 18) * 1_000_000 - 4_000_000
    b.write("RTC", 5_000_000, 1_800_000_000_000_000, 0)
    with read(b, tmp_path, "rtc.bin") as df:
        [rtc] = boot_identities(df, find_boots(timeline(df)))
    assert (rtc.utc_source, rtc.boot_utc_us) == ("RTC", 1_800_000_000_000_000 - 5_000_000)


def test_a_truncated_copy_is_the_same_flight(tmp_path: Path, pack: Pack) -> None:
    from flight_insights.history import log_firmware

    log = FIXTURES / "4.7.2" / "copter-quad-hover.bin"
    data = log.read_bytes()
    copy = tmp_path / "truncated.bin"
    copy.write_bytes(data[: len(data) * 2 // 3])
    keys: list[str] = []
    for path in (log, copy):
        with read_dataflash(path) as df:
            vehicle, firmware = log_firmware(df)
            boots = find_boots(timeline(df))
            [boot] = boot_identities(df, boots)
            [flight] = flights(df, boots, pack, vehicle, firmware)
            keys.append(flight_key(boot, flight, "board-hash"))
        assert boot.firmware == "4.7.2" and boot.git_hash == "fb773567"
        assert boot.boot_count == 1 and boot.utc_source == "RTC"
    assert keys[0] == keys[1] and keys[0].startswith("fl1-")
    with read_dataflash(FIXTURES / "4.7.0" / "copter-quad-hover.bin") as df:
        boots = find_boots(timeline(df))
        [boot] = boot_identities(df, boots)
        [flight] = flights(df, boots, pack, "copter", "4.7.0")
    assert flight_key(boot, flight, "board-hash") != keys[0]
    assert flight_key(boot, flight, "board-hash") != flight_key(boot, flight, "other-board")


# --- SI values and enums ------------------------------------------------------------


def test_values_follow_the_fmtu_multiplier_not_the_format_character() -> None:
    with read_dataflash(FIXTURES / "4.7.0" / "copter-quad-hover.bin") as df:
        xkf1 = df.table("XKF1")
        gps = df.table("GPS")
    raw_roll = np.asarray(xkf1.column("Roll").to_numpy(), dtype=np.float64)
    assert si.unit(xkf1, "Roll") == "deg"
    assert np.allclose(si.values(xkf1, "Roll"), raw_roll * 0.01)  # c with multiplier B
    assert np.array_equal(si.values(xkf1, "VN"), xkf1.column("VN").to_numpy())
    assert np.allclose(si.values(gps, "TimeUS"), gps.column("TimeUS").to_numpy() * 1e-6)


def test_the_release_enums_name_modes_and_events(pack: Pack) -> None:
    for version in ("4.7.0", "4.7.1", "4.7.2"):
        enums = pack.enums(version)
        assert enums["modes"]["copter"]["4"] == "GUIDED"
        assert enums["modes"]["plane"]["19"] == "QLOITER"
        assert enums["events"]["10"] == "ARMED" and enums["events"]["11"] == "DISARMED"
    assert pack.enums("4.7.9")["version"] == "4.7.2"


# --- steady hover -------------------------------------------------------------------


def label_hover(log: Path) -> tuple[float, str | None] | None:
    """The hover a fixture's label promises: (seconds, mode), or None for no hover."""
    m = re.search(
        r"about (\d+) s of (?:steady )?hover(?: in (\w+))?", log.with_suffix(".yaml").read_text()
    )
    return (float(m[1]), m[2]) if m else None


@pytest.mark.parametrize("log", FIXTURE_LOGS, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_each_fixture_hovers_as_its_label_says(pack: Pack, log: Path) -> None:
    from flight_insights.history import log_firmware

    with read_dataflash(log) as df:
        vehicle, firmware = log_firmware(df)
        boots = find_boots(timeline(df))
        fl = flights(df, boots, pack, vehicle, firmware)
        windows = hover_windows(df, boots, fl, vehicle, HoverRules.load(pack))
    expected = label_hover(log)
    if expected is None:
        assert windows == []
        return
    seconds, mode = expected
    [w] = windows
    assert abs(w.duration_s - seconds) <= 1.5, w
    assert w.flight == 1 and (mode is None or w.mode == mode)


def test_hover_needs_every_condition_for_long_enough(tmp_path: Path, pack: Pack) -> None:
    rules = HoverRules.load(pack)
    b = builder()
    b.fmt(0xA0, "XKF1", "QBccffff", "TimeUS,C,Roll,Pitch,VN,VE,VD,PD")
    b.units("XKF1", "s#ddnnnm", "F-BB0000")
    b.write("EV", 1_000_000, 10)

    def sample(
        t: int, vn: float = 0.0, vd: float = 0.0, roll_cd: int = 0, pd: float = -5.0
    ) -> None:
        b.write("XKF1", t, 0, roll_cd, 0, vn, 0.0, vd, pd)
        b.write("XKF1", t, 1, 0, 0, 9.0, 0.0, 0.0, 0.0)  # another core: ignored

    sample(1_000_000, pd=0.0)  # on the ground at arming
    t = 1_100_000
    for _ in range(30):  # 3 s: too short
        sample(t)
        t += 100_000
    sample(t, vn=2.0)  # drifting
    t += 100_000
    for _ in range(61):  # 6 s steady
        sample(t)
        t += 100_000
    sample(t, roll_cd=1500)  # 15 degrees of roll
    t += 100_000
    for _ in range(80):  # 8 s, but only 1 m up
        sample(t, pd=-1.0)
        t += 100_000
    b.write("EV", t, 11)
    with read(b, tmp_path) as df:
        boots = find_boots(timeline(df))
        fl = flights(df, boots, pack, "copter", "4.7.2")
        windows = hover_windows(df, boots, fl, "copter", rules)
        plane = hover_windows(df, boots, fl, "plane", rules)
    [w] = windows
    assert (w.start_us, w.end_us, w.samples) == (4_200_000, 10_200_000, 61)
    assert plane == []  # not in a VTOL mode
    assert not rules.calibrated
