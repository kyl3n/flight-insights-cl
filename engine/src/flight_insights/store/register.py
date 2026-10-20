"""Registering source files: each file is known by its content, once.

A file whose SHA-256 is already registered is a duplicate, whatever its name or
however many times it is imported (spec: Shared storage, "No duplicates"). The
same flight arriving in two different files is caught later, by its natural
identity.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from flight_insights.store.db import Store

SourceKind = Literal["dataflash", "tlog", "param", "params"]
CHUNK = 1 << 20


@dataclass(frozen=True)
class Registered:
    id: str
    sha256: str
    duplicate: bool  # True: this content was already registered; nothing was added


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def register_source(
    store: Store,
    path: Path,
    kind: SourceKind,
    ground_station_version_id: str | None = None,
) -> Registered:
    digest = file_sha256(path)
    with store.transaction() as conn:
        row = conn.execute("SELECT id FROM source_file WHERE sha256 = ?", (digest,)).fetchone()
        if row is not None:
            return Registered(row[0], digest, duplicate=True)
        source_id = store.insert(
            "source_file",
            {
                "sha256": digest,
                "kind": kind,
                "size": path.stat().st_size,
                "original_name": path.name,
                "ground_station_version_id": ground_station_version_id,
            },
        )
    return Registered(source_id, digest, duplicate=False)
