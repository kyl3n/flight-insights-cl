"""The aircraft's track as east, north and up metres from its first fix, for analysis on
this laptop only (spec: Location and privacy; Links, "Geometry and interference";
GNSS jamming and spoofing; Flying without GNSS).

Analyzers that need where the aircraft was relative to its start (distance from the
station, drift without GNSS, a GNSS jump against the inertial track) get it from here,
never from coordinates. The track holds no latitude or longitude, and it refuses to be
pickled, copied or turned into JSON, and shows only its size, so it cannot slip into a
result. Findings and evidence carry what is computed from it: distances, heights and
drift, as scalars. Evidence refuses the track itself, and any array.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

import numpy as np

Array = np.ndarray[Any, np.dtype[np.float64]]
WGS84_A = 6_378_137.0  # semi-major axis, m
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)  # first eccentricity squared


def _ecef(lat_deg: Array, lon_deg: Array, alt_m: Array) -> tuple[Array, Array, Array]:
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    n = WGS84_A / np.sqrt(1 - WGS84_E2 * np.sin(lat) ** 2)
    x = (n + alt_m) * np.cos(lat) * np.cos(lon)
    y = (n + alt_m) * np.cos(lat) * np.sin(lon)
    z = (n * (1 - WGS84_E2) + alt_m) * np.sin(lat)
    return x, y, z


def enu(lat_deg: Array, lon_deg: Array, alt_m: Array) -> tuple[Array, Array, Array]:
    """East, north and up metres from the first point, through Earth-centred coordinates:
    exact for any distance, not a flat-Earth approximation."""
    return _enu_from(lat_deg, lon_deg, alt_m, lat_deg[:1], lon_deg[:1], alt_m[:1])


def _enu_from(
    lat_deg: Array, lon_deg: Array, alt_m: Array, lat0: Array, lon0: Array, alt0: Array
) -> tuple[Array, Array, Array]:
    x, y, z = _ecef(lat_deg, lon_deg, alt_m)
    x0, y0, z0 = _ecef(lat0, lon0, alt0)
    dx, dy, dz = x - x0[0], y - y0[0], z - z0[0]
    phi, lam = np.radians(lat0[0]), np.radians(lon0[0])
    east = -np.sin(lam) * dx + np.cos(lam) * dy
    north = -np.sin(phi) * np.cos(lam) * dx - np.sin(phi) * np.sin(lam) * dy + np.cos(phi) * dz
    up = np.cos(phi) * np.cos(lam) * dx + np.cos(phi) * np.sin(lam) * dy + np.sin(phi) * dz
    return east, north, up


class LocalTrack:
    """East, north and up metres from the first fix, with the time of each sample."""

    __slots__ = ("_e", "_n", "_t", "_u")

    def __init__(
        self, times_us: np.ndarray[Any, Any], east: Array, north: Array, up: Array
    ) -> None:
        self._t = np.asarray(times_us, np.int64)
        self._e = np.asarray(east, np.float64)
        self._n = np.asarray(north, np.float64)
        self._u = np.asarray(up, np.float64)

    def __len__(self) -> int:
        return len(self._t)

    def __repr__(self) -> str:
        return f"LocalTrack({len(self)} samples)"

    __str__ = __repr__

    def _refuse(self, *_: Any) -> NoReturn:
        raise TypeError("a local track stays inside the analysis: it is never copied out")

    __reduce__ = __reduce_ex__ = __getstate__ = __copy__ = __deepcopy__ = _refuse

    @property
    def times_us(self) -> np.ndarray[Any, np.dtype[np.int64]]:
        return self._t.copy()

    def distance_m(self) -> Array:
        """Horizontal distance from the first fix."""
        return np.hypot(self._e, self._n)

    def height_m(self) -> Array:
        """Height above the first fix."""
        return self._u.copy()

    def east_north_m(self) -> tuple[Array, Array]:
        """East and north from the first fix, for bearings and drift."""
        return self._e.copy(), self._n.copy()


def _valid(lat_deg: Array, lon_deg: Array, alt_m: Array) -> np.ndarray[Any, np.dtype[np.bool_]]:
    return (
        np.isfinite(lat_deg) & np.isfinite(lon_deg) & np.isfinite(alt_m)
        & (np.abs(lat_deg) <= 90) & (np.abs(lon_deg) <= 180)
        & ((np.abs(lat_deg) > 1e-6) | (np.abs(lon_deg) > 1e-6))
    )  # fmt: skip


Series = tuple[np.ndarray[Any, Any], Array, Array, Array]  # times, latitude, longitude, altitude


def from_coordinates(
    times_us: np.ndarray[Any, Any], lat_deg: Array, lon_deg: Array, alt_m: Array
) -> LocalTrack | None:
    """The track from the first valid fix on (0, 0 is "no position")."""
    return tracks({"track": (times_us, lat_deg, lon_deg, alt_m)}).get("track")


def tracks(series: Mapping[str, Series]) -> dict[str, LocalTrack]:
    """Several sources' tracks from one origin, so they can be compared: the first valid
    fix of the first source that has two (in the order given). A source with fewer than
    two valid fixes is left out. The origin stays here: only metres leave."""
    valid: dict[str, Series] = {}
    for name, (t, lat, lon, alt) in series.items():
        ok = _valid(lat, lon, alt)
        if np.count_nonzero(ok) < 2:
            continue
        order = np.argsort(np.asarray(t)[ok], kind="stable")
        valid[name] = (np.asarray(t)[ok][order], lat[ok][order], lon[ok][order], alt[ok][order])
    if not valid:
        return {}
    _, lat0, lon0, alt0 = next(iter(valid.values()))
    return {
        name: LocalTrack(t, *_enu_from(lat, lon, alt, lat0[:1], lon0[:1], alt0[:1]))
        for name, (t, lat, lon, alt) in valid.items()
    }
