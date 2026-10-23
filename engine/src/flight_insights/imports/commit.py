"""Storing a batch once a person has decided (spec: Platforms, "Every import asks
which airframe and what changed").

For each onboard log: its decoded columns go to the encrypted cache, one set per
flight, with the board's system-ID message left out. Its source file, parameter
snapshot and flights are stored. If an airframe was chosen, the flights join it:
confirmed hardware changes make a new build version, and the parameters are
compared with the airframe's previous flight. If not, the flights wait in the
hold queue with the reason and the candidates, until `resolve` attaches them.

A tlog is linked to the flights of the boot it recorded, now or when that
flight's log arrives later. A parameter file is kept as a snapshot of the
airframe it matched.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, cast

import numpy as np
import pyarrow as pa

from flight_insights.dataflash import read_dataflash
from flight_insights.dataflash.log import DataflashLog
from flight_insights.fingerprint.board import system_id_rows
from flight_insights.fingerprint.devices import to_json
from flight_insights.history.changes import LoadedFile, diff_snapshots, label_changes
from flight_insights.history.persist import load_snapshot, save_change_sets, save_snapshot
from flight_insights.imports.propose import Proposal, file_snapshot, last_flight
from flight_insights.imports.scan import SAME_BOOT_US, Scanned, ScannedFlight
from flight_insights.knowledge import Pack
from flight_insights.platforms.records import create_airframe, record_build
from flight_insights.platforms.schema import SetupError
from flight_insights.segment import find_boots, timeline
from flight_insights.segment.validate import boot_index
from flight_insights.store import _sqlcipher
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store, StoreError
from flight_insights.store.register import register_source

ChangeKind = Literal["failure", "upgrade", "test", "detected"]
REASONS: tuple[ChangeKind, ...] = ("failure", "upgrade", "test", "detected")


@dataclass(frozen=True)
class ConfirmedChange:
    part: str  # "board", "INS_ACC_ID", "propellers"...
    reason_kind: ChangeKind
    reason: str = ""


@dataclass
class Decision:
    log: str
    action: Literal["import", "skip"] = "import"
    airframe_id: str | None = None  # None: hold the log's flights
    new_airframe: tuple[str, str] | None = None  # (platform id, name)
    changes: list[ConfirmedChange] = field(default_factory=list[ConfirmedChange])
    packs: list[str] = field(default_factory=list[str])
    payload: str | None = None
    station_version_id: str | None = None


@dataclass
class Result:
    log: str
    status: Literal["imported", "held", "linked", "skipped", "duplicate", "error"]
    flights: list[str] = field(default_factory=list[str])
    source_id: str | None = None
    message: str | None = None


# --- the column cache -----------------------------------------------------------------


def _slices(flights: Sequence[ScannedFlight]) -> list[tuple[ScannedFlight, float, float]]:
    """Each flight of one boot with the stretch of log time it keeps: from the end of
    the flight before it (so its own pre-arm checks are with it) to its successor's arming."""
    ordered = sorted(flights, key=lambda f: f.arm_us)
    out: list[tuple[ScannedFlight, float, float]] = []
    for k, f in enumerate(ordered):
        start = -np.inf if k == 0 else ordered[k - 1].disarm_us
        end = np.inf if k == len(ordered) - 1 else ordered[k + 1].arm_us
        out.append((f, start, end))
    return out


def cache_flights(
    df: DataflashLog, cache: ColumnCache, flights: Sequence[ScannedFlight]
) -> list[str]:
    """Every message type of each flight's stretch of the log, full rate, into the
    cache. The board's system-ID message is never written. A type redefined part-way
    through the log is kept per definition, as NAME, NAME.2... Returns the types left
    out because their name cannot be a file name (a corrupted FMT)."""
    boots = find_boots(timeline(df))
    by_boot: dict[int, list[ScannedFlight]] = defaultdict(list)
    for f in flights:
        by_boot[f.boot].append(f)
    secret = system_id_rows(df)
    seen: dict[tuple[str, str], int] = defaultdict(int)
    skipped: list[str] = []
    for fid, fmt in enumerate(df.index.formats):
        n = df.count_format(fid)
        if not n:
            continue
        boots_of = boot_index(boots, df.offsets(fid))
        times = (
            np.asarray(df.field(fid, "TimeUS"), dtype=np.float64)
            if "TimeUS" in fmt.field_names
            else None
        )
        keep = np.ones(n, dtype=bool)
        for secret_fid, position in secret:
            if secret_fid == fid:
                keep[position] = False
        for boot, flown in by_boot.items():
            for k, (f, start, end) in enumerate(_slices(flown)):
                mask = keep & (boots_of == boot)
                if times is not None:
                    mask &= (times >= start) & (times < end)
                elif k:  # untimed messages stay with the boot's first flight
                    continue
                positions = np.flatnonzero(mask)
                if not len(positions):
                    continue
                seen[(f.key, fmt.name)] += 1
                count = seen[(f.key, fmt.name)]
                name = fmt.name if count == 1 else f"{fmt.name}.{count}"
                if not cache.cacheable(name):
                    if name not in skipped:
                        skipped.append(name)
                    continue
                cache.write(f.key, name, _batches(df, fid, positions))
    return skipped


