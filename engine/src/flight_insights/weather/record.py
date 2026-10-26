"""Each flight's weather record (spec: Weather and wind, "The weather record per flight";
rules in `knowledge/rules/weather.json`, "record"), worked out on the laptop from the
regional downloads the shell keeps: the flight's own positions never leave it.

The flight is sampled along its track. At each sample the model fields are read from the
grid points around it (bilinear: the points a tile's request asked for, in the order it
asked, so each reply point is matched to its grid node), between the hours around it
(linear), and between the model's heights at the flight's height above take-off (log
law). Wind is carried as its east and north components, so averaging never wraps round
north. ERA5 reanalysis is used wherever it covers the sample, else the forecast.

At take-off, temperature and pressure are carried from the model's ground to the
take-off elevation (standard lapse rate; hypsometric equation), and density altitude is
worked out from them and the humidity. The record holds values only, no coordinate and
no tile.
"""

from __future__ import annotations

import gzip
import itertools
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import numpy as np

from flight_insights.dataflash import si
from flight_insights.knowledge import Pack
from flight_insights.store.cache import CacheError, ColumnCache
from flight_insights.store.db import Store
from flight_insights.weather import tiles as tiling

Floats = np.ndarray[Any, np.dtype[np.float64]]
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
SLOT_MS = 6 * HOUR_MS
G = 9.80665
R_DRY = 287.05
LEVELS = (10, 100, 200)  # model wind heights, m


@dataclass(frozen=True)
class Point:
    """One grid point of one reply: the model cell's elevation, and its hourly values."""

    elevation: float
    times_ms: np.ndarray[Any, np.dtype[np.int64]]
    values: Mapping[str, Floats]


class ReplyError(ValueError):
    pass


def _hours(times: Iterable[str]) -> np.ndarray[Any, np.dtype[np.int64]]:
    stamps = np.array(list(times), dtype="datetime64[m]")
    return stamps.astype("datetime64[ms]").astype(np.int64)


def parse_open_meteo(body: bytes) -> list[Point]:
    """An Open-Meteo reply: a list of locations (one location alone is an object)."""
    doc: Any = json.loads(body)
    locations = cast(list[Any], doc if isinstance(doc, list) else [doc])
    out: list[Point] = []
    for loc in locations:
        if not isinstance(loc, dict):
            raise ReplyError("a location is not an object")
        hourly = cast(dict[str, Any], cast(dict[str, Any], loc).get("hourly") or {})
        if "time" not in hourly:
            raise ReplyError("a location has no hourly times")
        times = _hours(cast(list[str], hourly["time"]))
        values = {
            name: np.array([np.nan if v is None else float(v) for v in vs], dtype=np.float64)
            for name, vs in hourly.items()
            if name != "time"
        }
        if any(len(v) != len(times) for v in values.values()):
            raise ReplyError("a variable's length differs from the times'")
        elevation = cast(dict[str, Any], loc).get("elevation")
        out.append(Point(float("nan") if elevation is None else float(elevation), times, values))
    return out


def parse_kp(body: bytes) -> list[tuple[int, float]]:
    """NOAA's planetary K-index: (start of each 3-hour interval, Kp). Either a header row
    and rows of values, or a list of objects."""
    doc: Any = json.loads(body)
    rows = cast(list[Any], doc)
    out: list[tuple[int, float]] = []
    if rows and isinstance(rows[0], list):
        header = [str(h) for h in cast(list[Any], rows[0])]
        at, kp = header.index("time_tag"), header.index("Kp")
        pairs = [(cast(list[Any], r)[at], cast(list[Any], r)[kp]) for r in rows[1:]]
    else:
        pairs = [(cast(dict[str, Any], r)["time_tag"], cast(dict[str, Any], r)["Kp"]) for r in rows]
    for tag, value in pairs:
        stamp = np.datetime64(str(tag).replace(" ", "T")[:19], "ms").astype(np.int64)
        out.append((int(stamp), float(value)))
    return out


