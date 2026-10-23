"""Which MAVLink fields hold a position, from the message definitions.

A field is a position when its unit is degE7, or degrees on a field named as a
latitude or longitude. Mission and command coordinates carry no unit: x/y of
MISSION_ITEM, MISSION_ITEM_INT and COMMAND_INT, and param5/param6 of
COMMAND_LONG, hold a latitude and longitude only for some commands and frames,
so those columns are marked "by_command" and each row decides (spec: AI layer,
location scrubber). RTCM correction data is a position too: it carries the base
station's coordinates.
"""

# pymavlink ships without type information.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from typing import Any

from pymavlink.dialects.v20 import ardupilotmega as mavlink

# message -> its latitude and longitude columns, and whether the frame decides
COMMAND_COORDINATES: dict[str, tuple[str, str, bool]] = {
    "COMMAND_LONG": ("param5", "param6", False),
    "COMMAND_INT": ("x", "y", True),
    "MISSION_ITEM_INT": ("x", "y", True),
    "MISSION_ITEM": ("x", "y", True),
}

# message -> columns that always hold a position although no unit says so
POSITION_PAYLOADS: dict[str, tuple[str, ...]] = {
    "GPS_RTCM_DATA": ("data",),  # RTCM 1005/1006 hold the base station's ECEF position
    "GPS_INJECT_DATA": ("data",),
}


def _named(name: str) -> bool:
    low = name.lower()
    return "lat" in low or "lon" in low or "lng" in low


def position_columns(cls: type) -> dict[str, str]:
    """Column -> "1" (always a position) or "by_command" (depends on the row)."""
    units: dict[str, str] = getattr(cls, "fieldunits_by_name", {})
    out: dict[str, str] = {}
    for name in cls.fieldnames:
        unit = units.get(name)
        if unit == "degE7" or (unit == "deg" and _named(name)):
            out[name] = "1"
    for name in POSITION_PAYLOADS.get(cls.msgname, ()):
        out[name] = "1"
    command = COMMAND_COORDINATES.get(cls.msgname)
    if command is not None:
        out[command[0]] = out[command[1]] = "by_command"
    return out


def row_is_position(msgname: str, row: dict[str, Any]) -> bool:
    """For a "by_command" message: does this row's command put a position in x/y?"""
    command = COMMAND_COORDINATES.get(msgname)
    if command is None:
        return False
    entry = mavlink.enums["MAV_CMD"].get(int(row["command"]))
    if entry is None:
        positional = True  # unknown command: treat as a position, to be safe
    else:
        # The definitions flag commands with a location; some (DO_SET_ROI, NAV_ROI)
        # label param5/6 only "x" and "y", so the labels alone miss them.
        labels = {i: str(entry.param.get(i, "")).lower() for i in (5, 6)}
        positional = bool(getattr(entry, "has_location", False)) or (
            "latitude" in labels[5] and "longitude" in labels[6]
        )
    if command[2]:
        frame = mavlink.enums["MAV_FRAME"].get(int(row["frame"]))
        positional = positional and (frame is None or "GLOBAL" in frame.name)
    return positional
