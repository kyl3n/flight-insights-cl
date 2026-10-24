"""A source file from another laptop, read into this laptop's cache (spec: Shared
storage and sync, "Each laptop recomputes analysis").

The flights in the file arrived as records already, with the other laptop's flight
keys. Keys hash the board ID with a laptop's own key, so they are not recomputed
here: each flight read from the file is matched to its record by when it armed
(boot time and arming time, both read from the file itself), and its columns are
cached under the record's key. Analysis then runs on them as on any import.
"""

from __future__ import annotations

from pathlib import Path

from flight_insights.dataflash import read_dataflash
from flight_insights.history.snapshot import from_log
from flight_insights.imports.commit import cache_flights
from flight_insights.imports.scan import ScannedFlight
from flight_insights.knowledge import Pack
from flight_insights.segment import boot_identities, find_boots, flights, timeline
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store


def replay(store: Store, cache: ColumnCache, pack: Pack, sha256: str, path: Path) -> list[str]:
    """Caches the flights of a received file that this laptop has records for and no
    columns yet; returns their ids."""
    records = store.conn.execute(
        "SELECT f.id, f.flight_key, f.boot_utc_us, f.arm_us FROM flight f "
        "JOIN flight_source fs ON fs.flight_id = f.id "
        "JOIN source_file s ON s.id = fs.source_file_id "
        "WHERE s.sha256 = ? AND s.kind = 'dataflash'",
        (sha256,),
    ).fetchall()
    wanted = {(utc, arm): (fid, key) for fid, key, utc, arm in records if not cache.names(key)}
    if not wanted:
        return []
    with read_dataflash(path) as df:
        snapshot = from_log(df)
        boots = find_boots(timeline(df))
        ids = boot_identities(df, boots)
        matched: list[ScannedFlight] = []
        done: list[str] = []
        for f in flights(df, boots, pack, snapshot.vehicle, snapshot.firmware):
            found = wanted.get((ids[f.boot].boot_utc_us, f.arm_us))
            if found is None:
                continue
            fid, key = found
            boot = ids[f.boot]
            matched.append(
                ScannedFlight(
                    f.number,
                    f.boot,
                    key,
                    f.arm_us,
                    f.disarm_us,
                    f.duration_s,
                    boot.boot_utc_us,
                    boot.boot_count,
                    fid,
                )
            )
            done.append(fid)
        cache_flights(df, cache, matched)
    return done
