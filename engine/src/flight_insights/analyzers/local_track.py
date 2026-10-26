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
    x, y, z = _ecef(lat_deg, lon_deg, alt_m)
    lat0, lon0 = np.radians(lat_deg[0]), np.radians(lon_deg[0])
    dx, dy, dz = x - x[0], y - y[0], z - z[0]
    east = -np.sin(lon0) * dx + np.cos(lon0) * dy
    north = -np.sin(lat0) * np.cos(lon0) * dx - np.sin(lat0) * np.sin(lon0) * dy + np.cos(lat0) * dz
    up = np.cos(lat0) * np.cos(lon0) * dx + np.cos(lat0) * np.sin(lon0) * dy + np.sin(lat0) * dz
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


def from_coordinates(
    times_us: np.ndarray[Any, Any], lat_deg: Array, lon_deg: Array, alt_m: Array
) -> LocalTrack | None:
    """The track from the first valid fix on (0, 0 is "no position")."""
    ok = (
        np.isfinite(lat_deg) & np.isfinite(lon_deg) & np.isfinite(alt_m)
        & (np.abs(lat_deg) <= 90) & (np.abs(lon_deg) <= 180)
        & ((np.abs(lat_deg) > 1e-6) | (np.abs(lon_deg) > 1e-6))
    )  # fmt: skip
    if np.count_nonzero(ok) < 2:
        return None
    order = np.argsort(np.asarray(times_us)[ok], kind="stable")
    t = np.asarray(times_us)[ok][order]
    east, north, up = enu(lat_deg[ok][order], lon_deg[ok][order], alt_m[ok][order])
    return LocalTrack(t, east, north, up)
