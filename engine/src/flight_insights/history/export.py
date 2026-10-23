"""Exporting a snapshot as a parameter file to restore an airframe (spec: Parameter
history, "History and upgrades").

A restore file leaves out counters and hardware identity by default: writing back
an old boot count or a sensor's device ID would be wrong, not a restore. Export
to a different firmware goes through canonical names, so the file uses the names
that firmware has.
"""

from __future__ import annotations

from flight_insights.history.snapshot import Snapshot
from flight_insights.knowledge import Pack
from flight_insights.params.dialect import Dialect, ParamClass
from flight_insights.params.files import Kind, write_mission_planner, write_qgroundcontrol

NOT_RESTORED: frozenset[ParamClass] = frozenset({"counter", "hardware"})


def export_snapshot(
    snapshot: Snapshot,
    kind: Kind,
    pack: Pack,
    firmware: str | None = None,
    include_all: bool = False,
    title: str = "",
) -> str:
    """The snapshot as `.param` (Mission Planner) or `.params` (QGroundControl) text,
    in the dialect of `firmware` (default: the firmware it was taken on)."""
    source = snapshot.dialect(pack)
    values = {
        name: value
        for name, value in snapshot.values.items()
        if include_all or source.param_class(name, snapshot.values) not in NOT_RESTORED
    }
    if firmware is not None and firmware != snapshot.firmware:
        target = Dialect(pack, snapshot.vehicle, firmware)
        values = target.from_canonical(source.to_canonical(values).values)
    if kind == "mission planner":
        return write_mission_planner(values)
    return write_qgroundcontrol(values, title=title)
