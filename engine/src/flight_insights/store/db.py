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
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any, Self

from flight_insights.store import _sqlcipher
from flight_insights.store.hlc import HybridClock
from flight_insights.store.keys import StoreKeys, check_laptop_id, short_code

MIGRATION_FILE = re.compile(r"(\d{4})_(\w+)\.sql")
# Columns every record has, set by the store, never by callers.
RECORD_COLUMNS = ("id", "created_hlc", "updated_hlc", "version", "laptop_id")


class StoreError(RuntimeError):
    """The store could not be opened, or a change could not be made."""


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
        self.conn = conn
        self.laptop_id = laptop_id
        self.clock = HybridClock(laptop_id)
        self._depth = 0
        self._lock = threading.RLock()  # one thread at a time; nested calls join in
        self._record_tables: frozenset[str] = frozenset()
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
    def transaction(self) -> Generator[sqlite3.Connection]:
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
                self.conn.execute("COMMIT")

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
            cur = conn.execute(
                f"UPDATE {table} SET {sets}version = version + 1, updated_hlc = ?, laptop_id = ? "
                "WHERE id = ? AND version = ?",
                [*changes.values(), self.clock.now(), self.laptop_id, record_id, base_version],
            )
            if cur.rowcount != 1:
                raise StoreError(f"{table} {record_id} is not at version {base_version}")
        return base_version + 1

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
        for (name,) in self.conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'"):
            columns = {r[1] for r in self.conn.execute(f"PRAGMA table_info({_ident(name)})")}
            if set(RECORD_COLUMNS) <= columns:
                tables.add(name)
        self._record_tables = frozenset(tables)

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
