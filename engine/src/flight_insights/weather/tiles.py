"""The weather tiles the fleet downloads (spec: Weather and wind, "How the location
stays private"; rules in `knowledge/rules/weather.json`).

Each flight's positions are snapped, on the laptop, to a fixed grid of large tiles. The
tiles a flight flew in, and the ring of tiles around each, join the download list for
good. A tile is named by its south-west corner, "N50E030" or "S34W058"; the name is all
that leaves this module, and only for the shell, which asks the weather provider for
the tile's whole grid of points. The positions themselves are read with the positions
key and dropped once snapped.

The estimator's positions (POS) are used, or, in a log without them, GPS positions
with a 3D fix. A tile counts when it holds at least `min_share` of the flight's
positions, so a few samples from a GNSS glitch or a spoofed jump add nothing. Each
flight's tiles are kept in `flight_tiles`, a local table: every laptop has the logs and
finds the same tiles, like analysis results.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.dataflash import si
from flight_insights.knowledge import Pack
from flight_insights.store.cache import CacheError, ColumnCache
from flight_insights.store.db import Store

TILE = re.compile(r"^([NS])(\d{2})([EW])(\d{3})$")
Floats = np.ndarray[Any, np.dtype[np.float64]]


class TileError(ValueError):
    pass


@dataclass(frozen=True)
class TileRules:
    tile_deg: int
    neighbours: int
    min_share: float
    gps_min_status: int
    grids_deg: Mapping[str, float]

    @staticmethod
    def load(pack: Pack) -> TileRules:
        r: Mapping[str, Any] = pack.json("rules/weather.json")
        deg = r["tile_deg"]
        if not isinstance(deg, int) or deg < 1 or 180 % deg:
            raise TileError(f"tile_deg must be a whole number of degrees dividing 180: {deg}")
        rules = TileRules(deg, int(r["neighbours"]), float(r["min_share"]),
                          int(r["gps_min_status"]), dict(r["grids_deg"]))  # fmt: skip
        for source, step in rules.grids_deg.items():
            if abs(deg / step - round(deg / step)) > 1e-9:
                raise TileError(f"{source}: a {step} degree grid does not fit a {deg} degree tile")
        return rules

    @property
    def basis(self) -> str:
        """What a flight's tiles depend on: they are found again when it changes."""
        return f"{self.tile_deg}/{self.min_share}/{self.gps_min_status}"


def name(lat_sw: int, lon_sw: int) -> str:
    """A tile's name from its south-west corner in whole degrees."""
    ns, ew = "N" if lat_sw >= 0 else "S", "E" if lon_sw >= 0 else "W"
    return f"{ns}{abs(lat_sw):02d}{ew}{abs(lon_sw):03d}"


def corner(tile: str) -> tuple[int, int]:
    """A tile's south-west corner (latitude, longitude) in whole degrees."""
    m = TILE.match(tile)
    if m is None:
        raise TileError(f"not a tile name: {tile!r}")
    lat = int(m[2]) * (1 if m[1] == "N" else -1)
    lon = int(m[4]) * (1 if m[3] == "E" else -1)
    if not -90 <= lat < 90 or not -180 <= lon < 180:
        raise TileError(f"not a tile name: {tile!r}")
    return lat, lon


