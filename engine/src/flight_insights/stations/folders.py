"""The save folders the shell watches for each ground station (spec: Ground
station profiles; setup step 5). Folders belong to this laptop: a path means
something only on the computer that has it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flight_insights.platforms.schema import SetupError
from flight_insights.store.db import Store

SOFTWARE = ("Mission Planner", "QGroundControl", "other")


def add_folder(store: Store, station_id: str, path: str, software: str | None = None) -> str:
    if store.get("ground_station", station_id) is None:
        raise SetupError(f"no ground station {station_id}")
    if software is not None and software not in SOFTWARE:
        raise SetupError("software is Mission Planner, QGroundControl or other")
    folder = Path(path)
    if not folder.is_absolute() or not folder.is_dir():
        raise SetupError("choose an existing folder")
    row = store.conn.execute(
        "SELECT id FROM station_folder WHERE path = ? AND laptop_id = ? AND enabled = 1",
        (str(folder), store.laptop_id),
    ).fetchone()
    if row:
        raise SetupError("this folder is already watched")
    return store.insert(
        "station_folder",
        {"ground_station_id": station_id, "path": str(folder), "software": software},
    )


def remove_folder(store: Store, folder_id: str) -> None:
    """Stop watching a folder. The record stays, marked off, as records never vanish."""
    row = store.get("station_folder", folder_id)
    if row is None or not row["enabled"]:
        raise SetupError(f"no watched folder {folder_id}")
    store.update("station_folder", folder_id, row["version"], {"enabled": 0})


def list_folders(store: Store, station_id: str | None = None) -> list[dict[str, Any]]:
    """This laptop's watched folders, with each station's current version, which the
    files found there are recorded against."""
    query = (
        "SELECT f.id, f.ground_station_id, s.name, s.current_version_id, f.path, f.software "
        "FROM station_folder f JOIN ground_station s ON s.id = f.ground_station_id "
        "WHERE f.enabled = 1 AND f.laptop_id = ?"
    )
    args: list[Any] = [store.laptop_id]
    if station_id is not None:
        query += " AND f.ground_station_id = ?"
        args.append(station_id)
    return [
        {"id": r[0], "station_id": r[1], "station": r[2], "station_version_id": r[3],
         "path": r[4], "software": r[5]}
        for r in store.conn.execute(query + " ORDER BY s.name, f.path", args)
    ]  # fmt: skip
