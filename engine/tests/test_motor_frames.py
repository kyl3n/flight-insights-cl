"""The motor frame table (knowledge/frames/): every multirotor frame's motor factors as
ArduPilot's matrix mixer sets them, and the generator that reads them from its source."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from flight_insights.knowledge import load_pack

TOOL = Path(__file__).resolve().parents[2] / "knowledge/tools/build_motor_frames.py"
SYMMETRIC = {"PLUS", "X", "BF_X", "DJI_X", "CW_X", "X_REV", "PLUSREV", "X/CW_X"}


def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_motor_frames", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_motor_frames"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def frames() -> dict[str, dict[str, Any]]:
    return load_pack().frames("4.7.0")


def factors(frames: dict[str, Any], cls: int, kind: int | str) -> list[tuple[float, ...]]:
    motors = frames[str(cls)]["types"][str(kind)]["motors"]
    return [(m["roll"], m["pitch"], m["yaw"], m["throttle"]) for m in motors]


def test_a_quad_x_is_the_mixer_ardupilot_documents(frames: dict[str, Any]) -> None:
    # motor 1 front right counter-clockwise, 2 back left counter-clockwise, 3 front left
    # clockwise, 4 back right clockwise
    assert factors(frames, 1, 1) == [
        (-0.5, 0.5, 0.5, 1.0), (0.5, -0.5, 0.5, 1.0),
        (0.5, 0.5, -0.5, 1.0), (-0.5, -0.5, -0.5, 1.0),
    ]  # fmt: skip
    assert frames["1"]["name"] == "QUAD" and frames["1"]["types"]["1"]["name"] == "X"


def test_every_class_the_mixer_sets_up_is_there(frames: dict[str, Any]) -> None:
    names = {int(k): v["name"] for k, v in frames.items()}
    assert names == {1: "QUAD", 2: "HEXA", 3: "OCTA", 4: "OCTAQUAD", 5: "Y6",
                     12: "DODECAHEXA", 14: "DECA"}  # fmt: skip
    counts = {"QUAD": 4, "HEXA": 6, "OCTA": 8, "OCTAQUAD": 8, "Y6": 6, "DODECAHEXA": 12,
              "DECA": 10}  # fmt: skip
    for c in frames.values():
        for t in c["types"].values():
            assert len(t["motors"]) == counts[c["name"]]
            assert sorted(m["test_order"] for m in t["motors"]) == list(
                range(1, counts[c["name"]] + 1)
            )


def test_factors_are_normalised_as_the_mixer_leaves_them(frames: dict[str, Any]) -> None:
    for c in frames.values():
        for t in c["types"].values():
            for axis in ("roll", "pitch", "yaw"):
                top = max(abs(m[axis]) for m in t["motors"])
                assert top == pytest.approx(0.5) or top == 0, (c["name"], t["name"], axis)
            assert max(m["throttle"] for m in t["motors"]) == pytest.approx(1.0)


def test_symmetric_frames_balance_on_every_axis(frames: dict[str, Any]) -> None:
    for c in frames.values():
        for t in c["types"].values():
            if t["name"] not in SYMMETRIC or (c["name"] == "OCTAQUAD" and "COR" in t["name"]):
                continue
            for axis in ("roll", "pitch", "yaw"):
                assert sum(m[axis] for m in t["motors"]) == pytest.approx(0, abs=1e-5), (
                    c["name"], t["name"], axis,
                )  # fmt: skip


def test_a_hexa_x_has_its_side_motors_on_roll_alone(frames: dict[str, Any]) -> None:
    rows = factors(frames, 2, 1)
    assert rows[0][:2] == (-0.5, 0.0) and rows[1][:2] == (0.5, 0.0)  # 90 and -90 degrees
    # -30 degrees: roll cos(60) of the side motors' 1, pitch cos(30) of the largest cos(30)
    assert rows[2][:2] == (round(0.5 * math.cos(math.radians(60)), 6), 0.5)


def test_a_co_rotating_x8_scales_its_top_layer(frames: dict[str, Any]) -> None:
    rows = factors(frames, 4, 20)  # OCTAQUAD X_COR: the top four by 0.9
    assert [r[3] for r in rows] == [0.9] * 4 + [1.0] * 4
    assert all(abs(r[0]) == pytest.approx(0.45) for r in rows[:4])


def test_plane_only_frames_are_marked(frames: dict[str, Any]) -> None:
    marked = {(c["name"], t["name"]) for c in frames.values() for t in c["types"].values()
              if t.get("vehicles") == ["plane"]}  # fmt: skip
    assert marked == {("QUAD", "NYT_PLUS"), ("QUAD", "NYT_X")}
    assert all(m["yaw"] == 0 for m in frames["1"]["types"]["17"]["motors"])  # no yaw torque


def test_a_class_s_default_layout_is_kept(frames: dict[str, Any]) -> None:
    assert frames["5"]["types"]["default"]["name"] == "default"  # Y6 with any other type


def test_later_releases_have_the_same_frames() -> None:
    pack = load_pack()
    assert pack.frames("4.7.1") == pack.frames("4.7.2") == pack.frames("4.7.0")
    assert pack.frames("4.8.0") == pack.frames("4.7.2")  # the nearest earlier release


# --- the generator ---------------------------------------------------------------------------

HEADERS = {
    "libraries/AP_Motors/AP_Motors_Class.h": """
