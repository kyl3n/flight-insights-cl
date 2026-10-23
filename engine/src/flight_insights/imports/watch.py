"""Which files found in watched folders are new: not stored, and not set aside by
someone who chose not to import them."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from flight_insights.imports.scan import kind_of
from flight_insights.store.db import Store
from flight_insights.store.register import file_sha256


def unknown(store: Store, paths: Sequence[Path]) -> list[Path]:
    out: list[Path] = []
    for path in paths:
        if kind_of(path) is None or not path.is_file():
            continue
        try:
            sha = file_sha256(path)
        except OSError:  # locked or gone: not offered now, tried again at the next look
            continue
        stored = store.conn.execute(
            "SELECT 1 FROM source_file WHERE sha256 = ? UNION ALL "
            "SELECT 1 FROM ignored_file WHERE sha256 = ?",
            (sha, sha),
        ).fetchone()
        if not stored:
            out.append(path)
    return out


def ignore(store: Store, paths: Sequence[Path]) -> int:
    """Remember files a person chose not to import, by content, so they are not
    offered again. Returns how many were newly set aside."""
    added = 0
    with store.transaction() as conn:
        for path in paths:
            if not path.is_file():
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO ignored_file (sha256, name, ignored_hlc) VALUES (?, ?, ?)",
                (file_sha256(path), path.name[:200], store.clock.now()),
            )
            added += cur.rowcount
    return added