def _batches(
    df: DataflashLog, fid: int, positions: np.ndarray[Any, Any]
) -> Iterator[pa.RecordBatch]:
    return df.iter_selected(fid, positions.astype(np.intp))


# --- attaching flights to an airframe -------------------------------------------------


def attach(
    store: Store,
    pack: Pack,
    flight_ids: Sequence[str],
    airframe_id: str,
    changes: Sequence[ConfirmedChange] = (),
    files: Sequence[LoadedFile] = (),
) -> None:
    """Give held or new flights their airframe: record confirmed changes as a new
    build version, label parameter changes since the airframe's previous flight, and
    remember the board. A new board on an airframe that had one is always recorded.
    The previous flight is the one before these: a log older than the airframe's
    latest flight is compared with its own predecessor and changes no board."""
    if not flight_ids:
        return
    airframe = store.get("airframe", airframe_id)
    if airframe is None:
        raise SetupError(f"no airframe {airframe_id}")
    marks = ",".join("?" for _ in flight_ids)
    rows = store.conn.execute(
        f"SELECT id, param_snapshot_id, board_id_hash, boot_utc_us + arm_us FROM flight "
        f"WHERE id IN ({marks}) ORDER BY boot_utc_us, arm_us",
        list(flight_ids),
    ).fetchall()
    if len(rows) != len(flight_ids):
        raise SetupError("some of these flights do not exist")
    first_id, snapshot_id, board, armed_utc = rows[0]
    previous = last_flight(store, airframe_id, armed_utc)
    latest = last_flight(store, airframe_id)
    newest = latest is None or (previous is not None and previous.flight_id == latest.flight_id)
    confirmed = list(changes)
    old_board = airframe["board_id_hash"]
    if (
        newest
        and board
        and old_board
        and board != old_board
        and not any(c.part == "board" for c in confirmed)
    ):
        confirmed.append(
            ConfirmedChange("board", "detected", "flight controller replaced: new board ID")
        )
    with store.transaction():
        if confirmed:
            kinds: set[ChangeKind] = {c.reason_kind for c in confirmed}
            reason = "; ".join(f"{c.part}: {c.reason}" if c.reason else c.part for c in confirmed)
            record_build(
                store, airframe_id, kinds.pop() if len(kinds) == 1 else "detected", reason, first_id
            )
            airframe = store.get("airframe", airframe_id) or airframe
        build = airframe["current_build_id"]
        for fid, *_ in rows:
            current = store.get("flight", fid)
            assert current is not None
            store.update(
                "flight",
                fid,
                current["version"],
                {"airframe_id": airframe_id, "build_version_id": build, "hold_json": None},
            )
        if newest and board and board != airframe["board_id_hash"]:
            store.update("airframe", airframe_id, airframe["version"], {"board_id_hash": board})
        if previous and previous.snapshot_id and snapshot_id:
            before, after = (
                load_snapshot(store, previous.snapshot_id),
                load_snapshot(store, snapshot_id),
            )
            diffs = diff_snapshots(before, after, pack)
            labelled = label_changes(diffs, after.dialect(pack), before.changes, (), files)
            if labelled:
                save_change_sets(store, airframe_id, previous.flight_id, first_id, labelled)


def held(store: Store) -> list[dict[str, Any]]:
    """Flights waiting for a person: why, and which airframes they might belong to."""
    out: list[dict[str, Any]] = []
    for fid, key, utc, arm, hold in store.conn.execute(
        "SELECT id, flight_key, boot_utc_us, arm_us, hold_json FROM flight "
        "WHERE airframe_id IS NULL ORDER BY boot_utc_us, arm_us"
    ):
        out.append(
            {"id": fid, "key": key, "boot_utc_us": utc, "arm_us": arm, **json.loads(hold or "{}")}
        )
    return out


def resolve(
    store: Store,
    pack: Pack,
    flight_ids: Sequence[str],
    airframe_id: str | None = None,
    new_airframe: tuple[str, str] | None = None,
    changes: Sequence[ConfirmedChange] = (),
) -> str:
    """Decide held flights: an existing airframe (for a replaced flight controller,
    say), or a new one. Returns the airframe's id."""
    for fid in flight_ids:
        row = store.get("flight", fid)
        if row is None or row["airframe_id"] is not None:
            raise SetupError(f"flight {fid} is not waiting for a decision")
    if new_airframe is not None:
        airframe_id = create_airframe(store, *new_airframe)
    if airframe_id is None:
        raise SetupError("choose an airframe or create one")
    attach(store, pack, flight_ids, airframe_id, changes)
    return airframe_id


