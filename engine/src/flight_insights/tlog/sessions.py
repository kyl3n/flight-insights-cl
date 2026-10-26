"""Each flight's tlog sessions (spec: Link and ground-station analysis, "Pairing and
alignment"): the boot of the vehicle a linked tlog recorded that is this flight's,
with the station's clock fitted against the vehicle's (align.py), the pairing checked
against the onboard attitude, and the packets lost on the way down.

A tlog's decoded tables are cached once, under the tlog's own key: one tlog can hold
several flights, and one flight several tlogs (two stations, or a station reconnecting).
Position columns go under the positions key, as the onboard log's do. Sessions are kept
in `tlog_session`, local to this laptop, and worked out again whenever a flight or a
tlog arrives; a tlog no longer readable here leaves its sessions 'missing'.

Analyzers read a session's packets on the flight's own clock (`session_table`): the
packets the station recorded during that boot, of every sender, each with the vehicle
boot time it maps to.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.dataflash import si
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store
from flight_insights.tlog.align import (
    CONFIRM_R,
    LAG_TOLERANCE_MS,
    SAME_BOOT_US,
    BootRecords,
    Clock,
    Confirmation,
    boot_utc_us,
    boots,
    confirm,
    fit,
    losses,
)
from flight_insights.tlog.decode import TlogLog, read_tlog

log = logging.getLogger(__name__)
SOURCES = "_sources"  # the senders and their counts; no message type starts with "_"
TIMED = ("tlog_time_us", "tlog_record", "src_sysid", "src_compid", "src_seq")
SOURCE_SCHEMA = pa.schema(
    [("sysid", pa.int64()), ("compid", pa.int64()), ("role", pa.string()),
     ("mav_type", pa.int64()), ("messages", pa.int64()), ("first_us", pa.int64()),
     ("last_us", pa.int64()), ("lost", pa.int64()), ("out_of_order", pa.int64())]
)  # fmt: skip
TO_DEG = {"deg": 1.0, "rad": 180.0 / np.pi}


def cache_key(source_id: str) -> str:
    return f"tlog-{source_id}"


def cache_tlog(cache: ColumnCache, source_id: str, decoded: TlogLog) -> list[str]:
    """Write a decoded tlog's tables to the cache; returns the message types it could
    not store (a name no file can have)."""
    key = cache_key(source_id)
    skipped: list[str] = []
    for name, table in decoded.tables.items():
        if cache.cacheable(name):
            cache.write_table(key, name, table)
        else:
            skipped.append(name)
    rows = [asdict(s) for s in decoded.sources]
    cache.write_table(key, SOURCES, pa.Table.from_pylist(rows, schema=SOURCE_SCHEMA))
    return skipped


def _path(store: Store, source_id: str) -> Path | None:
    row = store.get("source_file", source_id)
    if row is None:
        return None
    found = store.conn.execute(
        "SELECT path FROM source_location WHERE sha256 = ? AND path IS NOT NULL", (row["sha256"],)
    ).fetchone()
    for candidate in (found[0] if found else None, row.get("path")):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def ensure_cached(store: Store, cache: ColumnCache, source_id: str) -> bool:
    """Whether the tlog's tables are in the cache, reading it again where they are not
    (a tlog imported before tables were kept, or evicted)."""
    if SOURCES in cache.names(cache_key(source_id)):
        return True
    path = _path(store, source_id)
    if path is None:
        return False
    try:
        cache_tlog(cache, source_id, read_tlog(path))
    except Exception:  # unreadable here now: its sessions say so
        log.exception("tlog %s could not be read again", source_id)
        return False
    return True


def _tables(cache: ColumnCache, key: str) -> dict[str, pa.Table]:
    """Each message type's record columns, with the boot time and the attitude where a
    type has them; never positions."""
    out: dict[str, pa.Table] = {}
    for name in cache.names(key):
        if name == SOURCES:
            continue
        have = set(cache.schema(key, name).names)
        extra = [c for c in ("time_boot_ms", "time_unix_usec", "roll", "pitch") if c in have]
        if name != "SYSTEM_TIME":
            extra = [c for c in extra if c != "time_unix_usec"]
        if name != "ATTITUDE":
            extra = [c for c in extra if c not in ("roll", "pitch")]
        out[name] = cache.read(key, name, [*TIMED, *extra])
    return out


def _vehicles(cache: ColumnCache, key: str) -> list[tuple[int, int]]:
    senders = cache.read(key, SOURCES).to_pylist()
    return [(int(s["sysid"]), int(s["compid"])) for s in senders if s.get("role") == "vehicle"]


Series = tuple[np.ndarray[Any, Any], np.ndarray[Any, Any], np.ndarray[Any, Any]]


def _station_attitude(tables: Mapping[str, pa.Table], b: BootRecords) -> Series | None:
    table = tables.get("ATTITUDE")
    if table is None or "roll" not in table.column_names:
        return None
    n = np.asarray(table.column("tlog_record").to_numpy(zero_copy_only=False), np.int64)
    sysid = np.asarray(table.column("src_sysid").to_numpy(), np.int64)
    compid = np.asarray(table.column("src_compid").to_numpy(), np.int64)
    mine = (sysid == b.sysid) & (compid == b.compid) & (n >= b.records[0]) & (n <= b.records[1])
    t = np.asarray(table.column("time_boot_ms").to_numpy(), np.float64)[mine] * 1000.0
    roll = np.degrees(np.asarray(table.column("roll").to_numpy(), np.float64)[mine])
    pitch = np.degrees(np.asarray(table.column("pitch").to_numpy(), np.float64)[mine])
    order = np.argsort(t)
    return t[order], roll[order], pitch[order]


def _onboard_attitude(cache: ColumnCache, flight_key: str) -> Series | None:
    if "ATT" not in cache.names(flight_key):
        return None
    att = cache.read(flight_key, "ATT", ["TimeUS", "Roll", "Pitch"])
    if att.num_rows < 10 or si.unit(att, "Roll") not in TO_DEG:
        return None
    k = TO_DEG[si.unit(att, "Roll")]
    t = np.asarray(att.column("TimeUS").to_numpy(), np.float64)
    order = np.argsort(t, kind="stable")
    return t[order], si.values(att, "Roll")[order] * k, si.values(att, "Pitch")[order] * k


@dataclass(frozen=True)
class Session:
    sysid: int
    compid: int
    status: str
    clock: Clock | None
    confirmation: Confirmation | None
    records: tuple[int, int] | None
    received: int
    lost: int
    out_of_order: int


def _session(
    cache: ColumnCache, flight_key: str, tables: Mapping[str, pa.Table], b: BootRecords
) -> Session:
    clock = fit(b)
    received, lost, out_of_order = losses(dict(tables), b.sysid, b.compid, b.records)
    station = _station_attitude(tables, b)
    onboard = _onboard_attitude(cache, flight_key)
    found = confirm(station, onboard) if station and onboard else None
    status = "no clock"
    if clock is not None:
        status = "unconfirmed"
        if found is not None and found.r >= CONFIRM_R:
            status = "aligned" if abs(found.lag_ms) <= LAG_TOLERANCE_MS else "corrected"
            if status == "corrected":  # the streamed boot times run lag_ms behind TimeUS
                scale = 1.0 + clock.drift_ppm / 1e6
                clock = Clock(
                    clock.offset_us - scale * found.lag_ms * 1000.0, clock.drift_ppm,
                    clock.windows, clock.stalls, clock.spread_ms, clock.delay_ms,
                )  # fmt: skip
    return Session(
        b.sysid, b.compid, status, clock, found, b.records, received, lost, out_of_order
    )  # fmt: skip


def refresh(store: Store, cache: ColumnCache, flight_id: str) -> list[dict[str, Any]]:
    """Works out the flight's sessions again from its linked tlogs; returns them."""
    flight = store.get("flight", flight_id)
    if flight is None:
        return []
    linked = [
        r[0]
        for r in store.conn.execute(
            "SELECT source_file_id FROM flight_source WHERE flight_id = ? AND role = 'tlog'",
            (flight_id,),
        ).fetchall()
    ]
    rows: list[dict[str, Any]] = []
    for source_id in linked:
        if not ensure_cached(store, cache, source_id):
            rows.append({"source_file_id": source_id, "sysid": 0, "compid": 0, "status": "missing"})
            continue
        if flight["boot_utc_us"] is None:
            continue  # the onboard log has no UTC boot time: nothing to pair the boot with
        key = cache_key(source_id)
        tables = _tables(cache, key)
        for b in boots(tables, _vehicles(cache, key)):
            utc = boot_utc_us(b)
            if utc is None or abs(utc - int(flight["boot_utc_us"])) > SAME_BOOT_US:
                continue
            s = _session(cache, str(flight["flight_key"]), tables, b)
            rows.append(_row(source_id, s))
    with store.transaction():
        store.conn.execute("DELETE FROM tlog_session WHERE flight_id = ?", (flight_id,))
        for row in rows:
            store.insert("tlog_session", {"flight_id": flight_id, **row})
    return sessions(store, flight_id)


