"""Row values as JSON, and the shape of what travels between laptops."""

from __future__ import annotations

import base64
import sqlite3
from typing import Any, cast

from flight_insights.store.db import Store


def encode(value: Any) -> Any:
    if isinstance(value, bytes | bytearray):
        return {"$b": base64.b64encode(bytes(value)).decode()}
    return value


def decode(value: Any) -> Any:
    if isinstance(value, dict):
        d = cast(dict[str, Any], value)
        if set(d) == {"$b"}:
            return base64.b64decode(str(d["$b"]))
        return d
    return value


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def row(store: Store, table: str, where: dict[str, Any]) -> dict[str, Any] | None:
    keys = " AND ".join(f"{k} = ?" for k in where)
    cur = store.conn.execute(f"SELECT * FROM {table} WHERE {keys}", list(where.values()))
    found = cur.fetchone()
    if found is None:
        return None
    return {d[0]: v for d, v in zip(cur.description, found, strict=True)}


def references(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    """For each table, the (table, column) pairs that refer to it by id."""
    out: dict[str, list[tuple[str, str]]] = {}
    for (table,) in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'"):
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
            out.setdefault(str(fk[2]), []).append((str(table), str(fk[3])))
    return out
