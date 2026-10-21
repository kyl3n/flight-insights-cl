"""Reading a batch of files for import: what each one is and holds, before anything
is stored (spec: Platforms, "Every import asks which airframe and what changed").

A DataFlash log gives its flights with their natural keys, the board's keyed hash,
its parameter snapshot and the hardware it flew with. A tlog gives the vehicle
boots it covers, from the vehicle's own SYSTEM_TIME messages, so it can be paired
with the onboard log of the same boot whatever the computer's clock said; and its
parameter downloads and writes. A parameter file gives its values.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np

from flight_insights.dataflash import read_dataflash
from flight_insights.dataflash.log import DataflashLog
from flight_insights.fingerprint.board import read_board
from flight_insights.fingerprint.devices import Hardware, hardware
from flight_insights.history.snapshot import Snapshot, from_log
from flight_insights.knowledge import Pack
from flight_insights.params.files import ParamFile, read_param_file
from flight_insights.segment import boot_identities, find_boots, flight_key, flights, timeline
from flight_insights.store.db import Store
from flight_insights.store.register import file_sha256
from flight_insights.tlog import read_tlog
from flight_insights.tlog.decode import TlogLog
from flight_insights.tlog.params import ParamDownload, ParamWrite, param_downloads, param_writes

Kind = Literal["dataflash", "tlog", "param", "params"]
SUFFIXES: dict[str, Kind] = {
    ".bin": "dataflash",
    ".tlog": "tlog",
    ".param": "param",
    ".params": "params",
}
SAME_BOOT_US = 2_000_000  # boot times from SYSTEM_TIME agree to well within this
SYSID_PARAMS = ("MAV_SYSID", "SYSID_THISMAV")  # renamed in 4.7


@dataclass(frozen=True)
class ScannedFlight:
    number: int
    boot: int
    key: str
    arm_us: int
    disarm_us: int
    duration_s: float
    boot_utc_us: int | None
    boot_count: int | None
    known_id: str | None  # the flight already stored under this key


@dataclass(frozen=True)
class TlogBoot:
    sysid: int
    boot_utc_us: int


@dataclass
class Scanned:
    id: str  # "f1", "f2"... within the batch
    path: Path
    kind: Kind | None
    size: int
    sha256: str
    known_source: str | None = None  # the source file already stored with these bytes
    error: str | None = None
    # DataFlash logs
    vehicle: str | None = None
    firmware: str | None = None
    sysid: int | None = None
    board_id_hash: str | None = None
    board_name: str | None = None
    snapshot: Snapshot | None = None
    hardware: Hardware | None = None
    flights: list[ScannedFlight] = field(default_factory=list[ScannedFlight])
    # tlogs
    boots: list[TlogBoot] = field(default_factory=list[TlogBoot])
    downloads: list[ParamDownload] = field(default_factory=list[ParamDownload])
    writes: list[ParamWrite] = field(default_factory=list[ParamWrite])
    # parameter files
    params: ParamFile | None = None


def kind_of(path: Path) -> Kind | None:
    return SUFFIXES.get(path.suffix.lower())


def _sysid(snapshot: Snapshot) -> int | None:
    for name in SYSID_PARAMS:
        if name in snapshot.values:
            return int(snapshot.values[name])
    return None


def _dataflash(item: Scanned, df: DataflashLog, pack: Pack, board_key: bytes, store: Store) -> None:
    snapshot = from_log(df)
    boots = find_boots(timeline(df))
    flown = flights(df, boots, pack, snapshot.vehicle, snapshot.firmware)
    ids = boot_identities(df, boots)
    board = read_board(df, pack, snapshot.firmware, board_key)
    item.vehicle, item.firmware = snapshot.vehicle, snapshot.firmware
    item.sysid = _sysid(snapshot)
    item.board_id_hash, item.board_name = board.id_hash, board.name
    item.snapshot, item.hardware = snapshot, hardware(df, snapshot)
    for f in flown:
        key = flight_key(ids[f.boot], f, board.id_hash)
        row = store.conn.execute("SELECT id FROM flight WHERE flight_key = ?", (key,)).fetchone()
        item.flights.append(
            ScannedFlight(
                f.number,
                f.boot,
                key,
                f.arm_us,
                f.disarm_us,
                f.duration_s,
                ids[f.boot].boot_utc_us,
                ids[f.boot].boot_count,
                row[0] if row else None,
            )
        )


def tlog_boots(log: TlogLog) -> list[TlogBoot]:
    """Each vehicle boot the tlog covers, with its UTC boot time from SYSTEM_TIME."""
    table = log.tables.get("SYSTEM_TIME")
    if table is None:
        return []
    vehicles = {s.sysid for s in log.sources if s.role == "vehicle"}
    sysid = np.asarray(table.column("src_sysid").to_numpy(), dtype=np.int64)
    unix = np.asarray(table.column("time_unix_usec").to_numpy(), dtype=np.int64)
    boot_ms = np.asarray(table.column("time_boot_ms").to_numpy(), dtype=np.int64)
    out: list[TlogBoot] = []
    for vehicle in sorted(vehicles):
        mine = (sysid == vehicle) & (unix > 0)
        starts = np.sort(unix[mine] - boot_ms[mine] * 1000)
        if not len(starts):
            continue
        groups = np.split(starts, np.flatnonzero(np.diff(starts) > SAME_BOOT_US) + 1)
        out += [TlogBoot(vehicle, int(np.median(g))) for g in groups]
    return out


def scan(
    store: Store,
    pack: Pack,
    board_key: bytes,
    paths: Sequence[Path],
    progress: Callable[[float, str], None] = lambda _f, _m: None,
    check: Callable[[], None] = lambda: None,
) -> list[Scanned]:
    """What each file is and holds. A file that cannot be read is reported, not raised."""
    out: list[Scanned] = []
    for n, path in enumerate(paths, 1):
        check()
        progress((n - 1) / max(len(paths), 1), path.name)
        sha = file_sha256(path)
        row = store.conn.execute("SELECT id FROM source_file WHERE sha256 = ?", (sha,)).fetchone()
        item = Scanned(
            f"f{n}", path, kind_of(path), path.stat().st_size, sha, row[0] if row else None
        )
        out.append(item)
        try:
            match item.kind:
                case "dataflash":
                    with read_dataflash(path) as df:
                        _dataflash(item, df, pack, board_key, store)
                case "tlog":
                    log = read_tlog(path)
                    item.boots = tlog_boots(log)
                    item.downloads, item.writes = param_downloads(log), param_writes(log)
                case "param" | "params":
                    item.params = read_param_file(path)
                case None:
                    item.error = "not a .bin, .tlog, .param or .params file"
        except Exception as e:  # an unreadable file is reported with the rest of the batch
            item.error = f"{type(e).__name__}: {e}"
    progress(1.0, "scanned")
    return out
