"""Ground station profiles: the schema, versions, this computer's facts, installed
software and the RPC methods."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import pytest

from flight_insights.knowledge import Pack, load_pack
from flight_insights.platforms import Schema, SetupError, load_schema
from flight_insights.stations import (
    FactsError,
    Installed,
    computer_answers,
    create_station,
    hash_host,
    installed_software,
    list_stations,
    record_computer,
    rename_station,
    save_profile,
    software_answers,
    station_for_host,
    station_view,
)
from flight_insights.store import Store, StoreError, StoreKeys, open_store

LAPTOP = "e" * 32
KEY = bytes(range(32))
PARTS = ["computer", "software", "connections", "radios", "antennas", "clock"]
FACTS: dict[str, Any] = {
    "os": "Windows", "os_version": "11 (26100)", "cpu": "Intel Core i7-1365U",
    "cpu_cores": 10, "cpu_mhz": 1800.0, "memory_bytes": 32 * 1024**3,
    "disk_bytes": 512 * 1024**3, "disk_free_bytes": 201.37 * 1024**3, "power": "battery",
    "time_sync": True, "time_source": "time.windows.com",
}  # fmt: skip
MP = [Installed("Mission Planner", "1.3.82", "Windows uninstall registry")]


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


@pytest.fixture(scope="module")
def schema(pack: Pack) -> Schema:
    return load_schema(pack, "station")


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with open_store(tmp_path / "fleet.db", StoreKeys.generate(), LAPTOP) as s:
        yield s


def fly_with(store: Store, version_id: str) -> None:
    """A file recorded with this version, which freezes it."""
    store.insert(
        "source_file",
        {"sha256": secrets.token_hex(32), "kind": "tlog", "size": 1, "original_name": "a.tlog",
         "ground_station_version_id": version_id},
    )  # fmt: skip


def detect(store: Store, schema: Schema, sid: str, host: str = "GCS-LAPTOP", **facts: Any):
    view = station_view(store, sid)
    return record_computer(store, schema, sid, view["version"], FACTS | facts, MP,
                           hash_host(KEY, host))  # fmt: skip


# --- the schema -----------------------------------------------------------------------


def test_the_profile_has_every_part_in_every_language(schema: Schema) -> None:
    assert list(schema.steps) == PARTS
    assert schema.repeating == {"radios", "antennas"}
    for step in schema.raw["steps"]:
        assert step["title"].keys() == {"en", "uk"}
        for f in step["fields"]:
            assert f["label"].keys() == {"en", "uk"} and f["why"].keys() == {"en", "uk"}
            if f.get("required"):
                assert f["prevents"].keys() == {"en", "uk"}


def test_every_detected_field_can_be_detected(schema: Schema) -> None:
    detected = {f.id for f in schema.fields.values() if f.detect}
    answers = computer_answers(FACTS)
    produced = set(answers["computer"]) | set(answers["clock"]) | set(software_answers(MP))
    assert detected == produced


def test_only_minute_to_minute_values_are_volatile(schema: Schema) -> None:
    volatile = {f.id for f in schema.fields.values() if f.volatile}
    assert volatile == {"cpu_mhz", "disk_free_gb", "power", "time_sync"}


# --- versions -------------------------------------------------------------------------


def test_a_station_is_set_up_in_place_until_it_is_used(store: Store, schema: Schema) -> None:
    sid = create_station(store, "  Field laptop ")
    view = station_view(store, sid)
    assert view["name"] == "Field laptop" and view["current"]["number"] == 1
    view = save_profile(store, schema, sid, view["version"],
                        {"software": {"gcs": "QGroundControl"},
                         "radios": [{"radio_role": "telemetry", "radio_model": "RFD900x",
                                     "radio_air_rate": 64}]})  # fmt: skip
    view = save_profile(
        store,
        schema,
        sid,
        view["version"],
        {"antennas": [{"antenna_model": "RFD patch", "antenna_gain_dbi": 9.0}]},
    )
    current = view["current"]
    assert current["number"] == 1 and [h["number"] for h in view["history"]] == [1]
    assert current["profile"]["software"]["gcs"] == {"value": "QGroundControl", "source": "entered"}
    assert current["profile"]["radios"][0]["radio_air_rate"]["value"] == 64
    assert current["profile"]["antennas"][0]["antenna_gain_dbi"]["value"] == 9.0

    fly_with(store, current["id"])
    with pytest.raises(SetupError, match="record a change"):
        save_profile(store, schema, sid, view["version"], {"clock": {"time_source": "GPS"}})
    view = save_profile(store, schema, sid, view["version"],
                        {"antennas": [{"antenna_model": "Helical 5.8", "antenna_type": "helical"}]},
                        "failure", "patch cracked")  # fmt: skip
    assert [h["number"] for h in view["history"]] == [1, 2]
    assert view["history"][1]["reason"] == "patch cracked"
    first = store.get("ground_station_version", view["history"][0]["id"])
    assert first is not None and "RFD patch" in first["antennas_json"]  # flown version kept


def test_a_change_without_a_reason_text_says_what_changed(store: Store, schema: Schema) -> None:
    sid = create_station(store, "GCS")
    view = save_profile(store, schema, sid, 2, {"connections": {"baud": 57600}})
    fly_with(store, view["current"]["id"])
    view = save_profile(store, schema, sid, view["version"], {"connections": {"baud": 115200}},
                        "upgrade")  # fmt: skip
    assert view["history"][-1]["reason"] == "baud 57600 → 115200"


def test_saving_the_same_answers_changes_nothing(store: Store, schema: Schema) -> None:
    sid = create_station(store, "GCS")
    view = save_profile(store, schema, sid, 2, {"connections": {"link_type": "serial"}})
    again = save_profile(store, schema, sid, view["version"],
                         {"connections": {"link_type": "serial"}}, "upgrade")  # fmt: skip
    assert again["version"] == view["version"] and len(again["history"]) == 1


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"radios": [{"antenna_model": "x"}]}, "belongs in antennas"),
        ({"radios": {"radio_model": "x"}}, "must be a list"),
        ({"radios": ["RFD900x"]}, "must be an object"),
        ({"computer": [{"os": "x"}]}, "must be an object"),
        ({"links": {}}, "no profile part"),
        ({"connections": {"baud": 57600.5}}, "whole number"),
        ({"antennas": [{"antenna_type": "dish"}]}, "one of"),
    ],
)
def test_bad_answers_are_refused(
    store: Store, schema: Schema, changes: dict[str, Any], message: str
) -> None:
    sid = create_station(store, "GCS")
    with pytest.raises(SetupError, match=message):
        save_profile(store, schema, sid, 2, changes)


def test_saves_are_checked_against_the_version_read(store: Store, schema: Schema) -> None:
    sid = create_station(store, "GCS")
    save_profile(store, schema, sid, 2, {"connections": {"baud": 57600}})
    with pytest.raises(StoreError):
        save_profile(store, schema, sid, 2, {"connections": {"baud": 115200}})
    with pytest.raises(StoreError):
        rename_station(store, sid, 2, "GCS 2")
    assert rename_station(store, sid, 3, "GCS 2")["name"] == "GCS 2"
    assert [v["name"] for v in list_stations(store)] == ["GCS 2"]


# --- this computer --------------------------------------------------------------------


def test_this_computers_facts_fill_the_computer_and_clock(store: Store, schema: Schema) -> None:
    sid = create_station(store, "GCS")
    view = detect(store, schema, sid)
    profile = view["current"]["profile"]
    assert profile["computer"]["memory_gb"] == {"value": 32.0, "source": "detected: this computer"}
    assert profile["computer"]["disk_free_gb"]["value"] == 201.4
    assert profile["clock"]["time_sync"]["value"] == "synchronised"
    assert profile["software"]["gcs"]["value"] == "Mission Planner"
    assert profile["software"]["gcs_version"]["value"] == "1.3.82"
    assert view["this_computer"] and view["current"]["computer_known"]
    assert view["current"]["number"] == 1  # still being set up
    assert station_for_host(store, hash_host(KEY, "gcs-laptop ") or "") == sid


def test_minute_to_minute_values_never_make_a_version(store: Store, schema: Schema) -> None:
    sid = create_station(store, "GCS")
    fly_with(store, detect(store, schema, sid)["current"]["id"])
    view = detect(store, schema, sid, disk_free_bytes=5 * 1024**3, cpu_mhz=3900, power="mains",
                  time_sync=False)  # fmt: skip
    assert view["current"]["number"] == 1
    assert view["current"]["profile"]["computer"]["disk_free_gb"]["value"] == 5.0


def test_a_system_update_makes_a_detected_version(store: Store, schema: Schema) -> None:
    sid = create_station(store, "GCS")
    fly_with(store, detect(store, schema, sid)["current"]["id"])
    view = detect(store, schema, sid, os_version="11 (26200)")
    last = view["history"][-1]
    assert (last["number"], last["reason_kind"]) == (2, "detected")
    assert last["reason"] == "os_version 11 (26100) → 11 (26200)"


def test_another_computer_replaces_the_computer_part(store: Store, schema: Schema) -> None:
    sid = create_station(store, "GCS")
    view = detect(store, schema, sid)
    view = save_profile(store, schema, sid, view["version"],
                        {"computer": {"make_model": "Toughbook 40"}})  # fmt: skip
    fly_with(store, view["current"]["id"])
    view = detect(store, schema, sid, host="SPARE-LAPTOP")
    assert view["history"][-1]["reason"] == "detected on a different computer"
    assert "make_model" not in view["current"]["profile"]["computer"]
    assert view["this_computer"]
    assert station_view(store, sid, hash_host(KEY, "GCS-LAPTOP"))["this_computer"] is False


def test_a_persons_choice_of_program_stands(store: Store, schema: Schema) -> None:
    sid = create_station(store, "GCS")
    save_profile(store, schema, sid, 2, {"software": {"gcs": "QGroundControl"}})
    software = detect(store, schema, sid)["current"]["profile"]["software"]
    assert software["gcs"] == {"value": "QGroundControl", "source": "entered"}
    assert "gcs_version" not in software  # 1.3.82 is Mission Planner's, not QGroundControl's
    assert software["installed_gcs"]["value"] == "Mission Planner 1.3.82"


def test_the_host_name_is_kept_only_as_a_keyed_hash(store: Store, schema: Schema) -> None:
    a, b = hash_host(KEY, "GCS-LAPTOP"), hash_host(bytes(32), "GCS-LAPTOP")
    assert a is not None and b is not None and a != b and a.startswith("host1-")
    assert hash_host(KEY, " gcs-laptop") == a and hash_host(KEY, "  ") is None
    sid = create_station(store, "GCS")
    detect(store, schema, sid)
    for row in store.conn.execute("SELECT * FROM ground_station_version"):
        assert not any("GCS-LAPTOP".casefold() in str(v).casefold() for v in row)
    with pytest.raises(ValueError, match="32 bytes"):
        hash_host(b"short", "x")


@pytest.mark.parametrize(
    "bad",
    [{"memory_bytes": -1}, {"memory_bytes": "32 GB"}, {"cpu_cores": True},
     {"power": "solar"}, {"time_sync": "yes"}, {"os": 11}, {"disk_bytes": float("nan")}],
)  # fmt: skip
def test_bad_facts_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(FactsError):
        computer_answers(FACTS | bad)


def test_facts_the_shell_could_not_read_are_left_out() -> None:
    answers = computer_answers({"os": "Linux", "cpu_mhz": 0, "power": None, "time_source": ""})
    assert answers == {"computer": {"os": "Linux"}, "clock": {}}


# --- installed software ---------------------------------------------------------------


def test_programs_are_found_in_the_windows_registry() -> None:
    registry = [("Mission Planner", "1.3.9"), ("Mission Planner", "1.3.82"),
                ("QGroundControl", ""), ("Notepad++", "8.6")]  # fmt: skip
    found = installed_software("win32", registry=registry)
    assert [str(i) for i in found] == ["Mission Planner 1.3.9", "Mission Planner 1.3.82",
                                       "QGroundControl"]  # fmt: skip
    answers = software_answers(found)
    assert "gcs" not in answers  # two programs: a person picks
    only_mp = software_answers(found[:2])
    assert only_mp["gcs_version"]["value"] == "1.3.82"  # the newest, by number


def test_programs_are_found_on_linux(tmp_path: Path) -> None:
    home, system = tmp_path / "home", tmp_path / "root"
    info = home / ".local/share/flatpak/app/org.mavlink.qgroundcontrol/current/active/files/share"
    (info / "metainfo").mkdir(parents=True)
    (info / "metainfo" / "org.mavlink.qgroundcontrol.metainfo.xml").write_text(
        '<component><releases><release version="5.0.6" date="2025-06-01"/></releases></component>',
        encoding="utf-8",
    )
    (home / "Applications").mkdir()
    (home / "Applications" / "QGroundControl-4.4.2-x86_64.AppImage").write_bytes(b"")
    (system / "opt/MissionPlanner").mkdir(parents=True)
    (system / "opt/MissionPlanner/MissionPlanner.exe").write_bytes(b"")
    found = installed_software("linux", home=home, system=system)
    assert {(i.name, i.version, i.where) for i in found} == {
        ("QGroundControl", "5.0.6", "Flatpak, user"),
        ("QGroundControl", "4.4.2", "AppImage QGroundControl-4.4.2-x86_64.AppImage"),
        ("Mission Planner", None, "Mission Planner under Mono"),
    }
    assert installed_software("linux", home=tmp_path / "empty", system=tmp_path / "none") == []
    assert software_answers([]) == {}


# --- over RPC -------------------------------------------------------------------------


def test_stations_over_rpc(tmp_path: Path, pack: Pack) -> None:
    from test_rpc import Output, call

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, pack)
    keys = {k: secrets.token_hex(32) for k in ("database", "cache", "positions", "board_id")}
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys,
         host="GCS-LAPTOP")  # fmt: skip
    assert [s["id"] for s in call(server, out, 2, "station.schema")["result"]["steps"]] == PARTS
    station = call(server, out, 3, "station.create", name="Field laptop")["result"]
    assert station["this_computer"] is False
    facts = {k: v for k, v in FACTS.items() if k != "power"}
    found = call(server, out, 4, "station.record_computer", id=station["id"],
                 version=station["version"], facts=facts)["result"]  # fmt: skip
    assert found["this_computer"] and found["current"]["profile"]["computer"]["cpu_cores"]["value"]
    saved = call(server, out, 5, "station.save", id=station["id"], version=found["version"],
                 profile={"radios": [{"radio_role": "RC", "radio_model": "TX16S ELRS"}]},
                 detected=False)["result"]  # fmt: skip
    assert saved["current"]["profile"]["radios"][0]["radio_model"]["value"] == "TX16S ELRS"
    stale = call(server, out, 6, "station.save", id=station["id"], version=found["version"],
                 profile={"clock": {"time_source": "GPS"}})  # fmt: skip
    assert stale["error"]["code"] == -32004
    bad = call(server, out, 7, "station.save", id=station["id"], version=saved["version"],
               profile={"clock": {"time_sync": "maybe"}})  # fmt: skip
    assert bad["error"]["code"] == -32003
    odd = call(server, out, 8, "station.save", id=station["id"], version=saved["version"],
               profile={}, sources={"clock": {"time_source": 3}})  # fmt: skip
    assert odd["error"]["code"] == -32602
    software = call(server, out, 9, "station.detect_software")["result"]
    assert isinstance(software, dict)
    listed = call(server, out, 10, "station.list")["result"]
    assert [s["name"] for s in listed] == ["Field laptop"] and listed[0]["this_computer"]
    renamed = call(server, out, 11, "station.rename", id=station["id"],
                   version=saved["version"], name="Van")["result"]  # fmt: skip
    assert renamed["name"] == "Van"
    call(server, out, 12, "engine.shutdown")
