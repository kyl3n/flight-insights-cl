"""The DataFlash decoder, checked against pymavlink's DFReader on every message."""

from __future__ import annotations

import array
import math
import struct
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from df_builder import LogBuilder
from pymavlink_ref import reference

from flight_insights.dataflash import read_dataflash
from flight_insights.dataflash.compat import pymavlink_messages

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
FIXTURE_LOGS = sorted(FIXTURES.rglob("*.bin"))


def meta(table: pa.Table, column: str) -> dict[bytes, bytes]:
    metadata = table.schema.field(column).metadata  # pyright: ignore[reportUnknownMemberType]
    assert metadata is not None
    return metadata


def same(a: Any, b: Any) -> bool:
    """Equal, bit for bit for floats (so -0.0 and NaN payloads count)."""
    if isinstance(a, array.array):
        a = a.tolist()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if isinstance(a, float) or isinstance(b, float):
        return (
            isinstance(a, float)
            and isinstance(b, float)
            and struct.pack("<d", a) == struct.pack("<d", b)
        )
    return type(a) is type(b) and a == b


def assert_matches_pymavlink(path: Path) -> int:
    ref = reference(path)
    with read_dataflash(path) as log:
        ours = list(pymavlink_messages(log))
    assert len(ours) == len(ref), "message count differs from pymavlink"
    for i, ((rt, rv), (ot, ov)) in enumerate(zip(ref, ours, strict=True)):
        assert rt == ot, f"message {i}: type {ot} where pymavlink has {rt}"
        assert list(rv) == list(ov), f"message {i} ({rt}): columns differ"
        for col in rv:
            assert same(rv[col], ov[col]), (
                f"message {i} {rt}.{col}: {ov[col]!r} where pymavlink has {rv[col]!r}"
            )
    return len(ref)


def write(tmp_path: Path, log: LogBuilder, name: str = "log.bin") -> Path:
    path = tmp_path / name
    path.write_bytes(log.bytes())
    return path


# --- committed fixtures ----------------------------------------------------------


