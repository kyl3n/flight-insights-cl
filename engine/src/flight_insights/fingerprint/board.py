"""The board: which flight controller, its processor, and a keyed hash of its unique ID.

The logger writes the board's system ID as the message right after VER
(libraries/AP_Logger/LoggerMessageWriter.cpp, stage SYSTEM_ID). On ChibiOS boards
it reads "CubeOrange 00350029 3130510B 36383230": the board's short name and its
96-bit unique ID. The raw ID never leaves this module: it is hashed with the
fleet's board-ID key the moment it is read (spec: Platforms, "Airframe identity"),
so the same board gives the same hash on every laptop and nothing else.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

import numpy as np

from flight_insights.dataflash.formats import text
from flight_insights.dataflash.log import DataflashLog
from flight_insights.knowledge import Pack

# VER.BT, from libraries/AP_HAL/AP_HAL_Boards.h
HAL_BOARDS = {3: "SITL", 7: "Linux", 10: "ChibiOS", 12: "ESP32", 13: "QURT", 99: "Empty"}
CHIBIOS_ID = re.compile(r"^(\S{1,23}) ([0-9A-F]{8} [0-9A-F]{8} [0-9A-F]{8})$")
HASH_CONTEXT = b"flight-insights board id v1\x00"


@dataclass(frozen=True)
class Board:
    hal: str  # ChibiOS, SITL, Linux...
    name: str | None  # short board name, such as CubeOrange
    mcu: str | None  # STM32H743xx
    family: str | None  # H7, F7, F4...; "SITL" for the simulator
    apj_board_id: int | None
    id_hash: str | None  # keyed hash of the unique ID; None when the log has none


def hash_board_id(key: bytes, unique_id: str) -> str:
    if len(key) < 32:
        raise ValueError("the board-ID key must be at least 32 bytes")
    digest = hmac.new(key, HASH_CONTEXT + unique_id.encode(), hashlib.sha256).hexdigest()
    return f"bid1-{digest}"


def system_id_rows(df: DataflashLog) -> list[tuple[int, int]]:
    """(format id, position) of the MSG written immediately after each VER, in file
    order: the board's system ID, logged again at every boot of a multi-boot file,
    which caches and exports leave out."""
    ver = [df.offsets(f) for f in df.formats_named("VER") if df.count_format(f)]
    msg = [f for f in df.formats_named("MSG") if df.count_format(f)]
    if not ver or not msg:
        return []
    offsets = np.concatenate([df.offsets(f) for f in msg])
    fids = np.concatenate([np.full(df.count_format(f), f) for f in msg])
    positions = np.concatenate([np.arange(df.count_format(f)) for f in msg])
    order = np.argsort(offsets, kind="stable")
    after = np.searchsorted(offsets[order], np.unique(np.concatenate(ver)), side="right")
    found = np.unique(after[after < len(order)])  # a boot without one shares the next's
    return [(int(fids[order[k]]), int(positions[order[k]])) for k in found]


def system_id_row(df: DataflashLog) -> tuple[int, int] | None:
    """The first boot's system-ID row: the one the board is identified by."""
    rows = system_id_rows(df)
    return rows[0] if rows else None


def _system_id_message(df: DataflashLog) -> str | None:
    row = system_id_row(df)
    if row is None:
        return None
    fid, position = row
    return text(df.rows(fid)["Message"][position].tobytes())


def read_board(df: DataflashLog, pack: Pack, firmware: str, board_key: bytes) -> Board:
    hal_number = None
    for fid in df.formats_named("VER"):
        if df.count_format(fid) and "BT" in df.index.formats[fid].field_names:
            hal_number = int(df.field(fid, "BT")[0])
            break
    hal = HAL_BOARDS.get(hal_number, "unknown") if hal_number is not None else "unknown"
    message = _system_id_message(df)
    name: str | None = None
    unique: str | None = None
    if message is not None:
        m = CHIBIOS_ID.match(message)
        if m:
            name, unique = m[1], m[2]
        elif hal == "SITL" and message and " " not in message:
            unique = message  # the simulator reports the host's machine ID
    if hal == "SITL":
        name = "SITL"
    id_hash = None
    if unique is not None and unique.strip("0 "):  # a blanked (scrubbed) ID identifies nothing
        id_hash = hash_board_id(board_key, unique)
    del unique
    info = pack.boards(firmware).get(name or "", {})
    family = "SITL" if hal == "SITL" else info.get("family")
    return Board(hal, name, info.get("mcu"), family, info.get("apj_board_id"), id_hash)