#define AP_MOTORS_MOT_1 0U
#define AP_MOTORS_MOT_2 1U
#define AP_MOTORS_MOT_3 2U
#define AP_MOTORS_MOT_4 3U
class AP_Motors {
    enum motor_frame_class {
        MOTOR_FRAME_UNDEFINED = 0,
        MOTOR_FRAME_QUAD = 1,
    };
    enum motor_frame_type {
        MOTOR_FRAME_TYPE_PLUS = 0,
        MOTOR_FRAME_TYPE_X = 1,
        MOTOR_FRAME_TYPE_VTAIL = 4,
    };
};
""",
    "libraries/AP_Motors/AP_MotorsMatrix.h": """
#define AP_MOTORS_MATRIX_YAW_FACTOR_CW   -1
#define AP_MOTORS_MATRIX_YAW_FACTOR_CCW   1
""",
    "libraries/AP_Motors/AP_Motors_config.h": (
        "#define AP_MOTORS_FRAME_OCTAQUAD_COROTATING_SCALE_FACTOR 0.9\n"
    ),
}
MIXER = """
bool AP_MotorsMatrix::setup_quad_matrix(motor_frame_type frame_type)
{
    _frame_class_string = "QUAD";
    switch (frame_type) {
    case MOTOR_FRAME_TYPE_X: {
        _frame_type_string = "X";  // a comment
        static const AP_MotorsMatrix::MotorDef motors[] {
            {   45, AP_MOTORS_MATRIX_YAW_FACTOR_CCW,  1 },
            { -135, AP_MOTORS_MATRIX_YAW_FACTOR_CCW,  3 },
            {  -45, AP_MOTORS_MATRIX_YAW_FACTOR_CW,   4 },
            {  135, AP_MOTORS_MATRIX_YAW_FACTOR_CW,   2 },
        };
        add_motors(motors, ARRAY_SIZE(motors));
        break;
    }
    case MOTOR_FRAME_TYPE_VTAIL:
        _frame_type_string = "VTAIL";
        add_motor(AP_MOTORS_MOT_1, 60, 60, 0, 1);
        add_motor(AP_MOTORS_MOT_2, 0, -160, AP_MOTORS_MATRIX_YAW_FACTOR_CW, 3);
        add_motor(AP_MOTORS_MOT_3, -60, -60, 0, 4);
        add_motor(AP_MOTORS_MOT_4, 0, 160, AP_MOTORS_MATRIX_YAW_FACTOR_CCW, 2);
        break;
    default:
        return false;
    }
    return true;
}

void AP_MotorsMatrix::setup_motors(motor_frame_class frame_class, motor_frame_type frame_type)
{
    switch (frame_class) {
    case MOTOR_FRAME_QUAD:
        success = setup_quad_matrix(frame_type);
        break;
    }
}
"""


def tree(root: Path, mixer: str) -> Path:
    for rel, text in HEADERS.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    (root / "libraries/AP_Motors/AP_MotorsMatrix.cpp").write_text(mixer, encoding="utf-8")
    return root


def test_the_generator_replays_the_mixer(tmp_path: Path) -> None:
    out = tool().build(tree(tmp_path, MIXER))
    quad = out["1"]["types"]
    assert set(quad) == {"1", "4"}  # PLUS falls to the default case, which refuses it
    assert [m["roll"] for m in quad["1"]["motors"]] == [-0.5, 0.5, 0.5, -0.5]
    vtail = quad["4"]["motors"]
    assert [m["yaw"] for m in vtail] == [0.0, -0.5, 0.0, 0.5]  # yaw from the rear motors
    assert [m["roll"] for m in vtail] == [-0.5, 0.0, 0.5, 0.0]  # roll from the front


def test_the_generator_refuses_what_it_cannot_read(tmp_path: Path) -> None:
    built = tool()
    changed = MIXER.replace("        break;\n    }\n    case MOTOR_FRAME_TYPE_VTAIL",
                            "        _yaw_factor[0] = 0.25f;\n        break;\n    }\n"
                            "    case MOTOR_FRAME_TYPE_VTAIL", 1)  # fmt: skip
    with pytest.raises(built.FrameError, match="cannot read"):
        built.build(tree(tmp_path, changed))
