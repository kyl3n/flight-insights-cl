"""Mission Planner, QGroundControl and Methodic Configurator parameter files."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from flight_insights.params import (
    format_value,
    parse_params,
    read_methodic_folder,
    read_param_file,
    write_mission_planner,
    write_qgroundcontrol,
)

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
SESSIONS = sorted(FIXTURES.rglob("*.param"))


def f32(values: dict[str, float]) -> dict[str, float]:
    return {k: float(np.float32(v)) for k, v in values.items()}


@pytest.mark.parametrize("path", SESSIONS, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_both_formats_of_a_session_hold_the_same_parameters(path: Path) -> None:
    mp = read_param_file(path)
    qgc = read_param_file(path.with_suffix(".params"))
    assert (mp.kind, qgc.kind) == ("mission planner", "qgroundcontrol")
    assert not mp.problems and not qgc.problems
    assert mp.values() == qgc.values()
    assert {e.component for e in qgc.entries} == {(1, 1)}
    assert len(mp.entries) > 1000


@pytest.mark.parametrize("path", SESSIONS[:2], ids=lambda p: str(p.relative_to(FIXTURES)))
def test_writers_round_trip_at_the_autopilots_float32_precision(path: Path) -> None:
    values = read_param_file(path).values()
    mp = parse_params(write_mission_planner(values))
    qgc = parse_params(write_qgroundcontrol(values, (1, 1), title="test"))
    assert mp.kind == "mission planner" and qgc.kind == "qgroundcontrol"
    assert mp.values() == qgc.values()
    assert f32(mp.values()) == f32(values)
    assert parse_params(write_mission_planner(mp.values())).values() == mp.values()  # stable


def test_mission_planner_separators_and_comments() -> None:
    text = (
        "# saved by Mission Planner\n"
        "ATC_RAT_RLL_P,0.135\n"
        "ATC_RAT_PIT_P 0.135\n"
        "ATC_RAT_YAW_P\t0.18\n"
        "INS_HNTCH_FREQ,80 # first harmonic measured at hover\n"
    )
    params = parse_params(text)
    assert params.kind == "mission planner"
    assert params.header == ["saved by Mission Planner"]
    assert params.values() == {
        "ATC_RAT_RLL_P": 0.135,
        "ATC_RAT_PIT_P": 0.135,
        "ATC_RAT_YAW_P": 0.18,
        "INS_HNTCH_FREQ": 80,
    }
    assert params.entries[-1].comment == "first harmonic measured at hover"
    assert params.entries[0].text == "0.135"


def test_qgroundcontrol_keeps_components_apart() -> None:
    text = (
        "# Vehicle-Id Component-Id Name Value Type\n"
        "1\t1\tCAN_D1_PROTOCOL\t1\t2\n"
        "1\t1\tATC_RAT_RLL_P\t0.135\t9\n"
        "1\t154\tGIMBAL_MODE\t2\t2\n"
    )
    params = parse_params(text)
    assert params.kind == "qgroundcontrol"
    assert params.values((1, 1)) == {"CAN_D1_PROTOCOL": 1, "ATC_RAT_RLL_P": 0.135}
    assert params.values((1, 154)) == {"GIMBAL_MODE": 2}
    assert [e.type for e in params.entries] == [2, 9, 2]


def test_bad_lines_are_reported_and_the_rest_still_counts() -> None:
    text = (
        "ATC_RAT_RLL_P,0.135\n"
        "JUSTANAME\n"
        "BAD NAME HERE,1\n"
        "THIS_NAME_IS_FAR_TOO_LONG,1\n"
        "ATC_RAT_PIT_P,abc\n"
        "ATC_RAT_YAW_P,nan\n"
        "ATC_RAT_RLL_P,0.15\n"
    )
    params = parse_params(text)
    assert params.values() == {"ATC_RAT_RLL_P": 0.15}  # a repeat: the later value counts
    lines = [line for line, _ in params.problems]
    assert lines == [2, 3, 4, 5, 6, 7]
    assert "repeats line 1" in params.problems[-1][1]


def test_qgroundcontrol_ids_and_types_must_be_ascii_digits() -> None:
    """A "²" passes str.isdigit() but not int(): the line is a problem, not a crash."""
    text = (
        "1\t1\tATC_RAT_RLL_P\t0.135\t9\n"
        "1²\t1\tATC_RAT_PIT_P\t0.135\t9\n"
        "1\t1\tATC_RAT_YAW_P\t0.18\t9²\n"
    )
    params = parse_params(text, "qgroundcontrol")
    assert [line for line, _ in params.problems] == [2]
    assert params.values() == {"ATC_RAT_RLL_P": 0.135, "ATC_RAT_YAW_P": 0.18}
    assert [e.type for e in params.entries] == [9, None]


def test_format_value_writes_the_shortest_exact_float32() -> None:
    assert format_value(1.0) == "1"
    assert format_value(0.135) == "0.135"
    assert format_value(-35.363261) == "-35.363262"
    assert float(np.float32(float(format_value(1e-7)))) == float(np.float32(1e-7))


def test_methodic_configurator_steps_in_order_with_reasons(tmp_path: Path) -> None:
    (tmp_path / "10_first_flight_tune.param").write_text(
        "ATC_RAT_RLL_P,0.12 # reduced after the first hover oscillated\n"
    )
    (tmp_path / "02_imu_temperature_calibration_setup.param").write_text(
        "INS_TCAL1_ENABLE,1 # calibrate before tuning\n"
    )
    (tmp_path / "vehicle.jpg").write_bytes(b"not a parameter file")
    (tmp_path / "notes.param").write_text("X,1\n")  # not numbered: not a step
    steps = read_methodic_folder(tmp_path)
    assert [(s.number, s.name) for s in steps] == [
        (2, "imu_temperature_calibration_setup"),
        (10, "first_flight_tune"),
    ]
    assert steps[1].params.entries[0].comment == "reduced after the first hover oscillated"


@given(st.text(max_size=2000))
def test_any_text_parses_without_raising(text: str) -> None:
    for kind in (None, "mission planner", "qgroundcontrol"):
        parse_params(text, kind)  # type: ignore[arg-type]
