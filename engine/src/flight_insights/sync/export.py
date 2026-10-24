"""This laptop's changes as objects to send (spec: Shared storage and sync, "What is
shared: immutable objects").

Changes are captured as they are made (store/db.py). `export` turns what is pending
into objects in the outbox, each with this laptop's next sequence number:

- a new record goes whole, with its fields' clocks and, for a parameter snapshot, its
  values and in-flight changes;
- a changed record goes as its changed fields, each with its clock and the clock of
  the value it replaced, so the receiving laptop can tell a later change from a
  concurrent one;
- a link between records goes as its row.

A source file this laptop imported also gets an object of its own: its path is for
the shell to read it from, and never goes into the object.

The first export after sync starts also queues every record made before it.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any

from flight_insights.store import synced
from flight_insights.store.db import Store
from flight_insights.sync.codec import encode, row

FORMAT = "fi.sync.records/1"
SOURCE_FORMAT = "fi.sync.source/1"
MAX_ENTRIES = 2000  # records per object, so no object grows without bound


def _schema(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT max(number) FROM schema_migrations").fetchone()[0])


def _seed(store: Store, conn: sqlite3.Connection) -> None:
    """Queue every record and link made before sync began, once."""
    if conn.execute("SELECT seeded FROM sync_state").fetchone()[0]:
        return
    for table in synced.RECORDS:
        conn.execute(
            f"INSERT OR IGNORE INTO sync_pending SELECT ?, id, '*', NULL FROM {table}", (table,)
        )
    for table, key in synced.LINKS.items():
        joined = " || '|' || ".join(key)
        conn.execute(
            f"INSERT OR IGNORE INTO sync_pending SELECT ?, {joined}, '*', NULL FROM {table}",
            (table,),
        )
    conn.execute("UPDATE sync_state SET seeded = 1")


def _clocks(conn: sqlite3.Connection, table: str, rid: str) -> dict[str, list[str | None]]:
    return {
        f: [h, b]
        for f, h, b in conn.execute(
            "SELECT field, hlc, base_hlc FROM field_clock WHERE tbl = ? AND rid = ?", (table, rid)
        )
    }


def _put(store: Store, table: str, rid: str) -> dict[str, Any] | None:
    rec = row(store, table, {"id": rid})
    if rec is None:  # merged into another record since: nothing to send for this id
        return None
    local = synced.LOCAL_FIELDS.get(table, frozenset())
    entry: dict[str, Any] = {
        "op": "put",
        "t": table,
        "id": rid,
        "row": {k: encode(v) for k, v in rec.items() if k not in local},
        "clocks": {f: c for f, c in _clocks(store.conn, table, rid).items() if f not in local},
    }
    children = synced.CHILDREN.get(table, {})
    if children:
        entry["children"] = {}
        for child, column in children.items():
            cur = store.conn.execute(f"SELECT * FROM {child} WHERE {column} = ?", (rid,))
            names = [d[0] for d in cur.description]
            entry["children"][child] = [
                {n: encode(v) for n, v in zip(names, r, strict=True)} for r in cur.fetchall()
            ]
    return entry


def _set(
    store: Store, table: str, rid: str, fields: dict[str, str | None]
) -> dict[str, Any] | None:
    rec = row(store, table, {"id": rid})
    if rec is None:
        return None
    clocks = _clocks(store.conn, table, rid)
    return {
        "op": "set",
        "t": table,
        "id": rid,
        "fields": {
            f: {"v": encode(rec[f]), "hlc": clocks.get(f, [rec["updated_hlc"], None])[0],
                "base": base}
            for f, base in sorted(fields.items())
            if f in rec and f not in synced.LOCAL_FIELDS.get(table, frozenset())
        },
    }  # fmt: skip


def _link(store: Store, table: str, key: str) -> dict[str, Any] | None:
    columns = synced.LINKS[table]
    rec = row(store, table, dict(zip(columns, key.split("|"), strict=True)))
    if rec is None:
        return None
    return {"op": "link", "t": table, "row": {k: encode(v) for k, v in rec.items()}}


def _order(table: str) -> int:
    if table in synced.RECORDS:
        return synced.RECORDS.index(table)
    return len(synced.RECORDS) + list(synced.LINKS).index(table)


def export(store: Store) -> list[int]:
    """Turns what is pending into outbox objects; returns their sequence numbers."""
    made: list[int] = []
    with store.transaction() as conn:
        _seed(store, conn)
        pending: dict[tuple[str, str], dict[str, str | None]] = defaultdict(dict)
        for t, rid, field, base in conn.execute(
            "SELECT tbl, rid, field, base_hlc FROM sync_pending"
        ):
            pending[(t, rid)][field] = base
        if not pending:
            return made
        entries: list[dict[str, Any]] = []
        sources: list[str] = []
        for (table, rid), fields in sorted(
            pending.items(), key=lambda kv: (_order(kv[0][0]), kv[0][1])
        ):
            if table in synced.LINKS:
                entry = _link(store, table, rid)
            elif "*" in fields:
                entry = _put(store, table, rid)
                if entry is not None and table == "source_file":
                    sources.append(str(entry["row"]["sha256"]))
            else:
                entry = _set(store, table, rid, fields)
                if entry is not None and not entry["fields"]:  # only this laptop's own fields
                    entry = None
            if entry is not None:
                entries.append(entry)
        conn.execute("DELETE FROM sync_pending")
        schema = _schema(conn)
        for start in range(0, len(entries), MAX_ENTRIES):
            payload = {
                "format": FORMAT,
                "schema": schema,
                "laptop": store.laptop_id,
                "hlc": store.clock.now(),
                "entries": entries[start : start + MAX_ENTRIES],
            }
            made.append(_enqueue(store, conn, "records", payload))
        for sha in sources:
            loc = conn.execute(
                "SELECT path FROM source_location WHERE sha256 = ? AND path IS NOT NULL", (sha,)
            ).fetchone()
            if loc is None:  # received from another laptop, or its copy is gone: not ours to send
                continue
            src = row(store, "source_file", {"sha256": sha})
            assert src is not None
            payload = {"format": SOURCE_FORMAT, "sha256": sha, "kind": src["kind"],
                       "size": src["size"], "path": loc[0]}  # fmt: skip
            made.append(_enqueue(store, conn, "source", payload))
    return made


def _enqueue(store: Store, conn: sqlite3.Connection, kind: str, payload: dict[str, Any]) -> int:
    seq = int(conn.execute("SELECT next_sequence FROM sync_state").fetchone()[0])
    conn.execute(
        "INSERT INTO sync_outbox (sequence, kind, payload_json, created_hlc) VALUES (?, ?, ?, ?)",
        (seq, kind, json.dumps(payload, sort_keys=True, separators=(",", ":")), store.clock.now()),
    )
    conn.execute("UPDATE sync_state SET next_sequence = ?", (seq + 1,))
    return seq


def outbox(store: Store, limit: int = 50) -> list[dict[str, Any]]:
    """Objects not yet sent, oldest first: pending changes are exported first."""
    export(store)
    cur = store.conn.execute(
        "SELECT sequence, kind, payload_json FROM sync_outbox WHERE sent_hlc IS NULL "
        "ORDER BY sequence LIMIT ?",
        (int(limit),),
    )
    return [
        {"sequence": s, "kind": k, "laptop": store.laptop_id, "payload": json.loads(p)}
        for s, k, p in cur.fetchall()
    ]


def mark_sent(store: Store, sequence: int, object_key: str) -> None:
    """The shell wrote the object and the drive confirmed it. Its own objects are in the
    ledger too, so they are never applied back here."""
    with store.transaction() as conn:
        cur = conn.execute(
            "UPDATE sync_outbox SET object_key = ?, sent_hlc = ? WHERE sequence = ?",
            (object_key, store.clock.now(), sequence),
        )
        if cur.rowcount != 1:
            raise ValueError(f"no outbox object {sequence}")
        kind = conn.execute(
            "SELECT kind FROM sync_outbox WHERE sequence = ?", (sequence,)
        ).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO sync_ledger VALUES (?, ?, ?, ?, ?)",
            (object_key, store.laptop_id, sequence, kind[0], store.clock.now()),
        )