def _row(source_id: str, s: Session) -> dict[str, Any]:
    quality: dict[str, Any] = {}
    if s.clock is not None:
        quality |= {
            "windows": s.clock.windows, "stalls": s.clock.stalls,
            "spread_ms": round(s.clock.spread_ms, 3),
            "delay_ms": [round(v, 2) for v in s.clock.delay_ms],
        }  # fmt: skip
    if s.confirmation is not None:
        quality |= {"lag_ms": s.confirmation.lag_ms, "r": round(s.confirmation.r, 4)}
    return {
        "source_file_id": source_id,
        "sysid": s.sysid,
        "compid": s.compid,
        "status": s.status,
        "offset_us": None if s.clock is None else round(s.clock.offset_us),
        "drift_ppm": None if s.clock is None else s.clock.drift_ppm,
        "first_record": None if s.records is None else s.records[0],
        "last_record": None if s.records is None else s.records[1],
        "received": s.received,
        "lost": s.lost,
        "out_of_order": s.out_of_order,
        "quality_json": json.dumps(quality),
    }


def sessions(store: Store, flight_id: str) -> list[dict[str, Any]]:
    cur = store.conn.execute(
        "SELECT * FROM tlog_session WHERE flight_id = ? ORDER BY source_file_id, sysid, compid",
        (flight_id,),
    )
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        row = {d[0]: v for d, v in zip(cur.description, r, strict=True)}
        row["quality"] = json.loads(row.pop("quality_json"))
        out.append(row)
    return out


