"""Watched save folders: which folders, and which files found there are new."""

from __future__ import annotations

import secrets
import shutil
from pathlib import Path
from typing import Any

import pytest

from flight_insights.imports import watch
from flight_insights.platforms import SetupError
from flight_insights.stations import add_folder, create_station, list_folders, remove_folder
from flight_insights.store import Store, StoreKeys, open_store
from flight_insights.store.register import register_source

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
LAPTOP = "a" * 32


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with open_store(tmp_path / "fleet.db", StoreKeys.generate(), LAPTOP) as s:
        yield s


def test_folders_are_kept_per_station_and_laptop(store: Store, tmp_path: Path) -> None:
    sid = create_station(store, "Field tablet")
    mp = tmp_path / "Mission Planner" / "logs"
    mp.mkdir(parents=True)
    fid = add_folder(store, sid, str(mp), "Mission Planner")
    (f,) = list_folders(store)
    assert (f["path"], f["software"], f["station"]) == (str(mp), "Mission Planner", "Field tablet")
    assert f["station_version_id"] is not None
    with pytest.raises(SetupError, match="already watched"):
        add_folder(store, sid, str(mp))
    with pytest.raises(SetupError, match="existing folder"):
        add_folder(store, sid, str(tmp_path / "missing"))
    with pytest.raises(SetupError, match="existing folder"):
        add_folder(store, sid, "relative/path")
    with pytest.raises(SetupError, match="software"):
        add_folder(store, sid, str(tmp_path), "Notepad")
    remove_folder(store, fid)
    assert list_folders(store) == []
    with pytest.raises(SetupError):
        remove_folder(store, fid)
    add_folder(store, sid, str(mp))  # watched again after being removed


def test_only_new_files_are_offered(store: Store, tmp_path: Path) -> None:
    for name in ("copter-quad-hover.bin", "copter-quad-hover.tlog", "copter-quad-hover.param"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    (tmp_path / "notes.txt").write_text("not a log", encoding="utf-8")
    paths = sorted(tmp_path.iterdir())
    assert {p.name for p in watch.unknown(store, paths)} == {
        "copter-quad-hover.bin",
        "copter-quad-hover.tlog",
        "copter-quad-hover.param",
    }
    register_source(store, tmp_path / "copter-quad-hover.bin", "dataflash")
    assert watch.ignore(store, [tmp_path / "copter-quad-hover.param"]) == 1
    assert watch.ignore(store, [tmp_path / "copter-quad-hover.param"]) == 0
    assert [p.name for p in watch.unknown(store, paths)] == ["copter-quad-hover.tlog"]
    renamed = tmp_path / "renamed.param"
    shutil.copy(tmp_path / "copter-quad-hover.param", renamed)
    assert watch.unknown(store, [renamed]) == []  # known by content, not by name


def test_a_locked_file_is_left_for_the_next_look(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("copter-quad-hover.bin", "copter-quad-hover.tlog"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    paths = sorted(tmp_path.iterdir())
    locked, read = tmp_path / "copter-quad-hover.tlog", watch.file_sha256

    def sha256(path: Path) -> str:
        if path == locked:  # Mission Planner still writing it, on Windows
            raise PermissionError(13, "used by another process", str(path))
        return read(path)

    monkeypatch.setattr(watch, "file_sha256", sha256)
    assert [p.name for p in watch.unknown(store, paths)] == ["copter-quad-hover.bin"]
    monkeypatch.undo()
    assert len(watch.unknown(store, paths)) == 2


def test_folders_over_rpc(tmp_path: Path) -> None:
    from test_rpc import Output, call

    from flight_insights.knowledge import load_pack
    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, load_pack())
    keys = {k: secrets.token_hex(32) for k in ("database", "cache", "positions", "board_id")}
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path / "d"), keys=keys)
    station = call(server, out, 2, "station.create", name="Field tablet")["result"]
    folder = tmp_path / "QGroundControl" / "Telemetry"
    folder.mkdir(parents=True)
    added = call(
        server,
        out,
        3,
        "station.add_folder",
        id=station["id"],
        path=str(folder),
        software="QGroundControl",
    )["result"]
    assert [f["path"] for f in added] == [str(folder)]
    shutil.copy(FIXTURES / "copter-quad-hover.tlog", folder / "a.tlog")
    new = call(server, out, 4, "import.unknown", paths=[str(folder / "a.tlog")])["result"]
    assert new == [str(folder / "a.tlog")]
    assert call(server, out, 5, "import.ignore", paths=new)["result"] == {"ignored": 1}
    assert call(server, out, 6, "import.unknown", paths=new)["result"] == []
    bad = call(server, out, 7, "station.add_folder", id=station["id"], path="nowhere")
    assert bad["error"]["code"] == -32003
    call(server, out, 8, "station.remove_folder", folder_id=added[0]["id"])
    assert call(server, out, 9, "station.folders")["result"] == []
    call(server, out, 10, "engine.shutdown")