@dataclass
class Rules:
    sample_s: float
    z0_m: float
    lapse_k_per_m: float
    dew_lapse_k_per_m: float
    cloud_base_m_per_k: float
    tile_deg: int
    grids_deg: Mapping[str, float]

    @staticmethod
    def load(pack: Pack) -> Rules:
        doc: Mapping[str, Any] = pack.json("rules/weather.json")
        r = doc["record"]
        return Rules(float(r["sample_s"]), float(r["z0_m"]), float(r["lapse_k_per_m"]),
                     float(r["dew_lapse_k_per_m"]), float(r["cloud_base_m_per_k"]),
                     int(doc["tile_deg"]), dict(doc["grids_deg"]))  # fmt: skip


def _hour_label(ms: int) -> str:
    return str(np.datetime64(ms, "ms").astype("datetime64[h]"))


def _day_label(ms: int) -> str:
    return str(np.datetime64(ms, "ms").astype("datetime64[D]"))


def _month_label(ms: int) -> str:
    return str(np.datetime64(ms, "ms").astype("datetime64[M]"))


def candidate_periods(source: str, t0: int, t1: int) -> list[str]:
    """Every period of a source that may hold an hour within an hour of [t0, t1]."""
    lo, hi = t0 - HOUR_MS, t1 + HOUR_MS
    if source == "forecast":
        start = (lo // SLOT_MS) * SLOT_MS
        return [_hour_label(w) for w in range(start, hi + 1, SLOT_MS)]
    out: list[str] = []
    for day in range((lo // DAY_MS) * DAY_MS, hi + 1, DAY_MS):
        for label in (_month_label(day), _day_label(day)):
            if label not in out:
                out.append(label)
    return out


@dataclass
class Field:
    """One source's grid near a flight: each node's hourly series, from every reply that
    holds it."""

    store: Store
    source: str
    step_h: int  # grid step in hundredths of a degree
    tile_deg: int
    periods: list[str]
    used: dict[tuple[str, str], int] = field(default_factory=dict[tuple[str, str], int])
    _replies: dict[tuple[str, str], list[Point] | None] = field(
        default_factory=dict[tuple[str, str], list[Point] | None]
    )
    _nodes: dict[tuple[int, int], Point | None] = field(
        default_factory=dict[tuple[int, int], Point | None]
    )

    def _reply(self, tile: str, period: str) -> list[Point] | None:
        key = (tile, period)
        if key not in self._replies:
            row = self.store.conn.execute(
                "SELECT body, fetched_ms FROM weather_download "
                "WHERE source = ? AND tile = ? AND period = ?",
                (self.source, tile, period),
            ).fetchone()
            points: list[Point] | None = None
            if row is not None:
                try:
                    points = parse_open_meteo(gzip.decompress(row[0]))
                except (ReplyError, ValueError, KeyError):
                    points = None
                n = round(self.tile_deg * 100 / self.step_h)
                if points is not None and len(points) != n * n:
                    points = None  # not the grid this tile asks for
                if points is not None:
                    self.used[key] = int(row[1])
            self._replies[key] = points
        return self._replies[key]

    def node(self, lat_h: int, lon_h: int) -> Point | None:
        """A grid node's series over the flight's periods, or None where nothing holds it."""
        key = (lat_h, lon_h)
        if key in self._nodes:
            return self._nodes[key]
        lon_w = (lon_h + 18000) % 36000 - 18000
        size = self.tile_deg * 100
        sw_lat, sw_lon = (lat_h // size) * size, (lon_w // size) * size
        tile = tiling.name(sw_lat // 100, sw_lon // 100)
        n = size // self.step_h
        index = ((lat_h - sw_lat) // self.step_h) * n + (lon_w - sw_lon) // self.step_h
        parts = [p[index] for period in self.periods if (p := self._reply(tile, period))]
        if not parts:
            self._nodes[key] = None
            return None
        times = np.concatenate([p.times_ms for p in parts])
        order = np.argsort(times, kind="stable")
        times, keep = np.unique(times[order], return_index=True)
        names: set[str] = set(parts[0].values)
        for p in parts[1:]:
            names &= set(p.values)
        values = {k: np.concatenate([p.values[k] for p in parts])[order][keep] for k in names}
        found = Point(parts[-1].elevation, times, values)
        self._nodes[key] = found
        return found

    def at(self, lat: float, lon: float, t_ms: int) -> dict[str, float] | None:
        """Every variable at a place and time: bilinear in space, linear in time; None
        where a node or the hours around `t_ms` are missing."""
        lat_h, lon_h = lat * 100.0, lon * 100.0
        i0 = math.floor(lat_h / self.step_h) * self.step_h
        j0 = math.floor(lon_h / self.step_h) * self.step_h
        fy, fx = (lat_h - i0) / self.step_h, (lon_h - j0) / self.step_h
        corners = [
            (i0, j0, (1 - fy) * (1 - fx)), (i0 + self.step_h, j0, fy * (1 - fx)),
            (i0, j0 + self.step_h, (1 - fy) * fx), (i0 + self.step_h, j0 + self.step_h, fy * fx),
        ]  # fmt: skip
        total: dict[str, float] = {}
        names: set[str] | None = None
        for i, j, w in corners:
            p = self.node(int(i), int(j))
            if p is None or len(p.times_ms) < 2 or not p.times_ms[0] <= t_ms <= p.times_ms[-1]:
                return None
            k = int(np.searchsorted(p.times_ms, t_ms, side="right"))
            k = min(max(k, 1), len(p.times_ms) - 1)
            t_a, t_b = int(p.times_ms[k - 1]), int(p.times_ms[k])
            if t_b - t_a > HOUR_MS:
                return None  # a gap in the hours
            f = (t_ms - t_a) / (t_b - t_a)
            here = {"elevation": p.elevation}
            for name, v in _components(p.values, k - 1, k).items():
                here[name] = v[0] * (1 - f) + v[1] * f
            names = set(here) if names is None else names & set(here)
            for name, v in here.items():
                total[name] = total.get(name, 0.0) + w * v
        return {k: v for k, v in total.items() if names is not None and k in names}


def _components(values: Mapping[str, Floats], a: int, b: int) -> dict[str, tuple[float, float]]:
    """The values at two hours, wind as east and north components (where it blows to)."""
    out: dict[str, tuple[float, float]] = {}
    for name, v in values.items():
        if name.startswith(("wind_speed_", "wind_direction_")):
            continue
        out[name] = (float(v[a]), float(v[b]))
    for z in LEVELS:
        s, d = values.get(f"wind_speed_{z}m"), values.get(f"wind_direction_{z}m")
        if s is None or d is None:
            continue
        for key, comp in (("u", np.sin), ("v", np.cos)):
            pair = [-float(s[i]) * float(comp(np.radians(d[i]))) for i in (a, b)]
            out[f"{key}_{z}m"] = (pair[0], pair[1])
    return out


def wind_at(
    sample: Mapping[str, float], height_m: float, z0_m: float
) -> tuple[float, float] | None:
    """The wind's east and north components at a height above ground: log law between
    the model's levels, below the lowest from a surface roughness, the top level above."""
    levels = [z for z in LEVELS if f"u_{z}m" in sample and np.isfinite(sample[f"u_{z}m"])]
    if not levels:
        return None
    uv = {z: (sample[f"u_{z}m"], sample[f"v_{z}m"]) for z in levels}
    h = max(height_m, z0_m * 3)
    if h <= levels[0]:
        scale = math.log(h / z0_m) / math.log(levels[0] / z0_m)
        return uv[levels[0]][0] * scale, uv[levels[0]][1] * scale
    for lo, hi in itertools.pairwise(levels):
        if h <= hi:
            w = math.log(h / lo) / math.log(hi / lo)
            return (uv[lo][0] * (1 - w) + uv[hi][0] * w, uv[lo][1] * (1 - w) + uv[hi][1] * w)
    return uv[levels[-1]]


def density_altitude_m(pressure_hpa: float, temperature_c: float, dew_point_c: float) -> float:
    """Density altitude from pressure, temperature and humidity (virtual temperature;
    the US National Weather Service's fit to the standard atmosphere)."""
    e = 6.1078 * 10 ** (7.5 * dew_point_c / (237.3 + dew_point_c))  # vapour pressure, hPa
    tv = (temperature_c + 273.15) / (1 - 0.379 * e / pressure_hpa)
    rho = pressure_hpa * 100 / (R_DRY * tv)
    return 1000.0 * (44.3308 - 42.2665 * rho**0.234969)


@dataclass(frozen=True)
class Track:
    """The flight's own positions on the laptop: never stored, never sent."""

    t_us: Floats  # since boot
    lat: Floats
    lon: Floats
    height_m: Floats  # above take-off
    takeoff_amsl_m: float


def _track(cache: ColumnCache, key: str, arm_us: int, disarm_us: int, step_us: int) -> Track | None:
    """The flight's positions every `step_us`; None when the log holds no estimator
    position."""
    try:
        pos = cache.read(key, "POS", ["TimeUS", "Lat", "Lng", "Alt"], positions=True)
    except CacheError:
        return None
    t = np.asarray(pos.column("TimeUS").to_numpy(zero_copy_only=False), np.int64)
    lat, lon, alt = si.degrees(pos, "Lat"), si.degrees(pos, "Lng"), si.values(pos, "Alt")
    ok = (t >= arm_us) & (t <= disarm_us) & np.isfinite(lat) & np.isfinite(lon)
    ok &= (np.abs(lat) > 1e-6) | (np.abs(lon) > 1e-6)
    if int(ok.sum()) < 2:
        return None
    t, lat, lon, alt = t[ok], lat[ok], lon[ok], alt[ok]
    grid = np.arange(int(t[0]), int(t[-1]) + 1, step_us, dtype=np.int64)
    pick = np.searchsorted(t, grid).clip(0, len(t) - 1)
    return Track(grid.astype(np.float64), lat[pick], lon[pick], alt[pick] - alt[0], float(alt[0]))


def flight_record(
    store: Store, cache: ColumnCache, pack: Pack, flight_id: str
) -> tuple[dict[str, Any], list[tuple[str, str, str, int]]] | None:
    """A flight's weather record and the downloads it was made from, or None when the
    flight has no UTC time or no position."""
    rules = Rules.load(pack)
    row = store.get("flight", flight_id)
    if row is None or row["boot_utc_us"] is None or row["arm_us"] is None:
        return None
    arm, disarm = int(row["arm_us"]), int(row["disarm_us"] or row["arm_us"])
    track = _track(cache, str(row["flight_key"]), arm, disarm, int(rules.sample_s * 1e6))
    if track is None:
        return None
    lat, lon, height = track.lat, track.lon, track.height_m
    utc_ms = ((int(row["boot_utc_us"]) + track.t_us) / 1000.0).astype(np.int64)
    t0, t1 = int(utc_ms[0]), int(utc_ms[-1])
    fields = {
        source: Field(store, source, round(rules.grids_deg[grid] * 100), rules.tile_deg,
                      candidate_periods(source, t0, t1))
        for source, grid in (("reanalysis", "reanalysis"), ("forecast", "forecast"))
    }  # fmt: skip
    samples: list[tuple[str, dict[str, float], float]] = []
    for k in range(len(utc_ms)):
        for source in ("reanalysis", "forecast"):
            got = fields[source].at(float(lat[k]), float(lon[k]), int(utc_ms[k]))
            if got is not None:
                samples.append((source, got, float(height[k])))
                break
    used = [(s, tile, period, ms) for s, f in fields.items()
            for (tile, period), ms in sorted(f.used.items())]  # fmt: skip
    record = _summarise(samples, len(utc_ms), track.takeoff_amsl_m, rules)
    record["kp_max"] = _kp_max(store, t0, t1)
    return record, used


def _summarise(
    samples: list[tuple[str, dict[str, float], float]], n: int, takeoff_amsl_m: float,
    rules: Rules,
) -> dict[str, Any]:  # fmt: skip
    if not samples:
        return {"quality": "none", "coverage": 0.0}
    sources = {s for s, _, _ in samples}
    quality = sources.pop() if len(sources) == 1 else "mixed"
    winds = [wind_at(v, h, rules.z0_m) for _, v, h in samples]
    surface = [wind_at(v, 10.0, rules.z0_m) for _, v, _ in samples]
    speeds = [math.hypot(*w) for w in winds if w is not None]
    mean_u = float(np.mean([w[0] for w in winds if w is not None])) if speeds else float("nan")
    mean_v = float(np.mean([w[1] for w in winds if w is not None])) if speeds else float("nan")
    first, heights = samples[0][1], [h for _, _, h in samples]

    def mean(name: str) -> float | None:
        vals = [v[name] for _, v, _ in samples if name in v and math.isfinite(v[name])]
        return float(np.mean(vals)) if vals else None

    def largest(name: str) -> float | None:
        vals = [v[name] for _, v, _ in samples if name in v and math.isfinite(v[name])]
        return float(np.max(vals)) if vals else None

    out: dict[str, Any] = {
        "quality": quality,
        "coverage": round(len(samples) / n, 3),
        "height_band_m": [round(min(heights), 1), round(max(heights), 1)],
        "wind_ms": round(float(np.mean(speeds)), 2) if speeds else None,
        "wind_max_ms": round(float(np.max(speeds)), 2) if speeds else None,
        # where the mean wind blows from, degrees true
        "wind_from_deg": round(math.degrees(math.atan2(-mean_u, -mean_v)) % 360, 1)
        if speeds else None,
        "surface_wind_ms": round(float(np.mean([math.hypot(*w) for w in surface if w])), 2)
        if any(surface) else None,
        "gust_max_ms": largest("wind_gusts_10m"),
        "precipitation_mm_h": mean("precipitation"),
        "cloud_cover_pct": mean("cloud_cover"),
        "boundary_layer_m": mean("boundary_layer_height"),
    }  # fmt: skip
    t2, td = first.get("temperature_2m"), first.get("dew_point_2m")
    ps, rh = first.get("surface_pressure"), first.get("relative_humidity_2m")
    if t2 is not None and ps is not None and td is not None:
        # Open-Meteo gives each point's values at that point's ground: carried from the
        # model ground, interpolated, to the take-off's elevation
        rise = takeoff_amsl_m - first.get("elevation", takeoff_amsl_m)
        if not math.isfinite(rise):
            rise = 0.0
        temp = t2 - rules.lapse_k_per_m * rise
        td = td - rules.dew_lapse_k_per_m * rise
        ps = ps * math.exp(-G * rise / (R_DRY * ((t2 + temp) / 2 + 273.15)))
        out["temperature_c"] = round(temp, 2)
        out["pressure_hpa"] = round(ps, 1)
        out["humidity_pct"] = None if rh is None else round(rh, 1)
        out["dew_point_c"] = round(td, 2)
        out["density_altitude_m"] = round(density_altitude_m(ps, temp, td), 0)
        out["cloud_base_m"] = round(max(0.0, (temp - td) * rules.cloud_base_m_per_k), 0)
        # at the top of the flight: the standard lapse rate, and the hypsometric equation
        top = max(heights)
        t_top = temp - rules.lapse_k_per_m * top
        p_top = ps * math.exp(-G * top / (R_DRY * ((temp + t_top) / 2 + 273.15)))
        td_top = td - rules.dew_lapse_k_per_m * top
        out["density_altitude_top_m"] = round(density_altitude_m(p_top, t_top, td_top), 0)
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in out.items()}


def _kp_max(store: Store, t0: int, t1: int) -> float | None:
    """The highest planetary K-index of the 3-hour intervals the flight overlaps."""
    since = _hour_label(t0 - 8 * DAY_MS)
    rows: Iterable[Any] = store.conn.execute(
        "SELECT body FROM weather_download WHERE source = 'kp' AND period >= ?", (since,)
    ).fetchall()
    best: float | None = None
    for (body,) in rows:
        try:
            values = parse_kp(gzip.decompress(body))
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        for start, kp in values:
            if start <= t1 and start + 3 * HOUR_MS > t0 and math.isfinite(kp):
                best = kp if best is None else max(best, kp)
    return best


def _window(store: Store, flight_id: str) -> tuple[int, int] | None:
    row = store.get("flight", flight_id)
    if row is None or row["boot_utc_us"] is None or row["arm_us"] is None:
        return None
    boot = int(row["boot_utc_us"])
    disarm = int(row["disarm_us"] if row["disarm_us"] is not None else row["arm_us"])
    return (boot + int(row["arm_us"])) // 1000, (boot + disarm) // 1000


def basis(store: Store, pack: Pack, flight_id: str) -> str | None:
    """What a flight's record depends on: the rules, and every download that could cover
    its tiles and hours, with when each was fetched. None before the flight's tiles are
    known."""
    window = _window(store, flight_id)
    found = store.conn.execute(
        "SELECT tiles FROM flight_tiles WHERE flight_id = ?", (flight_id,)
    ).fetchone()
    if window is None or found is None:
        return None
    rules = tiling.TileRules.load(pack)
    near = sorted({t for own in json.loads(found[0]) for t in tiling.neighbours(own, rules)})
    t0, t1 = window
    rows: list[Any] = []
    for source in ("reanalysis", "forecast"):
        periods = candidate_periods(source, t0, t1)
        marks = ",".join("?" * len(periods))
        tile_marks = ",".join("?" * len(near))
        if not near:
            continue
        rows += store.conn.execute(
            f"SELECT source, tile, period, fetched_ms FROM weather_download "
            f"WHERE source = ? AND tile IN ({tile_marks}) AND period IN ({marks}) "
            f"ORDER BY tile, period",
            (source, *near, *periods),
        ).fetchall()
    rows += store.conn.execute(
        "SELECT source, tile, period, fetched_ms FROM weather_download "
        "WHERE source = 'kp' AND period >= ? ORDER BY period",
        (_hour_label(t0 - 8 * DAY_MS),),
    ).fetchall()
    doc = pack.json("rules/weather.json")
    return json.dumps(
        [doc["record"], doc["grids_deg"], doc["tile_deg"], [list(r) for r in rows]],
        sort_keys=True,
    )


def current(store: Store, cache: ColumnCache, pack: Pack, flight_id: str) -> dict[str, Any] | None:
    """A flight's weather record, made again only when what it depends on changed; None
    when the flight has no UTC time or no position."""
    b = basis(store, pack, flight_id)
    kept = store.conn.execute(
        "SELECT basis, record_json FROM flight_weather WHERE flight_id = ?", (flight_id,)
    ).fetchone()
    if kept is not None and b is not None and kept[0] == b:
        return cast(dict[str, Any], json.loads(kept[1]))
    made = flight_record(store, cache, pack, flight_id)
    if made is None:
        return None
    record, _used = made
    if b is not None:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO flight_weather (flight_id, basis, record_json) VALUES (?, ?, ?) "
                "ON CONFLICT (flight_id) DO UPDATE SET basis = excluded.basis, "
                "record_json = excluded.record_json",
                (flight_id, b, json.dumps(record, sort_keys=True)),
            )
    return record
