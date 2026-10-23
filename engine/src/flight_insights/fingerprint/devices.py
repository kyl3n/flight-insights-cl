"""The hardware a log was flown with, as identities that change when a part does
(spec: Platforms, "Detecting changes nobody recorded": direct evidence).

- IMUs and compasses: each sensor's device ID, which the firmware stores in its
  calibration parameters (INS_ACC_ID, INS_GYR2_ID, COMPASS_DEV_ID3...). A new
  sensor, or the same model on another bus, gives a new ID.
- DroneCAN nodes: the CAND messages the logger writes for each node it sees,
  by node ID, name and software version. The nodes' unique IDs are not kept.
- Firmware: from VER.

A device ID packs four fields (libraries/AP_HAL/Device.h, DeviceStructure): bus
type in bits 0-2, bus number in bits 3-7, address in bits 8-15 and the driver's
device type in bits 16-23.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from flight_insights.dataflash.formats import text
from flight_insights.dataflash.log import DataflashLog
from flight_insights.history.snapshot import Snapshot

BUS_TYPES = {
    0: "unknown",
    1: "I2C",
    2: "SPI",
    3: "DroneCAN",
    4: "SITL",
    5: "MSP",
    6: "serial",
    7: "WSPI",
}
SENSOR_ID = re.compile(r"^(?:INS\d?_(?:ACC|GYR)\d?_ID|COMPASS_DEV_ID\d?)$")


@dataclass(frozen=True)
class DeviceId:
    raw: int
    bus_type: str
    bus: int
    address: int
    devtype: int

    def describe(self) -> str:
        return (
            f"{self.bus_type} bus {self.bus}, address 0x{self.address:02X}, "
            f"type 0x{self.devtype:02X} (ID {self.raw})"
        )


def decode_device_id(raw: int) -> DeviceId:
    return DeviceId(
        raw,
        BUS_TYPES.get(raw & 0x7, "unknown"),
        (raw >> 3) & 0x1F,
        (raw >> 8) & 0xFF,
        (raw >> 16) & 0xFF,
    )


@dataclass(frozen=True)
class CanNode:
    node_id: int
    name: str
    version: str

    def describe(self) -> str:
        return f"node {self.node_id} {self.name} {self.version}".strip()


@dataclass(frozen=True)
class Hardware:
    firmware: str
    sensors: dict[str, DeviceId]  # parameter name -> its device ID; slots set to 0 left out
    can_nodes: tuple[CanNode, ...]


def sensor_ids(snapshot: Snapshot) -> dict[str, DeviceId]:
    out: dict[str, DeviceId] = {}
    for name, value in sorted(snapshot.values.items()):
        if SENSOR_ID.match(name) and value and value == int(value):
            out[name] = decode_device_id(int(value) & 0xFFFFFFFF)
    return out


def can_nodes(df: DataflashLog) -> tuple[CanNode, ...]:
    """Every DroneCAN node the log names, once each, by node ID."""
    if "CAND" not in df.message_types:
        return ()
    nodes: dict[int, CanNode] = {}
    for row in df.table("CAND").to_pylist():
        version = f"{row.get('Major', '')}.{row.get('Minor', '')}".strip(".")
        name = text(row["Name"]) if "Name" in row else ""
        nodes[int(row["NodeId"])] = CanNode(int(row["NodeId"]), name, version)
    return tuple(nodes[k] for k in sorted(nodes))


def hardware(df: DataflashLog, snapshot: Snapshot) -> Hardware:
    return Hardware(snapshot.firmware, sensor_ids(snapshot), can_nodes(df))


@dataclass(frozen=True)
class HardwareChange:
    part: str  # "board", a sensor's parameter name, "CAN node 10" or "firmware"
    before: str | None
    after: str | None
    evidence: str


def to_json(h: Hardware) -> dict[str, object]:
    return {
        "firmware": h.firmware,
        "sensors": {k: v.raw for k, v in h.sensors.items()},
        "can_nodes": [[n.node_id, n.name, n.version] for n in h.can_nodes],
    }


def from_json(data: dict[str, object]) -> Hardware:
    sensors = cast(dict[str, int], data.get("sensors") or {})
    nodes = cast(list[list[object]], data.get("can_nodes") or [])
    return Hardware(
        str(data.get("firmware") or ""),
        {k: decode_device_id(int(v)) for k, v in sensors.items()},
        tuple(CanNode(int(cast(int, n[0])), str(n[1]), str(n[2])) for n in nodes),
    )


def compare(
    before: Hardware, after: Hardware, board_before: str | None, board_after: str | None
) -> list[HardwareChange]:
    """Direct changes between two flights' hardware, each with its evidence."""
    out: list[HardwareChange] = []
    if board_before and board_after and board_before != board_after:
        out.append(
            HardwareChange(
                "board",
                "known board",
                "new board",
                "the board's unique ID differs (system-ID message)",
            )
        )
    if before.firmware != after.firmware:
        out.append(HardwareChange("firmware", before.firmware, after.firmware, "VER"))
    for name in sorted(before.sensors.keys() | after.sensors.keys()):
        b, a = before.sensors.get(name), after.sensors.get(name)
        if b != a:
            out.append(
                HardwareChange(
                    name,
                    b.describe() if b else None,
                    a.describe() if a else None,
                    f"parameter {name}",
                )
            )
    nb = {n.node_id: n for n in before.can_nodes}
    na = {n.node_id: n for n in after.can_nodes}
    for node in sorted(nb.keys() | na.keys()):
        b, a = nb.get(node), na.get(node)
        if b != a:
            out.append(
                HardwareChange(
                    f"CAN node {node}",
                    b.describe() if b else None,
                    a.describe() if a else None,
                    "CAND",
                )
            )
    return out
