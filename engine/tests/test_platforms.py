"""Platform setup, airframes and build versions."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from flight_insights.knowledge import Pack, load_pack
from flight_insights.platforms import (
    Schema,
    SetupError,
    airframe_view,
    check_value,
    create_airframe,
    create_platform,
    detect,
    list_airframes,
    load_schema,
    platform_view,
    record_change,
    save_answers,
)
from flight_insights.store import Store, StoreError, StoreKeys, open_store

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
LAPTOP = "d" * 32
STEPS = [
    "platform", "flight_controller", "propulsion", "power", "navigation", "links",
    "payload", "weights", "review", "first_params",
]  # fmt: skip
DETECTABLE = {
    "vehicle", "frame_class", "frame_type", "motor_count", "board", "firmware",
    "esc_protocol", "esc_rpm_telemetry", "cells", "capacity_mah", "gnss_count", "rtk",
    "compass_count", "rangefinder", "optical_flow", "visual_odometry", "rc_protocol",
}  # fmt: skip


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


@pytest.fixture(scope="module")
def schema(pack: Pack) -> Schema:
    return load_schema(pack)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with open_store(tmp_path / "fleet.db", StoreKeys.generate(), LAPTOP) as s:
        yield s


def every_required_answer(schema: Schema) -> dict[str, Any]:
    examples = {"text": "x", "source": "file-1", "number": 1.0, "integer": 4, "boolean": True}
    out: dict[str, Any] = {}
    for f in schema.fields.values():
        if f.required and f.id not in ("name", "vehicle"):
            out[f.id] = f.choices[0] if f.type == "choice" else examples[f.type]
    return out


# --- the schema -----------------------------------------------------------------------


def test_the_setup_walks_the_spec_steps_in_order(schema: Schema) -> None:
    assert list(schema.steps) == STEPS


def test_every_field_explains_itself_in_every_language(schema: Schema) -> None:
    for step in schema.raw["steps"]:
        assert step["title"]["en"] and step["title"]["uk"] and step["about"]["uk"]
        for f in step["fields"]:
            assert f["label"]["en"] and f["label"]["uk"], f["id"]
            assert f["why"]["en"] and f["why"]["uk"], f["id"]
            if f["required"]:
                assert f["prevents"]["en"] and f["prevents"]["uk"], f["id"]
            assert f["type"] in ("text", "number", "integer", "boolean", "choice", "source")
            assert (f["type"] == "choice") == bool(f.get("choices")), f["id"]
            assert f.get("detect") is None or f["detect"] in DETECTABLE, f["id"]


@pytest.mark.parametrize(
    ("field", "value", "stored"),
    [
        ("motor_count", 8, 8),
        ("motor_count", 8.0, 8),
        ("prop_diameter_in", 28, 28.0),
        ("directions_checked", True, True),
        ("payload", "variable", "variable"),
        ("name", "  Heavy-lift X8 ", "Heavy-lift X8"),
        ("name", "   ", None),
        ("motor_kv", None, None),
    ],
)
def test_values_are_checked_and_normalised(
    schema: Schema, field: str, value: Any, stored: Any
) -> None:
    assert check_value(schema.field(field), value) == stored


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("motor_count", 7.5),
        ("motor_count", True),
        ("prop_diameter_in", "28 in"),
        ("prop_diameter_in", float("nan")),
        ("payload", "sometimes"),
        ("directions_checked", "yes"),
        ("name", "x" * 501),
    ],
)
def test_bad_values_are_refused(schema: Schema, field: str, value: Any) -> None:
    with pytest.raises(SetupError):
        check_value(schema.field(field), value)


# --- platforms ------------------------------------------------------------------------


def test_a_new_platform_lists_what_each_gap_prevents(store: Store, schema: Schema) -> None:
    pid = create_platform(store, schema, "Heavy-lift X8", "copter")
    view = platform_view(store, schema, pid)
    assert view["name"] == "Heavy-lift X8" and view["vehicle"] == "copter"
    c = view["completeness"]
    assert not c["ready"] and 0 < c["percent"] < 100
    gaps = {g["field"]: g for g in c["gaps"]}
    assert gaps["prop_pitch_in"]["prevents"] == "Thrust model starting values"
    assert gaps["esc_temp_protection_c"]["step"] == "propulsion"
    assert c["steps"]["platform"] == "started" and c["steps"]["power"] == "not started"


def test_answering_every_required_field_makes_the_platform_ready(
    store: Store, schema: Schema
) -> None:
    pid = create_platform(store, schema, "VTOL mapper", "plane")
    build = platform_view(store, schema, pid)["reference_build"]["version"]
    view = save_answers(store, schema, pid, build, every_required_answer(schema))
    assert view["completeness"]["ready"] and view["completeness"]["percent"] == 100
    steps = view["completeness"]["steps"]
    assert set(steps.values()) <= {"done", "optional"} and steps["links"] == "optional"
    assert view["spec"]["motor_kv"] == {"value": 1.0, "source": "entered"}


def test_saves_are_checked_against_the_version_read(store: Store, schema: Schema) -> None:
    pid = create_platform(store, schema, "Quad", "copter")
    build = platform_view(store, schema, pid)["reference_build"]["version"]
    save_answers(store, schema, pid, build, {"motor_kv": 900})
    with pytest.raises(StoreError):
        save_answers(store, schema, pid, build, {"motor_kv": 950})  # someone else saved first
    with pytest.raises(SetupError, match="fixed"):
        save_answers(store, schema, pid, build + 1, {"vehicle": "plane"})
    renamed = save_answers(store, schema, pid, build + 1, {"name": "Quad mk2"})
    assert renamed["name"] == "Quad mk2"


# --- detection ------------------------------------------------------------------------


def test_a_parameter_file_prefills_the_frame_and_sensors(pack: Pack) -> None:
    found = detect(FIXTURES / "copter-quad-hover.param", pack)
    values = {k: v["value"] for k, v in found.items()}
    assert values["vehicle"] == "copter"
    assert (values["frame_class"], values["motor_count"]) == ("Quad", 4)
    assert values["esc_protocol"] == "Normal" and values["esc_rpm_telemetry"] is False
    assert values["gnss_count"] == 1 and values["compass_count"] == 3
    assert all(v["source"].startswith("detected: ") for v in found.values())


def test_a_bench_log_also_gives_board_firmware_and_battery(pack: Pack) -> None:
    found = detect(FIXTURES / "heli-hover.bin", pack)
    values = {k: v["value"] for k, v in found.items()}
    assert values["frame_class"] == "Heli" and values["motor_count"] == 1
    assert "frame_type" not in values  # a helicopter has no motor layout
    assert (values["board"], values["firmware"], values["cells"]) == ("SITL", "4.7.0", 3)
    assert values["esc_rpm_telemetry"] is True and values["rtk"] is True


def test_a_quadplane_is_read_from_its_q_frame(pack: Pack) -> None:
    found = detect(FIXTURES / "quadplane-hover.params", pack)
    assert found["vehicle"]["value"] == "plane"
    assert (found["frame_class"]["value"], found["motor_count"]["value"]) == ("Quad", 4)
    assert "Q_FRAME_CLASS" in found["frame_class"]["source"]


def test_detected_answers_can_be_saved(store: Store, schema: Schema, pack: Pack) -> None:
    pid = create_platform(store, schema, "Quad", "copter")
    build = platform_view(store, schema, pid)["reference_build"]["version"]
    found = detect(FIXTURES / "copter-quad-hover.param", pack)
    values = {k: v["value"] for k, v in found.items() if k != "vehicle"}
    view = save_answers(store, schema, pid, build, values, source="detected")
    assert view["spec"]["motor_count"] == {"value": 4, "source": "detected"}


def test_an_unrecognisable_file_is_refused(tmp_path: Path, pack: Pack) -> None:
    f = tmp_path / "other.param"
    f.write_text("SOME_PARAM,1\n")
    with pytest.raises(SetupError, match="copter or a plane"):
        detect(f, pack)


# --- airframes and builds -------------------------------------------------------------


def test_airframes_inherit_the_reference_and_record_changes(store: Store, schema: Schema) -> None:
    pid = create_platform(store, schema, "Heavy-lift X8", "copter")
    build = platform_view(store, schema, pid)["reference_build"]["version"]
    save_answers(store, schema, pid, build, {"prop_diameter_in": 28, "motor_kv": 100})
    aid = create_airframe(store, pid, "HL-07")
    first = airframe_view(store, schema, aid)
    assert first["spec"]["prop_diameter_in"]["value"] == 28.0
    assert first["differs_from_reference"] == [] and first["build"]["number"] == 1

    changed = record_change(store, schema, aid, {"prop_diameter_in": 30}, "upgrade", "new props")
    assert changed["build"]["number"] == 2
    assert changed["spec"]["prop_diameter_in"]["value"] == 30.0
    assert changed["spec"]["motor_kv"]["value"] == 100.0  # still inherited
    assert changed["differs_from_reference"] == ["prop_diameter_in"]
    assert [(h["number"], h["reason_kind"]) for h in changed["history"]] == [
        (1, "setup"),
        (2, "upgrade"),
    ]
    back = record_change(store, schema, aid, {"prop_diameter_in": None}, "test")
    assert back["spec"]["prop_diameter_in"]["value"] == 28.0
    assert back["differs_from_reference"] == []
    # the build an earlier flight flew with is untouched
    assert airframe_view(store, schema, aid)["history"][1]["changed"] == ["prop_diameter_in"]


def test_bad_changes_are_refused(store: Store, schema: Schema) -> None:
    pid = create_platform(store, schema, "Quad", "copter")
    aid = create_airframe(store, pid, "Q-1")
    with pytest.raises(SetupError):
        record_change(store, schema, aid, {"prop_diameter_in": 5}, "because")  # type: ignore[arg-type]
    with pytest.raises(SetupError):
        record_change(store, schema, aid, {}, "upgrade")
    with pytest.raises(SetupError):
        record_change(store, schema, aid, {"no_such_field": 1}, "upgrade")
    with pytest.raises(ValueError):
        record_change(store, schema, aid, {"motor_kv": 1}, "upgrade", effective_date="tomorrow")
    with pytest.raises(SetupError):
        create_airframe(store, pid, "  ")
    assert [a["name"] for a in list_airframes(store, schema, pid)] == ["Q-1"]


# --- over JSON-RPC --------------------------------------------------------------------


def test_setup_over_rpc(tmp_path: Path, pack: Pack) -> None:
    from test_rpc import Output, call

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, pack)
    keys = {k: secrets.token_hex(32) for k in ("database", "cache", "positions", "board_id")}
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys)
    assert [s["id"] for s in call(server, out, 2, "setup.schema")["result"]["steps"]] == STEPS
    platform = call(server, out, 3, "platform.create", name="Quad", vehicle="copter")["result"]
    found = call(server, out, 4, "platform.detect", path=str(FIXTURES / "copter-quad-hover.param"))
    assert found["result"]["motor_count"]["value"] == 4
    version = platform["reference_build"]["version"]
    saved = call(server, out, 5, "platform.save", id=platform["id"], build_version=version,
                 values={"motor_count": 4})["result"]  # fmt: skip
    assert saved["spec"]["motor_count"]["value"] == 4
    stale = call(server, out, 6, "platform.save", id=platform["id"], build_version=version,
                 values={"motor_count": 6})  # fmt: skip
    assert stale["error"]["code"] == -32004
    bad = call(server, out, 7, "platform.save", id=platform["id"], build_version=version + 1,
               values={"motor_count": "eight"})  # fmt: skip
    assert bad["error"]["code"] == -32003
    airframe = call(server, out, 8, "airframe.create", platform_id=platform["id"], name="Q-1")
    aid = airframe["result"]["id"]
    changed = call(server, out, 9, "airframe.record_change", id=aid, changes={"motor_kv": 920},
                   reason_kind="upgrade", reason="new motors")["result"]  # fmt: skip
    assert changed["build"]["number"] == 2
    listed = call(server, out, 10, "platform.list")["result"]
    assert [p["airframes"] for p in listed] == [1]
    json.dumps(listed)  # plain JSON all the way through
    call(server, out, 11, "engine.shutdown")