# --- the batch ------------------------------------------------------------------------


def _source(
    store: Store, item: Scanned, kind: str, station: str | None, meta: dict[str, Any] | None = None
) -> str:
    reg = register_source(store, item.path, kind, station)  # type: ignore[arg-type]
    if meta and not reg.duplicate:
        row = store.get("source_file", reg.id)
        assert row is not None
        store.update("source_file", reg.id, row["version"], {"meta_json": json.dumps(meta)})
    return reg.id


def _stored(store: Store, key: str) -> str | None:
    """The flight stored under this key: the scan's answer may be out of date, as the
    same flight can arrive twice in a batch, or from another batch since."""
    row = store.conn.execute("SELECT id FROM flight WHERE flight_key = ?", (key,)).fetchone()
    return row[0] if row else None


def _link(store: Store, flight_id: str, source_id: str, role: str) -> None:
    store.conn.execute(
        "INSERT OR IGNORE INTO flight_source (flight_id, source_file_id, role) VALUES (?, ?, ?)",
        (flight_id, source_id, role),
    )


def _pair_stored_tlogs(store: Store, flight_id: str, boot_utc_us: int | None) -> None:
    """Link tlogs imported before this flight's log that recorded the same boot."""
    if boot_utc_us is None:
        return
    for sid, meta in store.conn.execute(
        "SELECT id, meta_json FROM source_file WHERE kind = 'tlog'"
    ):
        boots = cast(list[dict[str, int]], json.loads(meta or "{}").get("boots", []))
        if any(abs(b["boot_utc_us"] - boot_utc_us) <= SAME_BOOT_US for b in boots):
            _link(store, flight_id, sid, "tlog")


def _packs(store: Store, codes: Sequence[str]) -> list[str]:
    ids: list[str] = []
    for code in codes:
        code = code.strip()
        if not code:
            continue
        row = store.conn.execute(
            "SELECT id FROM battery_pack WHERE pack_code = ?", (code,)
        ).fetchone()
        ids.append(row[0] if row else store.insert("battery_pack", {"pack_code": code[:64]}))
    return ids


def _station_version(store: Store, decision: Decision, default: str | None) -> str | None:
    if decision.station_version_id is None:
        return default
    if store.get("ground_station_version", decision.station_version_id) is None:
        raise SetupError(f"no ground station version {decision.station_version_id}")
    return decision.station_version_id


def _dataflash(
    store: Store,
    pack: Pack,
    cache: ColumnCache,
    item: Scanned,
    proposal: Proposal,
    decision: Decision,
    station: str | None,
    files: Sequence[LoadedFile],
) -> Result:
    new = [f for f in item.flights if _stored(store, f.key) is None]
    with read_dataflash(item.path) as df:
        skipped = cache_flights(df, cache, new)
    airframe_id = decision.airframe_id
    assert item.snapshot is not None and item.hardware is not None
    with store.transaction():
        if decision.new_airframe is not None:
            airframe_id = create_airframe(store, *decision.new_airframe)
        source = _source(store, item, "dataflash", station)
        row = store.conn.execute(
            "SELECT id FROM param_snapshot WHERE source_file_id = ? AND origin = 'log start'",
            (source,),
        ).fetchone()
        snapshot_id = row[0] if row else save_snapshot(store, item.snapshot, pack, source)
        if airframe_id and not item.flights:  # a bench log: kept as the airframe's, as a file is
            snap = store.get("param_snapshot", snapshot_id)
            assert snap is not None
            if snap["airframe_id"] is None:
                store.update(
                    "param_snapshot", snapshot_id, snap["version"], {"airframe_id": airframe_id}
                )
        hold = (
            None
            if airframe_id
            else json.dumps(
                {
                    "reason": proposal.status,
                    "note": proposal.note,
                    "log": item.path.name,
                    "board_id_hash": item.board_id_hash,
                    "candidates": [asdict(c) for c in proposal.candidates],
                }
            )
        )
        packs = _packs(store, decision.packs)
        flight_ids: list[str] = []
        for f in item.flights:
            if known := _stored(store, f.key):
                _link(store, known, source, "dataflash")
                continue
            fid = store.insert(
                "flight",
                {
                    "flight_key": f.key,
                    "board_id_hash": item.board_id_hash,
                    "arm_us": f.arm_us,
                    "disarm_us": f.disarm_us,
                    "boot_utc_us": f.boot_utc_us,
                    "boot_count": f.boot_count,
                    "firmware": item.firmware,
                    "duration_s": f.duration_s,
                    "param_snapshot_id": snapshot_id,
                    "ground_station_version_id": station,
                    "hardware_json": json.dumps(to_json(item.hardware)),
                    "hold_json": hold,
                    "payload": (decision.payload or "").strip()[:200] or None,
                },
            )
            flight_ids.append(fid)
            _link(store, fid, source, "dataflash")
            _pair_stored_tlogs(store, fid, f.boot_utc_us)
            for pack_id in packs:
                store.conn.execute(
                    "INSERT OR IGNORE INTO flight_battery (flight_id, battery_pack_id) "
                    "VALUES (?, ?)",
                    (fid, pack_id),
                )
        if airframe_id and flight_ids:
            attach(store, pack, flight_ids, airframe_id, decision.changes, files)
    if item.flights and not flight_ids:
        return Result(item.id, "duplicate", [], source, "every flight was already imported")
    note = (
        f"not cached, names no file can have: {', '.join(map(repr, skipped))}" if skipped else None
    )
    return Result(item.id, "imported" if airframe_id else "held", flight_ids, source, note)


