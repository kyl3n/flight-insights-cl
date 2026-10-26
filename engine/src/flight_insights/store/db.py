"""The entity database: SQLite encrypted with SQLCipher (spec: Storage).

The database key is the raw 32-byte key from the shell, so SQLCipher's
passphrase stretching is skipped. A wrong key fails at open. Schema changes are
numbered SQL files in migrations/, each applied once, in its own transaction,
and recorded in schema_migrations.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import threading
from collections.abc import Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any, Self

from flight_insights.store import _sqlcipher, synced
from flight_insights.store.hlc import HybridClock
from flight_insights.store.keys import StoreKeys, check_laptop_id, short_code

MIGRATION_FILE = re.compile(r"(\d{4})_(\w+)\.sql")
# Columns every record has, set by the store, never by callers.
RECORD_COLUMNS = ("id", "created_hlc", "updated_hlc", "version", "laptop_id")


class StoreError(RuntimeError):
    """The store could not be opened, or a change could not be made."""


class Rows:
    """A statement's results, read in full while the store's lock was held."""

    def __init__(
        self, rows: list[Any], description: Any, rowcount: int, lastrowid: int | None
    ) -> None:
        self._rows = iter(rows)
        self.description = description
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def __iter__(self) -> Iterator[Any]:
        return self._rows

    def fetchone(self) -> Any:
        return next(self._rows, None)

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class Connection:
    """The database connection, one statement at a time across threads.

    The engine's request threads share one connection, and sqlite3 connections are not
    safe for concurrent use: two threads stepping statements at once corrupt the
    connection's statement cache. Every statement here runs, and has its rows read in
    full, under the store's lock, which a transaction holds from BEGIN to COMMIT, so no
    thread interleaves with another's statement or sees another's open transaction.
    """

    def __init__(self, raw: sqlite3.Connection, lock: threading.RLock) -> None:
        self._raw = raw
        self._lock = lock

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Rows:
        with self._lock:
            cur = self._raw.execute(sql, params)
            rows = cur.fetchall() if cur.description is not None else []
            return Rows(rows, cur.description, cur.rowcount, cur.lastrowid)

    def executemany(self, sql: str, params: Iterable[Sequence[Any]]) -> Rows:
        with self._lock:
            cur = self._raw.executemany(sql, params)
            return Rows([], None, cur.rowcount, cur.lastrowid)

    def executescript(self, script: str) -> None:
        with self._lock:
            self._raw.executescript(script)

    @property
    def in_transaction(self) -> bool:
        with self._lock:
            return self._raw.in_transaction

    def close(self) -> None:
        with self._lock:
            self._raw.close()


def new_id() -> str:
    return secrets.token_hex(16)


def migrations() -> list[tuple[int, str, str]]:
    """Every migration as (number, name, SQL), in order."""
    found: list[tuple[int, str, str]] = []
    for entry in resources.files("flight_insights.store").joinpath("migrations").iterdir():
        m = MIGRATION_FILE.fullmatch(entry.name)
        if m:
            found.append((int(m[1]), m[2], entry.read_text(encoding="utf-8")))
    found.sort()
    if [n for n, _, _ in found] != list(range(1, len(found) + 1)):
        raise StoreError("migrations must be numbered 0001, 0002, ... with no gaps")
    return found


class Store:
    """One laptop's working copy of the fleet's records."""

    def __init__(self, conn: sqlite3.Connection, laptop_id: str, laptop_name: str | None) -> None:
        """Use open_store(). Brings the schema up to date and records this laptop."""
        self._lock = threading.RLock()  # one thread at a time; nested calls join in
        self.conn = Connection(conn, self._lock)
        self.laptop_id = laptop_id
        self.clock = HybridClock(laptop_id)
        self._depth = 0
        self._applying = 0  # inside sync's apply: changes from another laptop, not captured
        self._record_tables: frozenset[str] = frozenset()
        self._tables: frozenset[str] = frozenset()
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(number INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_hlc TEXT NOT NULL)"
        )
        self._load_tables()
        self._start_clock()
        self.applied_migrations = self._migrate()
        self._load_tables()
        if self.get("laptop", laptop_id) is None:
            self.insert("laptop", {"id": laptop_id, "name": laptop_name or short_code(laptop_id)})

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # --- transactions ---------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Generator[Connection]:
        """All of it or none of it. Nested calls join the outermost transaction; other
        threads wait until it ends."""
        with self._lock:
            if self._depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self.conn
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if self._depth == 0:
                try:
                    self.conn.execute("COMMIT")
                except BaseException:
                    # A deferred check (foreign keys) can refuse the commit itself.
                    if self.conn.in_transaction:
                        self.conn.execute("ROLLBACK")
                    raise

    # --- records --------------------------------------------------------------------

    def insert(self, table: str, values: Mapping[str, Any]) -> str:
        """Add a record, stamped as written by this laptop now. Returns its id."""
        self._check_table(table)
        record_id: str = values.get("id") or new_id()
        row: dict[str, Any] = {
            "id": record_id,
            "created_hlc": None,
            "updated_hlc": None,
            "version": 1,
            "laptop_id": self.laptop_id,
        }
        row |= {k: v for k, v in values.items() if k not in RECORD_COLUMNS}
        names = ", ".join(_ident(k) for k in row)
        marks = ", ".join("?" for _ in row)
        with self.transaction() as conn:
            # Stamped under the lock: two threads reading the clock at once share a stamp.
            row["created_hlc"] = row["updated_hlc"] = self.clock.now()
            conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(row.values()))
            if self._capturing(table):
                self._pending(conn, table, record_id, "*", None)
        return record_id

    def update(
        self, table: str, record_id: str, base_version: int, values: Mapping[str, Any]
    ) -> int:
        """Change a record that was read at `base_version`. Returns the new version.

        A record changed since then is not overwritten: the caller re-reads it and
        decides, so no change is silently lost.
        """
        self._check_table(table)
        changes = {k: v for k, v in values.items() if k not in RECORD_COLUMNS}
        sets = "".join(f"{_ident(k)} = ?, " for k in changes)
        with self.transaction() as conn:
            stamp = self.clock.now()
            capture = self._capturing(table)
            bases = {f: self.field_hlc(table, record_id, f) for f in changes} if capture else {}
            cur = conn.execute(
                f"UPDATE {table} SET {sets}version = version + 1, updated_hlc = ?, laptop_id = ? "
                "WHERE id = ? AND version = ?",
                [*changes.values(), stamp, self.laptop_id, record_id, base_version],
            )
            if cur.rowcount != 1:
                raise StoreError(f"{table} {record_id} is not at version {base_version}")
            for field, base in bases.items():
                self._pending(conn, table, record_id, field, base)
                conn.execute(
                    "INSERT INTO field_clock VALUES (?, ?, ?, ?, ?) ON CONFLICT (tbl, rid, field) "
                    "DO UPDATE SET hlc = excluded.hlc, base_hlc = excluded.base_hlc",
                    (table, record_id, field, stamp, base),
                )
        return base_version + 1

    def link(self, table: str, row: Mapping[str, Any]) -> bool:
        """Add a link between records (one of synced.LINKS) unless it is there; returns
        whether it was added."""
        if table not in synced.LINKS:
            raise StoreError(f"{table!r} is not a link table")
        names = ", ".join(_ident(k) for k in row)
        marks = ", ".join("?" for _ in row)
        with self.transaction() as conn:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({marks})", list(row.values())
            )
            added = cur.rowcount == 1
            if added and self._capturing(table):
                self._pending(conn, table, synced.link_key(table, dict(row)), "*", None)
        return added

    # --- sync capture ---------------------------------------------------------------

    @contextmanager
    def applying(self) -> Generator[Connection]:
        """A transaction applying another laptop's changes: they are not captured to be
        sent again, and foreign keys are checked when it commits, so records that refer
        to each other can arrive in one object in any order."""
        with self.transaction() as conn:
            conn.execute("PRAGMA defer_foreign_keys = ON")
            self._applying += 1
            try:
                yield conn
            finally:
                self._applying -= 1

    def _capturing(self, table: str) -> bool:
        return (
            not self._applying
            and (table in synced.SYNCED or table in synced.LINKS)
            and "sync_pending" in self._tables
        )

    def _pending(
        self, conn: Connection, table: str, rid: str, field: str, base: str | None
    ) -> None:
        # The first change since the last object keeps the field's earlier clock.
        conn.execute(
            "INSERT OR IGNORE INTO sync_pending VALUES (?, ?, ?, ?)", (table, rid, field, base)
        )

    def field_hlc(self, table: str, record_id: str, field: str) -> str | None:
        """The clock of a record's field: its last change, or the record's creation."""
        row = self.conn.execute(
            "SELECT hlc FROM field_clock WHERE tbl = ? AND rid = ? AND field = ?",
            (table, record_id, field),
        ).fetchone()
        if row:
            return str(row[0])
        created = self.conn.execute(
            f"SELECT created_hlc FROM {_ident(table)} WHERE id = ?", (record_id,)
        ).fetchone()
        return str(created[0]) if created else None

    def get(self, table: str, record_id: str) -> dict[str, Any] | None:
        self._check_table(table)
        with self._lock:
            cur = self.conn.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,))
            row = cur.fetchone()
            if row is None:
                return None
            return {d[0]: v for d, v in zip(cur.description, row, strict=True)}

    @property
    def record_tables(self) -> frozenset[str]:
        return self._record_tables

    @property
    def tables(self) -> frozenset[str]:
        return self._tables

    def _check_table(self, table: str) -> None:
        if table not in self._record_tables:
            raise StoreError(f"no record table named {table!r}")

    # --- opening --------------------------------------------------------------------

    def _migrate(self) -> list[int]:
        done = {n for (n,) in self.conn.execute("SELECT number FROM schema_migrations")}
        applied: list[int] = []
        for number, name, sql in migrations():
            if number in done:
                continue
            try:
                # executescript runs outside the store's own transaction bookkeeping,
                # so the migration wraps itself.
                self.conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\n;")
                self.conn.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                    (number, name, self.clock.now()),
                )
                self.conn.execute("COMMIT")
            except Exception as e:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise StoreError(f"migration {number:04d}_{name} failed: {e}") from e
            applied.append(number)
        return applied

    def _load_tables(self) -> None:
        tables: set[str] = set()
        every: set[str] = set()
        for (name,) in self.conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'"):
            every.add(name)
            columns = {r[1] for r in self.conn.execute(f"PRAGMA table_info({_ident(name)})")}
            if set(RECORD_COLUMNS) <= columns:
                tables.add(name)
        self._record_tables = frozenset(tables)
        self._tables = frozenset(every)

    def _start_clock(self) -> None:
        """Continue after the newest stamp stored, even if the wall clock stepped back."""
        stamps = [f"SELECT max(updated_hlc) FROM {t}" for t in sorted(self._record_tables)] + [
            "SELECT max(applied_hlc) FROM schema_migrations"
        ]
        for (stamp,) in self.conn.execute(" UNION ALL ".join(stamps)):
            if stamp:
                self.clock.observe(stamp)


def _ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise StoreError(f"not a column name: {name!r}")
    return name


def open_store(
    path: Path, keys: StoreKeys, laptop_id: str, laptop_name: str | None = None
) -> Store:
    """Open (creating if needed) the encrypted database, bring its schema up to date,
    and make sure this laptop has a record."""
    check_laptop_id(laptop_id)
    conn = _sqlcipher.connect(str(path))
    try:
        # Not cipher_memory_security: its page locking fails once a process passes the
        # OS lock quota (RLIMIT_MEMLOCK, or the working set behind VirtualLock), after
        # which SQLCipher logs a warning on every allocation, and on Windows the stack
        # overflowed. Full-disk encryption, which the spec requires, covers swapped pages.
        conn.execute(f"PRAGMA key = \"x'{keys.database.hex()}'\"")
        try:
            conn.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        except _sqlcipher.DatabaseError as e:
            raise StoreError("the database key does not open this file") from e
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return Store(conn, laptop_id, laptop_name)
    except BaseException:
        conn.close()
        raise
