"""This flight's own positions, for the scrubber's tripwire (spec: AI layer, "Location
work stays on the laptop"; the second check in the shell, "Checked twice").

Read from the encrypted cache with the positions key: home, origin, the track and every
other latitude and longitude the log holds. The result never goes into a packet. It is
handed to the scrubber and, over stdio, to the shell's own check, held in memory and
never written or logged. Positions are thinned to one per ~11 m cell, up to 5000 cells
(about 55 km of track); a longer track is sampled evenly, which the scrubber's 0.01°
pair check still covers.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any, cast

import numpy as np
import pyarrow as pa

from flight_insights.dataflash import si
from flight_insights.store.cache import ColumnCache, is_position

Point = tuple[float, float]
CELL_DEG = 1e-4  # about 11 m
MAX_POINTS = 5000


def _kind(field: pa.Field[Any]) -> str | None:
    """'lat', 'lon' or None, from the field's unit label, else its name."""
    meta = cast(Mapping[bytes, bytes] | None, field.metadata) or {}
    label = (
        (meta.get(b"unit", b"") or b"").decode(errors="replace").lower() + " " + field.name.lower()
    )
    if "lat" in label:
        return "lat"
    if "lon" in label or "lng" in label:
        return "lon"
    return None


def _degrees(table: pa.Table, name: str) -> np.ndarray[Any, np.dtype[np.float64]]:
    values = si.values(table, name)
    unit = si.unit(table, name).lower()
    if "e7" in unit and np.nanmax(np.abs(values), initial=0.0) > 180.0:
        values = values * 1e-7  # degE7 not yet scaled by its multiplier
    return values


def flight_points(cache: ColumnCache, flight_key: str, cap: int = MAX_POINTS) -> list[Point]:
    """Every latitude-longitude pair the flight's cached messages hold, thinned."""
    cells: dict[tuple[int, int], Point] = {}
    for name in cache.names(flight_key):
        schema = cast("Iterable[pa.Field[Any]]", cache.schema(flight_key, name))
        fields = [f for f in schema if is_position(f)]
        lats = [f.name for f in fields if _kind(f) == "lat"]
        lons = [f.name for f in fields if _kind(f) == "lon"]
        if not lats or not lons:
            continue
        table = cache.read(flight_key, name, lats + lons, positions=True)
        for lat_name, lon_name in zip(lats, lons, strict=False):
            lat, lon = _degrees(table, lat_name), _degrees(table, lon_name)
            ok = np.isfinite(lat) & np.isfinite(lon) & (np.abs(lat) <= 90) & (np.abs(lon) <= 180)
            ok &= (np.abs(lat) > 1e-6) | (np.abs(lon) > 1e-6)  # 0,0 is "no position"
            for a, b in zip(lat[ok].tolist(), lon[ok].tolist(), strict=True):
                cells.setdefault((math.floor(a / CELL_DEG), math.floor(b / CELL_DEG)), (a, b))
    points = list(cells.values())
    if len(points) > cap:
        step = len(points) / cap
        points = [points[int(i * step)] for i in range(cap)]
    return points