@pytest.mark.parametrize("path", FIXTURE_LOGS, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_fixture_decodes_exactly_as_pymavlink(path: Path) -> None:
    assert assert_matches_pymavlink(path) > 1000


def test_fixtures_exist() -> None:
    assert FIXTURE_LOGS


# --- every format character --------------------------------------------------------

# FMT's format field holds 16 characters, so the alphabet takes two message types.
NUMS, MORE = "abBhHiIqQfdgcCeE", "LMnNZ"


def columns(fmt: str) -> str:
    return ",".join(f"V{c}" if c != "a" else "VA" for c in fmt)


def all_chars_log() -> LogBuilder:
    log = LogBuilder()
    log.fmt(0x90, "NUMS", NUMS, columns(NUMS))
    log.fmt(0x91, "MORE", MORE, columns(MORE))
    log.write(
        "NUMS",
        list(range(-16, 16)), -128, 255, -32768, 65535, -(2**31), 2**32 - 1, -(2**63),
        2**64 - 1, 1.1, -2.2e-300, 0.5, -12345, 54321, -123456789, 4000000000,
    )  # fmt: skip
    log.write(
        "NUMS",
        [0] * 32, 0, 0, 0, 0, 0, 0, 0, 0, float("nan"), float("-inf"), -0.0, 0, 0, 0, 0,
    )  # fmt: skip
    log.write("MORE", -353632621, 200, b"abcd", b"sixteen-chars-xx", b"hello\x00world")
    log.write("MORE", 1491652374, 0, b"", b"", b"")
    return log


def test_every_format_character_matches_pymavlink(tmp_path: Path) -> None:
    assert_matches_pymavlink(write(tmp_path, all_chars_log()))


def test_tables_hold_stored_values_with_their_reading_rules(tmp_path: Path) -> None:
    with read_dataflash(write(tmp_path, all_chars_log())) as log:
        nums = log.table("NUMS")
        row = nums.slice(0, 1).to_pylist()[0]
        assert row["VA"] == list(range(-16, 16))
        assert row["Vq"] == -(2**63) and row["VQ"] == 2**64 - 1
        assert row["Vc"] == -12345  # stored integer; the c format implies x0.01
        assert nums.column("Vf").type == pa.float32()
        more = log.table("MORE")
        row = more.slice(0, 1).to_pylist()[0]
        assert row["VL"] == -353632621  # stored integer, not degrees
        assert row["VM"] == 200  # flight modes are unsigned
        assert row["VZ"] == b"hello\x00world".ljust(64, b"\x00")
        vl = meta(more, "VL")
        assert vl[b"format"] == b"L" and float(vl[b"scale"]) == 1e-7


# --- the file's structure -------------------------------------------------------------


def gps_log() -> LogBuilder:
    log = LogBuilder()
    log.fmt(0x91, "GPS", "QBLLf", "TimeUS,I,Lat,Lng,Alt")
    log.units("GPS", "s#DUm", "F-GG0")
    return log


def test_units_instances_and_positions_come_from_the_log(tmp_path: Path) -> None:
    log = gps_log()
    for t in range(6):
        log.write("GPS", t, t % 2, -353632621 + t, 1491652374, 584.0)
    with read_dataflash(write(tmp_path, log)) as df:
        gps = df.table("GPS")
        fields = {name: meta(gps, name) for name in gps.schema.names}
        assert fields["Lat"][b"position"] == b"1" and fields["Lng"][b"position"] == b"1"
        assert fields["Alt"][b"position"] == b"0" and fields["Alt"][b"unit"] == b"m"
        assert fields["I"][b"instance"] == b"1"
        assert float(fields["TimeUS"][b"multiplier"]) == 1e-6
        split = df.instances("GPS")
        assert sorted(split) == [0, 1]
        assert split[1].column("TimeUS").to_pylist() == [1, 3, 5]


def test_units_written_after_the_messages_still_apply(tmp_path: Path) -> None:
    log = LogBuilder()
    log.fmt(0x91, "BARO", "Qf", "TimeUS,Alt")
    log.write("BARO", 1, 2.0)
    log.units("BARO", "sm", "F0")
    with read_dataflash(write(tmp_path, log)) as df:
        assert meta(df.table("BARO"), "Alt")[b"unit"] == b"m"


def test_junk_bytes_are_skipped_one_at_a_time(tmp_path: Path) -> None:
    log = gps_log()
    log.write("GPS", 1, 0, 1, 2, 3.0)
    log.raw(b"\x00\xa3\x95\xff\x13")  # includes a header with an undefined type
    log.write("GPS", 2, 0, 1, 2, 3.0)
    path = write(tmp_path, log)
    assert_matches_pymavlink(path)
    with read_dataflash(path) as df:
        assert df.index.skipped_bytes == 5
        assert df.table("GPS").num_rows == 2


def test_a_truncated_last_message_ends_the_log(tmp_path: Path) -> None:
    log = gps_log()
    for t in range(3):
        log.write("GPS", t, 0, 1, 2, 3.0)
    path = tmp_path / "cut.bin"
    path.write_bytes(log.bytes()[:-7])
    assert_matches_pymavlink(path)
    with read_dataflash(path) as df:
        assert df.index.stop == "truncated"
        assert df.table("GPS").num_rows == 2


def test_types_defined_mid_log_and_defined_again(tmp_path: Path) -> None:
    log = LogBuilder()
    log.fmt(0x92, "RATE", "Qf", "TimeUS,R")
    log.write("RATE", 1, 0.5)
    log.fmt(0x93, "LATE", "Qh", "TimeUS,X")
    log.write("LATE", 2, -7)
    log.fmt(0x92, "RATE", "Qf", "TimeUS,R")  # the same definition again
    log.write("RATE", 3, 0.25)
    path = write(tmp_path, log)
    assert_matches_pymavlink(path)
    with read_dataflash(path) as df:
        assert df.table("RATE").column("R").to_pylist() == [0.5, 0.25]
        assert df.table("LATE").column("X").to_pylist() == [-7]


def test_messages_before_their_fmt_decode(tmp_path: Path) -> None:
    """pymavlink learns every format in a first pass, so an FMT can follow its messages."""
    log = LogBuilder()
    log.formats["ERLY"] = (0x94, "Qi")
    log.write("ERLY", 1, -5)
    log.fmt(0x94, "ERLY", "Qi", "TimeUS,V")
    log.write("ERLY", 2, 6)
    path = write(tmp_path, log)
    assert_matches_pymavlink(path)
    with read_dataflash(path) as df:
        assert df.table("ERLY").column("V").to_pylist() == [-5, 6]


def test_a_type_redefined_with_new_columns_decodes_with_each_layout(tmp_path: Path) -> None:
    """Here pymavlink 2.4.50 is wrong: it keeps the first layout and stops reading."""
    log = LogBuilder()
    log.fmt(0x93, "LATE", "Qh", "TimeUS,X")
    log.write("LATE", 2, -7)
    log.fmt(0x93, "LATE", "QhB", "TimeUS,X,Y")
    log.write("LATE", 4, 8, 9)
    path = write(tmp_path, log)
    assert [t for t, _ in reference(path)][-1] == "FMT"  # pymavlink gives up here
    with read_dataflash(path) as df:
        rows = [(t, v["TimeUS"]) for t, v in pymavlink_messages(df) if t == "LATE"]
        assert rows == [("LATE", 2), ("LATE", 4)]
        with pytest.raises(ValueError, match="different columns"):
            df.table("LATE")


def test_an_fmt_with_an_unknown_character_defines_nothing(tmp_path: Path) -> None:
    """Here pymavlink 2.4.50 raises and reads nothing; the engine resyncs past it."""
    log = gps_log()
    log.raw(b"\xa3\x95\x80" + struct.pack("<BB4s16s64s", 0x94, 12, b"ODD", b"QX", b"TimeUS,Odd"))
    log.raw(b"\xa3\x95\x94" + bytes(9))
    log.write("GPS", 1, 0, 1, 2, 3.0)
    path = write(tmp_path, log)
    with pytest.raises(Exception, match="Unsupported format char"):
        reference(path)
    with read_dataflash(path) as df:
        assert "ODD" not in df.message_types
        assert df.table("GPS").num_rows == 1


def test_messages_whose_length_disagrees_with_their_format_are_rejected(tmp_path: Path) -> None:
    log = gps_log()
    log.fmt(0x95, "LONG", "Qf", "TimeUS,V", length=3 + 12 + 4)  # declares 4 bytes too many
    log.raw(b"\xa3\x95\x95" + bytes(16))
    for t in range(40):  # enough data after it that pymavlink keeps going
        log.write("GPS", t, 0, 1, 2, 3.0)
    path = write(tmp_path, log)
    assert_matches_pymavlink(path)
    with read_dataflash(path) as df:
        assert len(df.index.rejected) == 1
        assert df.table("GPS").num_rows == 40


def test_strings_decode_like_pymavlink(tmp_path: Path) -> None:
    log = LogBuilder()
    log.fmt(0x96, "TXT", "QN", "TimeUS,S")
    for s in [b"caf\xc3\xa9", b"caf\xe9", b"ok\x00junk\xff", b"\xff\xfe"]:
        log.write("TXT", 1, s)
    assert_matches_pymavlink(write(tmp_path, log))


def test_an_empty_file_has_no_messages(tmp_path: Path) -> None:
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    with read_dataflash(path) as df:
        assert df.message_types == []
        assert len(df.index.offsets) == 0


@pytest.mark.parametrize("value", [1.5, -1e-300, math.pi])
def test_doubles_survive_exactly(tmp_path: Path, value: float) -> None:
    log = LogBuilder()
    log.fmt(0x97, "DBL", "Qd", "TimeUS,V")
    log.write("DBL", 1, value)
    with read_dataflash(write(tmp_path, log)) as df:
        assert df.table("DBL").column("V").to_pylist() == [value]


def test_unit_messages_with_unexpected_column_kinds_are_ignored(tmp_path: Path) -> None:
    """Found by fuzzing: an FMTU whose FmtType is a string must not crash the decoder."""
    log = LogBuilder(units=False)
    log.fmt(0x83, "FMTU", "QNNN", "TimeUS,FmtType,UnitIds,MultIds")
    log.fmt(0x91, "BARO", "Qf", "TimeUS,Alt")
    log.write("FMTU", 0, b"ts---", b"sm", b"F0")
    log.write("BARO", 1, 2.0)
    with read_dataflash(write(tmp_path, log)) as df:
        assert df.table("BARO").num_rows == 1
        assert meta(df.table("BARO"), "Alt")[b"unit"] == b""


def test_a_redefinition_that_changes_only_types_is_refused_cleanly(tmp_path: Path) -> None:
    """Found by fuzzing: same column names, different types, must not reach Arrow's concat."""
    log = LogBuilder()
    log.fmt(0x92, "RATE", "Qf", "TimeUS,R")
    log.write("RATE", 1, 0.5)
    log.fmt(0x92, "RATE", "Qi", "TimeUS,R")
    log.write("RATE", 2, 7)
    with (
        read_dataflash(write(tmp_path, log)) as df,
        pytest.raises(ValueError, match="different columns or types"),
    ):
        df.table("RATE")
