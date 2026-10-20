"""The vehicle fingerprint and the board's keyed identity."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from df_builder import LogBuilder

from flight_insights.dataflash import read_dataflash
from flight_insights.fingerprint import Fingerprint, fingerprint, hash_board_id, read_board
from flight_insights.history import from_log
from flight_insights.knowledge import Pack, load_pack

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl"
UID = "00350029 3130510B 36383230"


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


@pytest.fixture(scope="module")
def prints(pack: Pack) -> dict[str, Fingerprint]:
    key = secrets.token_bytes(32)
    out: dict[str, Fingerprint] = {}
    for log in sorted(FIXTURES.rglob("*.bin")):
        with read_dataflash(log) as df:
            out[f"{log.parent.name}/{log.stem}"] = fingerprint(df, pack, key)
    return out


def board_log(tmp_path: Path, system_id: str, hal: int = 10) -> Path:
    b = LogBuilder()
    b.fmt(0x90, "VER", "QBBBBZ", "TimeUS,BT,Maj,Min,Pat,FWS")
    b.fmt(0x91, "MSG", "QZ", "TimeUS,Message")
    b.write("MSG", 1000, b"ArduCopter V4.7.2 (fb773567)")  # before VER: not the ID
    b.write("VER", 2000, hal, 4, 7, 2, b"ArduCopter V4.7.2 (fb773567)")
    b.write("MSG", 2000, system_id.encode())
    b.write("MSG", 2000, b"Param space used: 1234/16384")
    path = tmp_path / "board.bin"
    path.write_bytes(b.bytes())
    return path


# --- board --------------------------------------------------------------------------


def test_a_chibios_board_is_named_and_its_id_only_kept_hashed(tmp_path: Path, pack: Pack) -> None:
    key = secrets.token_bytes(32)
    with read_dataflash(board_log(tmp_path, f"CubeOrange {UID}")) as df:
        board = read_board(df, pack, "4.7.2", key)
    assert (board.hal, board.name, board.mcu, board.family, board.apj_board_id) == (
        "ChibiOS", "CubeOrange", "STM32H743xx", "H7", 140,
    )  # fmt: skip
    assert board.id_hash == hash_board_id(key, UID)
    assert UID not in repr(board) and UID.replace(" ", "") not in repr(board)


def test_board_hashes_need_the_fleet_key() -> None:
    k1, k2 = secrets.token_bytes(32), secrets.token_bytes(32)
    assert hash_board_id(k1, UID) == hash_board_id(k1, UID)
    assert hash_board_id(k1, UID) != hash_board_id(k2, UID)
    assert hash_board_id(k1, UID) != hash_board_id(k1, "00350029 3130510B 36383231")
    assert hash_board_id(k1, UID).startswith("bid1-")
    with pytest.raises(ValueError):
        hash_board_id(b"short", UID)


def test_a_blanked_or_missing_id_gives_no_identity(tmp_path: Path, pack: Pack) -> None:
    key = secrets.token_bytes(32)
    with read_dataflash(board_log(tmp_path, "CubeOrange 00000000 00000000 00000000")) as df:
        assert read_board(df, pack, "4.7.2", key).id_hash is None
    with read_dataflash(board_log(tmp_path, "0" * 32, hal=3)) as df:
        sitl = read_board(df, pack, "4.7.2", key)
    assert (sitl.hal, sitl.name, sitl.family, sitl.id_hash) == ("SITL", "SITL", "SITL", None)


def test_an_unknown_board_is_named_without_a_processor(tmp_path: Path, pack: Pack) -> None:
    with read_dataflash(board_log(tmp_path, f"HomeMadeFC {UID}")) as df:
        board = read_board(df, pack, "4.7.2", secrets.token_bytes(32))
    assert (board.name, board.mcu, board.family) == ("HomeMadeFC", None, None)
    assert board.id_hash is not None


def test_every_release_has_a_board_table(pack: Pack) -> None:
    for version in ("4.7.0", "4.7.1", "4.7.2"):
        boards = pack.boards(version)
        assert len(boards) > 400
        assert boards["CubeOrange"]["family"] == "H7"
        assert boards["Pixhawk1"]["family"] == "F4"
        assert all(len(name) <= 23 for name in boards)  # the logged short name


# --- fixtures -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "vehicle_class", "frame_class", "detail"),
    [
        ("4.7.0/copter-quad-hover", "multirotor", "Quad", "Plus"),
        ("4.7.0/heli-hover", "helicopter", "Heli", "single"),
        ("4.7.0/plane-circuit", "plane", None, None),
        ("4.7.0/quadplane-hover", "quadplane", "Quad", "standard"),
        ("4.7.2/quadplane-hover", "quadplane", "Quad", "standard"),
    ],
)
def test_frames(
    prints: dict[str, Fingerprint],
    fixture: str,
    vehicle_class: str,
    frame_class: str | None,
    detail: str | None,
) -> None:
    frame = prints[fixture].frame.value
    assert frame is not None
    assert (frame.vehicle_class, frame.frame_class) == (vehicle_class, frame_class)
    assert detail in (frame.frame_type, frame.heli, frame.vtol)


def test_common_attributes_of_every_fixture(prints: dict[str, Fingerprint]) -> None:
    for name, fp in prints.items():
        assert fp.firmware == name.split("/")[0]
        assert fp.board.family == "SITL" and fp.board.id_hash is None, name
        assert fp.imu_count.value == 2
        assert fp.imu_rates_hz.value is not None and fp.imu_rates_hz.value[0][0] == pytest.approx(
            1000, abs=2
        )
        assert fp.gps.value is not None and fp.gps.value["configured"] == 1
        assert fp.compasses.value == 3
        assert fp.ekf_sources.value is not None and fp.ekf_sources.value["POSXY"] == "GPS"
        assert fp.motor_protocol.source and fp.cells.source  # every attribute says where from
        assert not fp.rules_calibrated
        if fp.cells.value is not None:
            assert fp.cells.value == 3  # SITL's default 3S battery


def test_measured_hover_throttle_matches_what_the_autopilot_learned(
    prints: dict[str, Fingerprint], pack: Pack
) -> None:
    for name in ("4.7.0/copter-quad-hover", "4.7.2/copter-quad-hover"):
        with read_dataflash(FIXTURES / f"{name}.bin") as df:
            learned_after = from_log(df).final_values()["MOT_THST_HOVER"]
        measured = prints[name].hover_throttle.value
        assert measured is not None and abs(measured - learned_after) < 0.02


def test_the_propulsion_peak_comes_from_esc_rpm_in_hover(prints: dict[str, Fingerprint]) -> None:
    heli = prints["4.7.0/heli-hover"]
    assert "ESC telemetry" in (heli.rpm_sources.value or [])
    assert heli.motor_hz_at_hover.value == pytest.approx(
        25.0, abs=0.5
    )  # SITL's 1500 rpm head speed
    quad = prints["4.7.0/copter-quad-hover"]
    assert quad.motor_hz_at_hover.value is None  # the quad model reports no RPM
    assert quad.motor_hz_at_hover.note == "no RPM in hover"


def test_the_flight_profile(prints: dict[str, Fingerprint]) -> None:
    fp = prints["4.7.0/quadplane-hover"]
    assert set(fp.mode_s) == {"QLOITER", "QLAND"}
    # modes cover the flight but for the moment before the first MODE record
    assert sum(fp.mode_s.values()) == pytest.approx(fp.flight_s, abs=0.1)
    assert 18 < fp.hover_s < 21 and fp.hover_windows
    assert prints["4.7.0/plane-circuit"].hover_s == 0
