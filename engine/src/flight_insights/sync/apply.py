"""Other laptops' objects, applied (spec: Shared storage and sync, "Checking incoming
data", "Two laptops editing the same thing", "No duplicates").

The shell hands over only objects that passed its checks: the sender was enrolled,
the signature and the encryption hold, and the plaintext hashes to the object's key.
Here an object is checked again for shape and sense, then applied in one transaction
with its ledger entry, so a crash leaves all of it or none of it:

- a new record is inserted with its clocks, unless this laptop already has the same
  thing under another id (the same flight or file imported on both): then the two
  merge into the record with the smaller id, on every laptop alike;
- a changed field takes the later of the two clocks. When neither change was made on
  top of the other and the values differ, both are kept as a conflict for a person,
  and the later still wins;
- a link is added once.

An object that refers to records not yet received waits and is offered again later;
one that makes no sense is quarantined with the reason, never applied, never
deleted. Applying an object twice changes nothing.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from flight_insights.knowledge import Pack
from flight_insights.store import _sqlcipher, synced
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store
from flight_insights.store.register import file_sha256
from flight_insights.sync.codec import columns, decode, encode, references, row
from flight_insights.sync.export import FORMAT, SOURCE_FORMAT
from flight_insights.sync.replay import replay

State = Literal["applied", "waiting", "quarantined"]


@dataclass(frozen=True)
class Applied:
    state: State
    reason: str | None = None

    def json(self) -> dict[str, Any]:
        return {"state": self.state, "reason": self.reason}


class Waiting(Exception):
    """The object refers to something not yet here."""


class Invalid(Exception):
    """The object makes no sense: quarantined."""


def quarantine(
    store: Store, object_key: str, laptop: str | None, sequence: int | None, reason: str
) -> Applied:
    """Keeps a failed object's place with the reason (which check, never a value)."""
    with store.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sync_quarantine VALUES (?, ?, ?, ?, ?)",
            (object_key, laptop, sequence, reason[:300], store.clock.now()),
        )
    return Applied("quarantined", reason)


def _known(store: Store, object_key: str, laptop: str, sequence: int) -> Applied | None:
    if store.conn.execute(
        "SELECT 1 FROM sync_ledger WHERE object_key = ?", (object_key,)
    ).fetchone():
        return Applied("applied", "already applied")
    if store.conn.execute(
        "SELECT 1 FROM sync_quarantine WHERE object_key = ?", (object_key,)
    ).fetchone():
        return Applied("quarantined", "already quarantined")
    other = store.conn.execute(
        "SELECT object_key FROM sync_ledger WHERE laptop_id = ? AND sequence = ?",
        (laptop, sequence),
    ).fetchone()
    if other:
        return quarantine(store, object_key, laptop, sequence, "a sequence number used twice")
    return None


def _alias(conn: sqlite3.Connection, table: str, rid: Any) -> Any:
    found = conn.execute(
        "SELECT new_id FROM sync_alias WHERE tbl = ? AND old_id = ?", (table, rid)
    ).fetchone()
    return found[0] if found else rid