def _cells(lat: Floats, lon: Floats, deg: int) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any]]:
    """Each position's tile as (row from the south pole, column from 180 W)."""
    rows = np.clip(np.floor((lat + 90.0) / deg), 0, 180 // deg - 1).astype(np.int64)
    cols = np.floor(np.mod(lon + 180.0, 360.0) / deg).astype(np.int64) % (360 // deg)
    return rows, cols


def _valid(lat: Floats, lon: Floats) -> np.ndarray[Any, np.dtype[np.bool_]]:
    ok = np.isfinite(lat) & np.isfinite(lon) & (np.abs(lat) <= 90) & (np.abs(lon) <= 180)
    return ok & ((np.abs(lat) > 1e-6) | (np.abs(lon) > 1e-6))  # 0,0 is "no position"


def tiles_of(lat: Floats, lon: Floats, rules: TileRules) -> list[str]:
    """The tiles holding at least `min_share` of these positions, sorted."""
    ok = _valid(lat, lon)
    if not ok.any():
        return []
    rows, cols = _cells(lat[ok], lon[ok], rules.tile_deg)
    cells, counts = np.unique(np.stack([rows, cols], axis=1), axis=0, return_counts=True)
    need = rules.min_share * int(ok.sum())
    return sorted(
        name(-90 + int(r) * rules.tile_deg, -180 + int(c) * rules.tile_deg)
        for (r, c), n in zip(cells.tolist(), counts.tolist(), strict=True)
        if n >= need
    )


def neighbours(tile: str, rules: TileRules) -> list[str]:
    """The tile and the ring of `neighbours` tiles around it: longitude wraps round the
    date line; latitude stops at the poles."""
    lat, lon = corner(tile)
    deg, ring = rules.tile_deg, rules.neighbours
    out: set[str] = set()
    for dr in range(-ring, ring + 1):
        row = lat + dr * deg
        if not -90 <= row < 90:
            continue
        for dc in range(-ring, ring + 1):
            out.add(name(row, (lon + dc * deg + 180) % 360 - 180))
    return sorted(out)


def grid(tile: str, step: float, rules: TileRules) -> list[tuple[float, float]]:
    """A tile's evenly spaced points, south to north, then west to east: its south and
    west edges and everything inside (a 2 degree tile at 0.1 degrees: 20 by 20). The
    north and east edges are the next tiles' south and west, so no point is asked for
    twice, and the ring of tiles around a flown one covers its edges."""
    lat, lon = corner(tile)
    n = round(rules.tile_deg / step)
    return [
        (round(lat + i * step, 6), round(lon + j * step, 6)) for i in range(n) for j in range(n)
    ]


def _positions(cache: ColumnCache, key: str, rules: TileRules) -> tuple[Floats, Floats] | None:
    """The flight's positions: the estimator's, else GPS with a 3D fix. None when this
    laptop has none of the flight's tables yet."""
    names = set(cache.names(key))
    if not names:
        return None
    empty: tuple[Floats, Floats] = (np.empty(0), np.empty(0))
    if "POS" in names and {"Lat", "Lng"} <= set(cache.schema(key, "POS").names):
        pos = cache.read(key, "POS", ["Lat", "Lng"], positions=True)
        lat, lon = si.degrees(pos, "Lat"), si.degrees(pos, "Lng")
        if _valid(lat, lon).any():
            return lat, lon
    fields = ["Lat", "Lng", "Status"]
    if "GPS" in names and set(fields) <= set(cache.schema(key, "GPS").names):
        gps = cache.read(key, "GPS", fields, positions=True)
        fix = si.values(gps, "Status") >= rules.gps_min_status
        return si.degrees(gps, "Lat")[fix], si.degrees(gps, "Lng")[fix]
    return empty


def flight_tiles(cache: ColumnCache, key: str, rules: TileRules) -> list[str] | None:
    """The tiles one flight flew in; None when its tables are not on this laptop."""
    try:
        found = _positions(cache, key, rules)
    except CacheError:
        return None
    return None if found is None else tiles_of(*found, rules)


def scan(
    store: Store, cache: ColumnCache, rules: TileRules,
    progress: Callable[[float, str], None] | None = None,
) -> int:  # fmt: skip
    """Finds the tiles of every flight not yet scanned with these rules; returns how
    many flights it scanned."""
    todo: list[tuple[str, str]] = [
        (r[0], r[1])
        for r in store.conn.execute(
            "SELECT f.id, f.flight_key FROM flight f "
            "LEFT JOIN flight_tiles t ON t.flight_id = f.id "
            "WHERE f.flight_key IS NOT NULL AND (t.flight_id IS NULL OR t.basis != ?) "
            "ORDER BY f.id",
            (rules.basis,),
        ).fetchall()
    ]
    done = 0
    for i, (flight_id, key) in enumerate(todo):
        if progress is not None:
            progress(i / len(todo), "finding weather tiles")
        tiles = flight_tiles(cache, key, rules)
        if tiles is None:
            continue  # its log has not reached this laptop: scanned once it has
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO flight_tiles (flight_id, basis, tiles) VALUES (?, ?, ?) "
                "ON CONFLICT (flight_id) DO UPDATE SET basis = excluded.basis, "
                "tiles = excluded.tiles",
                (flight_id, rules.basis, json.dumps(tiles)),
            )
        done += 1
    return done


def download_list(store: Store, rules: TileRules) -> list[str]:
    """Every tile any flight flew in, with the tiles around it, sorted."""
    own: set[str] = set()
    rows: Iterable[Any] = store.conn.execute(
        "SELECT tiles FROM flight_tiles WHERE basis = ?", (rules.basis,)
    ).fetchall()
    for (tiles,) in rows:
        own.update(json.loads(tiles))
    return sorted({t for tile in own for t in neighbours(tile, rules)})
