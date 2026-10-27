"""Names for MAVLink's numbered values, from the dialect the tlog decoder reads."""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

from pymavlink.dialects.v20 import ardupilotmega as mavlink


def command_name(cmd: int) -> str:
    """MAV_CMD's name without its prefix (DO_SET_MODE), or the number for one it lacks."""
    entry = mavlink.enums["MAV_CMD"].get(cmd)
    return str(entry.name).removeprefix("MAV_CMD_") if entry is not None else f"command {cmd}"