class _Apply:
    def __init__(self, store: Store, conn: sqlite3.Connection, writer: str) -> None:
        self.store, self.conn, self.writer = store, conn, writer
        self.refs = references(conn)
        # (table, column) -> the table it refers to, for remapping merged ids
        self.points: dict[tuple[str, str], str] = {
            (t, c): target for target, uses in self.refs.items() for t, c in uses
        }

    def _cols(self, table: str) -> list[str]:
        return columns(self.conn, table)

    def _known(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        """Only this table's own columns ever reach a statement."""
        if set(values) - set(self._cols(table)):
            raise Waiting(f"{table} has fields this laptop does not know yet: update the app")
        return values

    def _remap(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        out = dict(values)
        for col, v in values.items():
            target = self.points.get((table, col))
            if target is not None and v is not None:
                out[col] = _alias(self.conn, target, v)
        return out

    def _stamp(self, hlc: str | None) -> None:
        if hlc:
            self.store.clock.receive(hlc)

    # --- fields ---------------------------------------------------------------------

    def merge_field(
        self,
        table: str,
        rid: str,
        field: str,
        value: Any,
        hlc: str,
        base: str | None,
        changed: bool = True,
    ) -> None:
        """One field from another laptop. `changed` says whether someone set it since
        the record was made; a value no one chose to change loses to one someone did,
        whatever the clocks say (a twin's starting value, against a decision made on
        the other copy). Otherwise the later clock wins, and when neither change was
        made on top of the other and the values differ, both are kept as a conflict."""
        current = row(self.store, table, {"id": rid})
        assert current is not None
        clock = self.conn.execute(
            "SELECT hlc, base_hlc FROM field_clock WHERE tbl = ? AND rid = ? AND field = ?",
            (table, rid, field),
        ).fetchone()
        local_changed = clock is not None
        local_hlc, local_base = (clock[0], clock[1]) if clock else (current["created_hlc"], None)
        local = current[field]
        if hlc == local_hlc:
            return  # the same change, seen again
        if changed != local_changed:
            incoming_wins, concurrent = changed, False
        else:
            pending = self.conn.execute(
                "SELECT 1 FROM sync_pending WHERE tbl = ? AND rid = ? AND field = ?",
                (table, rid, field),
            ).fetchone()
            on_top_of_local = base is not None and base >= local_hlc and not pending
            on_top_of_incoming = local_base is not None and local_base >= hlc
            incoming_wins = hlc > local_hlc
            concurrent = changed and not (on_top_of_local or on_top_of_incoming)
        if local != value and concurrent:
            kept, kept_hlc, other, other_hlc = (
                (value, hlc, local, local_hlc) if incoming_wins else (local, local_hlc, value, hlc)
            )
            self.conn.execute(
                "INSERT INTO sync_conflict (tbl, rid, field, kept_json, kept_hlc, other_json, "
                "other_hlc, at_hlc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (table, rid, field, json.dumps(encode(kept)), kept_hlc,
                 json.dumps(encode(other)), other_hlc, self.store.clock.now()),
            )  # fmt: skip
        if not incoming_wins:
            return
        if local != value:
            self.conn.execute(
                f"UPDATE {table} SET {field} = ?, version = version + 1, "
                "updated_hlc = max(updated_hlc, ?) WHERE id = ?",
                (value, hlc, rid),
            )
        if changed:
            self.conn.execute(
                "INSERT INTO field_clock VALUES (?, ?, ?, ?, ?) ON CONFLICT (tbl, rid, field) "
                "DO UPDATE SET hlc = excluded.hlc, base_hlc = excluded.base_hlc",
                (table, rid, field, hlc, base),
            )

    # --- entries --------------------------------------------------------------------

    def put(self, e: dict[str, Any]) -> None:
        table = str(e["t"])
        local = synced.LOCAL_FIELDS.get(table, frozenset())
        incoming = {
            k: decode(v) for k, v in cast(dict[str, Any], e["row"]).items() if k not in local
        }
        incoming = self._remap(table, self._known(table, incoming))
        rid = _alias(self.conn, table, str(incoming["id"]))
        incoming["id"] = rid
        clocks = cast(dict[str, list[str | None]], e.get("clocks") or {})
        self._stamp(incoming.get("updated_hlc"))
        natural = synced.natural_key(table, incoming)
        if row(self.store, table, {"id": rid}) is None and natural:
            where = " AND ".join(f"{c} IS ?" for c in natural)
            twin = self.conn.execute(
                f"SELECT id FROM {table} WHERE {where}", list(natural.values())
            ).fetchone()
            if twin is not None and twin[0] != rid:
                self._merge_twins(table, str(twin[0]), incoming, clocks, e)
                return
        if row(self.store, table, {"id": rid}) is not None:
            self._merge_row(table, rid, incoming, clocks)
            return
        self._insert(table, incoming, clocks, e)

    def _insert(
        self,
        table: str,
        values: dict[str, Any],
        clocks: dict[str, list[str | None]],
        e: dict[str, Any],
    ) -> None:
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        self.conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(values.values()))
        for field, (hlc, base) in clocks.items():
            self.conn.execute(
                "INSERT OR REPLACE INTO field_clock VALUES (?, ?, ?, ?, ?)",
                (table, values["id"], field, hlc, base),
            )
        for child, rows in cast(dict[str, list[dict[str, Any]]], e.get("children") or {}).items():
            if child not in synced.CHILDREN.get(table, {}):
                raise Invalid(f"{child} is not a child of {table}")
            for r in rows:
                vals = self._remap(child, self._known(child, {k: decode(v) for k, v in r.items()}))
                vals[synced.CHILDREN[table][child]] = values["id"]
                self.conn.execute(
                    f"INSERT OR IGNORE INTO {child} ({', '.join(vals)}) "
                    f"VALUES ({', '.join('?' for _ in vals)})",
                    list(vals.values()),
                )

    def _merge_row(
        self, table: str, rid: str, values: dict[str, Any], clocks: dict[str, list[str | None]]
    ) -> None:
        for field, value in values.items():
            if field in ("id", "created_hlc", "updated_hlc", "version", "laptop_id"):
                continue
            hlc, base = clocks.get(field, [values["created_hlc"], None])
            assert hlc is not None
            self.merge_field(table, rid, field, value, hlc, base, changed=field in clocks)

    def _clocks_of(self, table: str, rid: str) -> dict[str, list[str | None]]:
        return {
            f: [h, b]
            for f, h, b in self.conn.execute(
                "SELECT field, hlc, base_hlc FROM field_clock WHERE tbl = ? AND rid = ?",
                (table, rid),
            )
        }

    def _alias_to(self, table: str, gone: str, keep: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO sync_alias VALUES (?, ?, ?)", (table, gone, keep))
        self.conn.execute(  # one hop, always: what pointed at `gone` points at `keep`
            "UPDATE sync_alias SET new_id = ? WHERE tbl = ? AND new_id = ?", (keep, table, gone)
        )

    def _repoint(self, table: str, gone: str, keep: str) -> None:
        """Every reference to `gone` moves to `keep`. Records that become the same thing
        by it (two recommendations, now for one flight) merge too, so the laptops agree
        whatever order the objects arrived in."""
        for t, c in self.refs.get(table, []):
            self.conn.execute(f"UPDATE OR IGNORE {t} SET {c} = ? WHERE {c} = ?", (keep, gone))
            if t in synced.LINKS or any(t in kids for kids in synced.CHILDREN.values()):
                # A link or child row both records had is already there under the kept id.
                self.conn.execute(f"DELETE FROM {t} WHERE {c} = ?", (gone,))
            if t in synced.SYNCED and c in synced.NATURAL_KEYS.get(t, ()):
                self._merge_local_twins(t, c, keep)

    def _merge_local_twins(self, table: str, column: str, value: str) -> None:
        groups: dict[str, list[str]] = {}
        cur = self.conn.execute(f"SELECT * FROM {table} WHERE {column} = ?", (value,))
        names = [d[0] for d in cur.description]
        for r in cur.fetchall():
            rec = dict(zip(names, r, strict=True))
            key = synced.natural_key(table, rec)
            if key is not None:
                groups.setdefault(json.dumps(encode(list(key.values()))), []).append(str(rec["id"]))
        for ids in groups.values():
            keep, *others = sorted(ids)
            for gone in others:
                self._fold(table, gone, keep)

    def _fold(self, table: str, gone: str, keep: str) -> None:
        """Two records on this laptop that are the same thing: `gone` merges into `keep`."""
        other = row(self.store, table, {"id": gone})
        if other is None:
            return
        clocks = self._clocks_of(table, gone)
        self._alias_to(table, gone, keep)
        self.conn.execute("DELETE FROM field_clock WHERE tbl = ? AND rid = ?", (table, gone))
        self.conn.execute(f"DELETE FROM {table} WHERE id = ?", (gone,))
        self._repoint(table, gone, keep)
        self._merge_row(table, keep, other | {"id": keep}, clocks)
        self._move_pending(table, gone, keep)

    def _move_pending(self, table: str, gone: str, keep: str) -> None:
        # changes not yet sent go out under the kept id
        self.conn.execute(
            "UPDATE OR IGNORE sync_pending SET rid = ? WHERE tbl = ? AND rid = ?",
            (keep, table, gone),
        )
        self.conn.execute("DELETE FROM sync_pending WHERE tbl = ? AND rid = ?", (table, gone))

    def _merge_twins(
        self,
        table: str,
        local_id: str,
        incoming: dict[str, Any],
        clocks: dict[str, list[str | None]],
        e: dict[str, Any],
    ) -> None:
        """The same flight or file created on two laptops: one record, the smaller id,
        on every laptop, with the other's references moved to it."""
        incoming_id = str(incoming["id"])
        keep, gone = sorted((local_id, incoming_id))
        self._alias_to(table, gone, keep)
        if keep == local_id:
            self._merge_row(table, local_id, incoming | {"id": local_id}, clocks)
            return
        # The incoming id wins: this laptop's record moves under it, and its values merge
        # into the kept record as changes with their own clocks.
        local = row(self.store, table, {"id": local_id})
        assert local is not None
        local_clocks = self._clocks_of(table, local_id)
        self.conn.execute(f"DELETE FROM {table} WHERE id = ?", (local_id,))
        self.conn.execute("DELETE FROM field_clock WHERE tbl = ? AND rid = ?", (table, local_id))
        self._insert(table, incoming, clocks, e)
        self._repoint(table, local_id, keep)
        self._merge_row(table, keep, local | {"id": keep}, local_clocks)
        self._move_pending(table, local_id, keep)

    def set(self, e: dict[str, Any]) -> None:
        table = str(e["t"])
        rid = _alias(self.conn, table, str(e["id"]))
        if row(self.store, table, {"id": rid}) is None:
            raise Waiting(f"a change to a {table} record not yet received")
        known = set(self._cols(table))
        for field, change in cast(dict[str, dict[str, Any]], e["fields"]).items():
            if field not in known:
                raise Waiting(f"{table} has fields this laptop does not know yet: update the app")
            if field in ("id", "created_hlc", "version", "laptop_id"):
                raise Invalid(f"a change to {table}.{field}, which no change may touch")
            if field in synced.LOCAL_FIELDS.get(table, frozenset()):
                continue  # names a row on the writer's laptop only
            value = self._remap(table, {field: decode(change["v"])})[field]
            self._stamp(change["hlc"])
            self.merge_field(table, rid, field, value, str(change["hlc"]), change.get("base"))

    def link(self, e: dict[str, Any]) -> None:
        table = str(e["t"])
        raw = {k: decode(v) for k, v in cast(dict[str, Any], e["row"]).items()}
        vals = self._remap(table, self._known(table, raw))
        self.conn.execute(
            f"INSERT OR IGNORE INTO {table} ({', '.join(vals)}) "
            f"VALUES ({', '.join('?' for _ in vals)})",
            list(vals.values()),
        )


def _check(payload: Any, writer: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise Invalid("not a records object")
    p = cast(dict[str, Any], payload)
    if p.get("format") != FORMAT:
        raise Invalid("not a records object this app reads")
    if p.get("laptop") != writer:
        raise Invalid("written by a different laptop than the one that signed it")
    entries = p.get("entries")
    if not isinstance(entries, list):
        raise Invalid("no entries")
    for e in cast(list[Any], entries):
        if not isinstance(e, dict):
            raise Invalid("an entry that is not an object")
        op, table = cast(dict[str, Any], e).get("op"), cast(dict[str, Any], e).get("t")
        if op in ("put", "set") and table not in synced.SYNCED:
            raise Invalid(f"a record of {table!r}, which does not sync")
        if op == "link" and table not in synced.LINKS:
            raise Invalid(f"a link in {table!r}, which is not a link table")
        if op not in ("put", "set", "link"):
            raise Invalid("an entry of an unknown kind")
    return cast(list[dict[str, Any]], entries)


def apply_records(
    store: Store, object_key: str, writer: str, sequence: int, payload: Any
) -> Applied:
    """One records object from another laptop, all or nothing."""
    done = _known(store, object_key, writer, sequence)
    if done is not None:
        return done
    try:
        entries = _check(payload, writer)
        schema = int(cast(dict[str, Any], payload).get("schema") or 0)
        mine = int(store.conn.execute("SELECT max(number) FROM schema_migrations").fetchone()[0])
        if schema > mine:
            return Applied("waiting", "written by a newer version of the app: update this one")
        with store.applying() as conn:
            work = _Apply(store, conn, writer)
            for e in entries:
                getattr(work, str(e["op"]))(e)
            conn.execute(
                "INSERT INTO sync_ledger VALUES (?, ?, ?, ?, ?)",
                (object_key, writer, sequence, "records", store.clock.now()),
            )
    except Waiting as w:
        return Applied("waiting", str(w))
    except (sqlite3.IntegrityError, _sqlcipher.IntegrityError) as e:
        if "FOREIGN KEY" in str(e).upper():
            return Applied("waiting", "refers to records not yet received")
        return quarantine(store, object_key, writer, sequence, "does not fit this laptop's records")
    except (sqlite3.DatabaseError, _sqlcipher.DatabaseError):
        return quarantine(store, object_key, writer, sequence, "does not fit this laptop's records")
    except (Invalid, KeyError, TypeError, ValueError) as e:
        reason = str(e) if isinstance(e, Invalid) else "not shaped like a records object"
        return quarantine(store, object_key, writer, sequence, reason)
    return Applied("applied")


def apply_source(
    store: Store,
    cache: ColumnCache,
    pack: Pack,
    object_key: str,
    writer: str,
    sequence: int,
    manifest: Any,
    path: Path,
) -> Applied:
    """A source file from another laptop, decrypted by the shell to `path` for the length
    of this call: its flights' columns go into this laptop's encrypted cache."""
    done = _known(store, object_key, writer, sequence)
    if done is not None:
        return done
    if (
        not isinstance(manifest, dict)
        or cast(dict[str, Any], manifest).get("format") != SOURCE_FORMAT
    ):
        return quarantine(store, object_key, writer, sequence, "not a source object this app reads")
    m = cast(dict[str, Any], manifest)
    sha = str(m.get("sha256"))
    if file_sha256(path) != sha:
        return quarantine(store, object_key, writer, sequence, "the file is not the one it names")
    if row(store, "source_file", {"sha256": sha}) is None:
        return Applied("waiting", "its record has not arrived yet")
    try:
        replay(store, cache, pack, sha, path)
    except Exception:  # an unreadable log is the object's problem, not this laptop's
        return quarantine(store, object_key, writer, sequence, "the file cannot be read")
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO source_location (sha256, object_key, seen_hlc) VALUES (?, ?, ?) "
            "ON CONFLICT (sha256) DO UPDATE SET object_key = excluded.object_key",
            (sha, object_key, store.clock.now()),
        )
        conn.execute(
            "INSERT INTO sync_ledger VALUES (?, ?, ?, ?, ?)",
            (object_key, writer, sequence, "source", store.clock.now()),
        )
    return Applied("applied")


def unknown(store: Store, object_keys: list[str]) -> list[str]:
    """The keys among these that were never applied or quarantined here: the objects
    worth downloading."""
    seen: set[str] = set()
    for start in range(0, len(object_keys), 500):
        part = object_keys[start : start + 500]
        marks = ", ".join("?" for _ in part)
        for table in ("sync_ledger", "sync_quarantine"):
            seen |= {
                k for (k,) in store.conn.execute(
                    f"SELECT object_key FROM {table} WHERE object_key IN ({marks})", part
                )
            }  # fmt: skip
    return [k for k in object_keys if k not in seen]


BOOKKEEPING = frozenset({"version", "updated_hlc", "laptop_id"})  # each laptop's own count


def digest(store: Store, show: frozenset[str] = frozenset()) -> dict[str, Any]:
    """A hash of everything that syncs, per table, without each laptop's bookkeeping
    or its laptop-only fields: two laptops that hold the same records agree on it.
    The tables in `show` come with their rows, to find where two laptops differ."""
    tables = [*synced.RECORDS, *synced.LINKS, *(c for k in synced.CHILDREN.values() for c in k)]
    out: dict[str, Any] = {}
    every = hashlib.sha256()
    for table in tables:
        cur = store.conn.execute(f"SELECT * FROM {table}")
        names = [d[0] for d in cur.description]
        drop = BOOKKEEPING | synced.LOCAL_FIELDS.get(table, frozenset())
        rows = sorted(
            json.dumps({n: encode(v) for n, v in zip(names, r, strict=True) if n not in drop},
                       sort_keys=True)
            for r in cur.fetchall()
        )  # fmt: skip
        h = hashlib.sha256("\n".join(rows).encode()).hexdigest()
        out[table] = {"rows": len(rows), "sha256": h}
        if table in show:
            out[table]["data"] = rows
        every.update(f"{table}:{h};".encode())
    return {"sha256": every.hexdigest(), "tables": out}


def conflicts(store: Store, include_resolved: bool = False) -> list[dict[str, Any]]:
    cur = store.conn.execute(
        "SELECT id, tbl, rid, field, kept_json, kept_hlc, other_json, other_hlc, at_hlc, resolved "
        "FROM sync_conflict WHERE resolved = 0 OR ? ORDER BY id",
        (int(include_resolved),),
    )
    names = [d[0] for d in cur.description]
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        c = dict(zip(names, r, strict=True))
        c["kept"], c["other"] = json.loads(c.pop("kept_json")), json.loads(c.pop("other_json"))
        c["resolved"] = bool(c["resolved"])
        out.append(c)
    return out


def status(store: Store) -> dict[str, Any]:
    """What the sync panel shows: waiting to go, applied per laptop with any gaps, the
    quarantine and the open conflicts."""
    conn = store.conn
    waiting = conn.execute("SELECT count(*) FROM sync_outbox WHERE sent_hlc IS NULL").fetchone()[0]
    pending = conn.execute("SELECT count(*) FROM sync_pending").fetchone()[0]
    laptops: list[dict[str, Any]] = []
    for laptop, top, n in conn.execute(
        "SELECT laptop_id, max(sequence), count(*) FROM sync_ledger WHERE laptop_id != ? "
        "GROUP BY laptop_id",
        (store.laptop_id,),
    ):
        got = {
            s
            for (s,) in conn.execute(
                "SELECT sequence FROM sync_ledger WHERE laptop_id = ?", (laptop,)
            )
        }
        quarantined = {
            s for (s,) in conn.execute(
                "SELECT sequence FROM sync_quarantine WHERE laptop_id = ? AND sequence IS NOT NULL",
                (laptop,),
            )
        }  # fmt: skip
        gaps = [s for s in range(1, int(top) + 1) if s not in got and s not in quarantined]
        laptops.append({"laptop": laptop, "latest": int(top), "objects": int(n), "gaps": gaps[:50]})
    quarantine_rows = [
        {"object_key": k, "laptop": lap, "sequence": s, "reason": r, "at": at}
        for k, lap, s, r, at in conn.execute(
            "SELECT object_key, laptop_id, sequence, reason, at_hlc FROM sync_quarantine "
            "ORDER BY at_hlc"
        )
    ]
    return {
        "waiting_to_send": int(waiting) + (1 if pending else 0),
        "laptops": laptops,
        "quarantine": quarantine_rows,
        "conflicts": len(conflicts(store)),
    }
