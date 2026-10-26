"""The weather the shell downloaded, kept in the encrypted store (spec: Weather and wind,
"Fixed schedule" and "Offline").

The shell owns the requests: which source, which tile, which period, and when. Each
response it receives is handed over as a file and kept here as it came, compressed,
until the engine turns it into each flight's weather record. The engine also keeps when
this laptop first listed each tile, so the shell can wait its random delay before the
tile's backfill, across restarts.

A period names what a download covers, in UTC: a month ("2026-10", a reanalysis
backfill), a day ("2026-10-20", a day of reanalysis) or a 6-hour forecast slot
("2026-10-26T06"). Space weather has no tile.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterable
from typing import Any, cast

from flight_insights.store.db import Store
from flight_insights.weather.tiles import TILE

SOURCES = ("forecast", "reanalysis", "kp")
PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01])(T([01]\d|2[0-3]))?)?$")
MAX_BYTES = 64 * 1024 * 1024


class DownloadError(ValueError):
    pass


def note_tiles(store: Store, tiles: Iterable[str], now_ms: int) -> dict[str, int]:
    """When this laptop first listed each tile: now for a tile not seen before."""
    tiles = sorted(set(tiles))
    with store.transaction() as conn:
        conn.executemany(
            "INSERT INTO weather_tile (tile, added_ms) VALUES (?, ?) ON CONFLICT DO NOTHING",
            [(t, now_ms) for t in tiles],
        )
        rows: Iterable[Any] = conn.execute("SELECT tile, added_ms FROM weather_tile").fetchall()
    added = {str(t): int(ms) for t, ms in rows}
    return {t: added[t] for t in tiles}


def _check(source: str, tile: str, period: str) -> None:
    if source not in SOURCES:
        raise DownloadError(f"source must be one of {', '.join(SOURCES)}")
    if source == "kp" and tile:
        raise DownloadError("space weather has no tile")
    if source != "kp" and not TILE.match(tile):
        raise DownloadError("not a tile name")
    if not PERIOD.match(period):
        raise DownloadError("period must be a month, a day or a 6-hour slot, in UTC")


def keep(
    store: Store, source: str, tile: str, period: str, body: bytes, now_ms: int
) -> int:  # fmt: skip
    """Keeps one download as it came; returns its size compressed. A body that is not
    JSON, or is the provider's error reply, is refused."""
    _check(source, tile, period)
    if len(body) > MAX_BYTES:
        raise DownloadError("the download is larger than any reply the schedule asks for")
    try:
        doc = json.loads(body)
    except ValueError:
        raise DownloadError("the download is not JSON") from None
    if isinstance(doc, dict) and cast(dict[str, Any], doc).get("error") is True:
        raise DownloadError("the download is the provider's error reply")
    packed = gzip.compress(body, mtime=0)
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO weather_download (source, tile, period, fetched_ms, body) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (source, tile, period) DO UPDATE SET "
            "fetched_ms = excluded.fetched_ms, body = excluded.body",
            (source, tile, period, now_ms, packed),
        )
    return len(packed)


def held(store: Store, source: str, since: str = "") -> list[tuple[str, str]]:
    """The (tile, period) downloads kept for a source, periods from `since` on."""
    if source not in SOURCES:
        raise DownloadError(f"source must be one of {', '.join(SOURCES)}")
    rows: Iterable[Any] = store.conn.execute(
        "SELECT tile, period FROM weather_download WHERE source = ? AND period >= ? "
        "ORDER BY tile, period",
        (source, since),
    ).fetchall()
    return [(str(t), str(p)) for t, p in rows]


def body(store: Store, source: str, tile: str, period: str) -> bytes | None:
    """One download as it came, or None."""
    row = store.conn.execute(
        "SELECT body FROM weather_download WHERE source = ? AND tile = ? AND period = ?",
        (source, tile, period),
    ).fetchone()
    return None if row is None else gzip.decompress(row[0])