def _tlog(store: Store, item: Scanned, station: str | None) -> Result:
    with store.transaction():
        source = _source(store, item, "tlog", station, {"boots": [asdict(b) for b in item.boots]})
        linked: list[str] = []
        for boot in item.boots:
            for (fid,) in store.conn.execute(
                "SELECT id FROM flight WHERE boot_utc_us BETWEEN ? AND ?",
                (boot.boot_utc_us - SAME_BOOT_US, boot.boot_utc_us + SAME_BOOT_US),
            ).fetchall():
                _link(store, fid, source, "tlog")
                linked.append(fid)
    if linked:
        return Result(item.id, "linked", linked, source)
    return Result(
        item.id, "imported", [], source, "kept until the onboard log of its flight arrives"
    )


def _param_file(
    store: Store, pack: Pack, item: Scanned, airframe_id: str | None, station: str | None
) -> Result:
    snapshot = file_snapshot(item, pack)
    with store.transaction():
        source = _source(store, item, item.kind or "param", station)
        kept = store.conn.execute(
            "SELECT 1 FROM param_snapshot WHERE source_file_id = ? AND airframe_id = ?",
            (source, airframe_id),
        ).fetchone()
        if kept:
            return Result(item.id, "duplicate", [], source, "already kept for this airframe")
        if snapshot is not None and airframe_id:
            sid = save_snapshot(store, snapshot, pack, source)
            row = store.get("param_snapshot", sid)
            assert row is not None
            store.update("param_snapshot", sid, row["version"], {"airframe_id": airframe_id})
    return Result(item.id, "imported", [], source)


def commit(
    store: Store,
    pack: Pack,
    cache: ColumnCache,
    batch: Sequence[Scanned],
    proposals: Sequence[Proposal],
    decisions: Sequence[Decision],
    default_station_version: str | None = None,
    progress: Callable[[float, str], None] = lambda _f, _m: None,
    check: Callable[[], None] = lambda: None,
) -> list[Result]:
    """Store the batch as decided. Onboard logs go first, so tlogs and parameter files
    in the same batch find their flights. Each file is stored on its own: one that
    fails is reported and the rest still go in."""
    by_log = {d.log: d for d in decisions}
    proposal_of = {p.log: p for p in proposals}
    for d in decisions:
        if d.log not in proposal_of:
            raise SetupError(f"no file {d.log} in this batch")
        for c in d.changes:
            if c.reason_kind not in REASONS:
                raise SetupError("a change's reason is failure, upgrade, test or detected")
    order = sorted(
        batch, key=lambda i: {"dataflash": 0, "param": 1, "params": 1}.get(i.kind or "", 2)
    )
    files = [LoadedFile(i.path.name, i.params) for i in batch if i.params is not None]
    results: list[Result] = []
    for n, item in enumerate(order):
        check()
        progress(n / max(len(order), 1), item.path.name)
        decision = by_log.get(item.id, Decision(item.id))
        proposal = proposal_of[item.id]
        if decision.action == "skip" or item.error:
            results.append(
                Result(item.id, "skipped" if not item.error else "error", message=item.error)
            )
            continue
        try:
            station = _station_version(store, decision, default_station_version)
            match item.kind:
                case "dataflash":
                    results.append(
                        _dataflash(store, pack, cache, item, proposal, decision, station, files)
                    )
                case "tlog":
                    results.append(_tlog(store, item, station))
                case _:
                    chosen = decision.airframe_id or proposal.airframe_id
                    results.append(_param_file(store, pack, item, chosen, station))
        except (SetupError, ValueError) as e:
            results.append(Result(item.id, "error", message=str(e)))
        except (StoreError, OSError, _sqlcipher.DatabaseError) as e:  # this file only
            results.append(Result(item.id, "error", message=f"{type(e).__name__}: {e}"))
    progress(1.0, "imported")
    return sorted(results, key=lambda r: int(r.log[1:]))