def needs_refresh(store: Store, flight_id: str) -> bool:
    """A flight with a tlog linked but no session for it (linked before sessions were
    kept, or on another laptop)."""
    row = store.conn.execute(
        "SELECT 1 FROM flight_source f WHERE f.flight_id = ? AND f.role = 'tlog' AND NOT EXISTS "
        "(SELECT 1 FROM tlog_session s WHERE s.flight_id = f.flight_id "
        "AND s.source_file_id = f.source_file_id) LIMIT 1",
        (flight_id,),
    ).fetchone()
    return row is not None


def session_table(
    cache: ColumnCache, session: Mapping[str, Any], name: str, columns: list[str] | None = None
) -> pa.Table | None:
    """A message type the station recorded during the session's boot, from every sender,
    with `TimeUS`: the vehicle boot time each packet maps to (the station's clock through
    the session's fit). None when the tlog has none, or the session has no clock."""
    if session["offset_us"] is None or session["first_record"] is None:
        return None
    key = cache_key(str(session["source_file_id"]))
    if name not in cache.names(key):
        return None
    table = cache.read(key, name, None if columns is None else [*TIMED, *columns])
    n = np.asarray(table.column("tlog_record").to_numpy(zero_copy_only=False), np.int64)
    inside = (n >= session["first_record"]) & (n <= session["last_record"])
    table = table.filter(pa.array(inside, pa.bool_()))
    clock = Clock(float(session["offset_us"]), float(session["drift_ppm"]), 0, 0, 0.0, (0.0, 0.0))
    t = table.column("tlog_time_us").to_numpy(zero_copy_only=False)
    boot = clock.to_boot(np.asarray(t, np.float64))
    mapped = pa.array(
        [None if not np.isfinite(v) else round(float(v)) for v in boot], pa.int64()
    )  # fmt: skip
    return table.append_column(pa.field("TimeUS", pa.int64(), metadata={"unit": "us"}), mapped)


def session_packets(cache: ColumnCache, session: Mapping[str, Any]) -> pa.Table | None:
    """Every packet the station recorded during the session's boot, from every sender:
    when it was recorded, its record number, sender and sequence, and `TimeUS` as in
    `session_table`, in recorded order. None when the session has no clock."""
    if session["offset_us"] is None or session["first_record"] is None:
        return None
    key = cache_key(str(session["source_file_id"]))
    parts = [
        table for name in cache.names(key)
        if name != SOURCES and (table := session_table(cache, session, name, [])) is not None
    ]  # fmt: skip
    if not parts:
        return None
    packets = pa.concat_tables(parts)
    records = np.asarray(packets.column("tlog_record").to_numpy(zero_copy_only=False), np.int64)
    return packets.take(np.argsort(records, kind="stable"))
