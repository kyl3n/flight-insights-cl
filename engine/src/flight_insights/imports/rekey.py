"""Board-ID hashes made again with the fleet's key (spec: Shared storage and sync,
"Encryption", and Vehicle fingerprinting).

A laptop hashes board IDs with a key of its own until it joins a fleet; from then on
with the fleet's, so the same board is recognised on every laptop. Raw board IDs are
never stored, so each flight's hash is remade from its log, read again from where it
was imported. Its flight key, which holds the hash, and its cached columns move with
it, and so do its airframe's board and this computer's ground-station hash.

A flight whose log is no longer on disk keeps the hash it had. It is still matched on
this laptop: imports hash a board with this laptop's old key too, and an airframe
known by that hash is found by it.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from flight_insights.dataflash import read_dataflash
from flight_insights.fingerprint.board import read_board
from flight_insights.history.snapshot import from_log
from flight_insights.knowledge import Pack
from flight_insights.segment import boot_identities, find_boots, flight_key, flights, timeline
from flight_insights.stations.computer import hash_host
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store
from flight_insights.store.register import file_sha256


def board_check(key: bytes) -> str:
    """A value that names the key without revealing it."""
    return hmac.new(key, b"fi.board-key-check", hashlib.sha256).hexdigest()[:32]


@dataclass(frozen=True)
class Rekeyed:
    flights: int  # hashed again from their logs
    kept: int  # logs gone: their hashes stay, matched on this laptop only
    stations: int


def needs_rekey(store: Store, key: bytes) -> bool:
    """Whether the stored hashes were made with another key. A store that has made no
    hash yet takes this key as its own."""
    stored = store.conn.execute("SELECT board_check FROM sync_state").fetchone()[0]
    if stored is None:
        with store.transaction() as conn:
            conn.execute("UPDATE sync_state SET board_check = ?", (board_check(key),))
        return False
    return str(stored) != board_check(key)


def rekey(
    store: Store,
    cache: ColumnCache,
    pack: Pack,
    key: bytes,
    old_key: bytes | None,
    host: str | None,
    progress: Callable[[float, str], None] = lambda _f, _m: None,
) -> Rekeyed:
    """Every hash this laptop can remake, with `key`. `old_key` and `host` remake this
    computer's ground-station hash; without them it is left as it was."""
    sources = store.conn.execute(
        "SELECT DISTINCT s.sha256, l.path FROM source_file s "
        "JOIN source_location l ON l.sha256 = s.sha256 "
        "JOIN flight_source fs ON fs.source_file_id = s.id "
        "JOIN flight f ON f.id = fs.flight_id "
        "WHERE s.kind = 'dataflash' AND l.path IS NOT NULL AND f.board_id_hash IS NOT NULL "
        "ORDER BY s.sha256"
    ).fetchall()
    moved: dict[str, str] = {}  # old board hash -> new
    done: set[str] = set()
    for n, (sha, path_text) in enumerate(sources):
        progress(n / max(len(sources), 1), "hashing board IDs again")
        path = Path(path_text)
        if not path.is_file() or file_sha256(path) != sha:
            continue  # moved or changed since: its flights keep their hashes
        records = {
            (utc, arm): (fid, fkey, old)
            for fid, fkey, old, utc, arm in store.conn.execute(
                "SELECT f.id, f.flight_key, f.board_id_hash, f.boot_utc_us, f.arm_us "
                "FROM flight f JOIN flight_source fs ON fs.flight_id = f.id "
                "JOIN source_file s ON s.id = fs.source_file_id WHERE s.sha256 = ?",
                (sha,),
            )
        }
        with read_dataflash(path) as df:
            snapshot = from_log(df)
            boots = find_boots(timeline(df))
            ids = boot_identities(df, boots)
            board = read_board(df, pack, snapshot.firmware, key)
            flown = flights(df, boots, pack, snapshot.vehicle, snapshot.firmware)
        for f in flown:
            found = records.get((ids[f.boot].boot_utc_us, f.arm_us))
            if found is None or found[0] in done or found[2] is None or board.id_hash is None:
                continue
            fid, old_key_text, old_hash = found
            new_key = flight_key(ids[f.boot], f, board.id_hash)
            row = store.get("flight", fid)
            assert row is not None
            with store.transaction():
                store.update("flight", fid, row["version"],
                             {"flight_key": new_key, "board_id_hash": board.id_hash})  # fmt: skip
            cache.rename(old_key_text, new_key)
            moved[old_hash] = board.id_hash
            done.add(fid)
    for aid, old, version in store.conn.execute(
        "SELECT id, board_id_hash, version FROM airframe WHERE board_id_hash IS NOT NULL"
    ).fetchall():
        if old in moved:
            store.update("airframe", aid, version, {"board_id_hash": moved[old]})
    stations = 0
    if old_key is not None and host:
        before, after = hash_host(old_key, host), hash_host(key, host)
        for vid, version in store.conn.execute(
            "SELECT id, version FROM ground_station_version WHERE host_hash = ?", (before,)
        ).fetchall():
            store.update("ground_station_version", vid, version, {"host_hash": after})
            stations += 1
    kept = store.conn.execute(
        "SELECT count(*) FROM flight WHERE board_id_hash IS NOT NULL"
    ).fetchone()[0] - len(done)
    with store.transaction() as conn:
        conn.execute("UPDATE sync_state SET board_check = ?", (board_check(key),))
    progress(1.0, "board IDs hashed again")
    return Rekeyed(len(done), int(kept), stations)
