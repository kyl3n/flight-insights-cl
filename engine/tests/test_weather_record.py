"""Each flight's weather record, worked out on the laptop: replies parsed, fields read
between grid points and hours exactly as a linear field asks, wind carried as components
and up the log law, take-off conditions and density altitude, reanalysis preferred over
the forecast, made again only when its downloads change, and no position in it."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from test_imports import airframe

from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store, open_store
from flight_insights.store.keys import StoreKeys
from flight_insights.weather import downloads, record, tiles
from flight_insights.weather.record import HOUR_MS, Field

PACK = load_pack()
TILES = tiles.TileRules.load(PACK)
FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0/copter-quad-fs-radio.bin"
T0 = int(np.datetime64("2026-10-26T00:00", "ms").astype(np.int64))
Fn = Callable[[float, float, int], float]


def iso(ms: int) -> str:
    return str(np.datetime64(ms, "ms").astype("datetime64[m]"))


def wind(u: Fn, v: Fn) -> dict[str, Fn]:
    """Speed and direction (blowing from) of a wind whose components are u and v."""
    return {
        "wind_speed_10m": lambda la, lo, t: math.hypot(u(la, lo, t), v(la, lo, t)),
        "wind_direction_10m": lambda la, lo, t: math.degrees(
            math.atan2(-u(la, lo, t), -v(la, lo, t))
        ) % 360,
    }  # fmt: skip


def const(x: float) -> Fn:
    def f(_la: float, _lo: float, _t: int) -> float:
        return x

    return f


def reply(tile: str, step: float, hours: list[int], fields: dict[str, Fn],
          elevation: float = 100.0) -> bytes:  # fmt: skip
    out: list[dict[str, object]] = []
    for lat, lon in tiles.grid(tile, step, TILES):
        hourly: dict[str, list[object]] = {"time": [iso(t) for t in hours]}
        for name, f in fields.items():
            hourly[name] = [f(lat, lon, t) for t in hours]
        out.append({"latitude": lat, "longitude": lon, "elevation": elevation, "hourly": hourly})
    return json.dumps(out).encode()


def temperature(la: float, lo: float, t: int) -> float:
    return 10 + 0.5 * la - 0.2 * lo + 0.1 * (t - T0) / HOUR_MS


def u(la: float, lo: float, t: int) -> float:
    return 3.0 + 0.4 * la - 0.3 * lo + 0.05 * (t - T0) / HOUR_MS


def v(la: float, lo: float, t: int) -> float:
    return -2.0 + 0.1 * la + 0.2 * lo


FIELDS: dict[str, Fn] = {"temperature_2m": temperature, **wind(u, v)}


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return open_store(tmp_path / "w.db", StoreKeys.generate(), "a" * 32)


def keep_forecast(store: Store, tile: str, window: int, fields: dict[str, Fn]) -> None:
    hours = [window + h * HOUR_MS for h in range(6)]
    downloads.keep(store, "forecast", tile, record._hour_label(window),  # pyright: ignore[reportPrivateUsage]
                   reply(tile, 0.1, hours, fields), 1)  # fmt: skip


def test_replies_are_read_in_both_shapes() -> None:
    one = json.dumps({"latitude": 50.0, "longitude": 30.0, "elevation": 120.5,
                      "hourly": {"time": ["2026-10-26T00:00", "2026-10-26T01:00"],
                                 "temperature_2m": [1.5, None]}}).encode()  # fmt: skip
    [p] = record.parse_open_meteo(one)
    assert p.elevation == 120.5 and list(p.times_ms) == [T0, T0 + HOUR_MS]
    assert p.values["temperature_2m"][0] == 1.5 and math.isnan(p.values["temperature_2m"][1])
    rows = [["time_tag", "Kp", "a_running", "station_count"],
            ["2026-10-26 00:00:00.000", "2.33", "9", "8"]]  # fmt: skip
    objects = [{"time_tag": "2026-10-26T03:00:00", "Kp": 4.0}]
    assert record.parse_kp(json.dumps(rows).encode()) == [(T0, 2.33)]
    assert record.parse_kp(json.dumps(objects).encode()) == [(T0 + 3 * HOUR_MS, 4.0)]


def test_a_linear_field_is_read_back_exactly(store: Store) -> None:
    for tile in ("N50E030", "N52E030"):
        for window in (T0, T0 + 6 * HOUR_MS):
            keep_forecast(store, tile, window, FIELDS)
    field = Field(store, "forecast", 10, 2, ["2026-10-26T00", "2026-10-26T06"])
    for lat, lon, t in [
        (50.537, 30.264, T0 + 2 * HOUR_MS + 1234),  # inside one tile
        (51.96, 30.051, T0 + 5 * HOUR_MS + 1800_000),  # across a tile edge and two windows
    ]:
        got = field.at(lat, lon, t)
        assert got is not None
        assert got["temperature_2m"] == pytest.approx(temperature(lat, lon, t), abs=1e-9)
        assert got["u_10m"] == pytest.approx(u(lat, lon, t), abs=1e-9)
        assert got["v_10m"] == pytest.approx(v(lat, lon, t), abs=1e-9)
    # past the hours held, or where a neighbouring tile is missing: nothing
    assert field.at(50.5, 30.2, T0 + 12 * HOUR_MS) is None
    assert field.at(51.95, 31.95, T0 + HOUR_MS) is None  # needs N52E032


def test_wind_up_the_log_law() -> None:
    sample = {"u_10m": 3.0, "v_10m": 4.0, "u_100m": 6.0, "v_100m": 8.0}
    assert record.wind_at(sample, 10, 0.03) == pytest.approx((3.0, 4.0))
    mid = record.wind_at(sample, math.sqrt(10 * 100), 0.03)  # halfway in log height
    assert mid == pytest.approx((4.5, 6.0))
    assert record.wind_at(sample, 500, 0.03) == pytest.approx((6.0, 8.0))
    low = record.wind_at(sample, 2, 0.03)
    assert low is not None and math.hypot(*low) == pytest.approx(
        5 * math.log(2 / 0.03) / math.log(10 / 0.03)
    )
    assert record.wind_at({}, 10, 0.03) is None


def test_density_altitude_matches_the_standard_atmosphere() -> None:
    dry = -60.0
    assert record.density_altitude_m(1013.25, 15.0, dry) == pytest.approx(0, abs=30)
    assert record.density_altitude_m(898.76, 8.5, dry) == pytest.approx(1000, abs=30)
    # 20 K above standard at sea level: about 120 ft (37 m) a kelvin; humid air is
    # lighter still
    hot = record.density_altitude_m(1013.25, 35.0, dry)
    assert hot == pytest.approx(20 * 36.6, rel=0.1)
    assert record.density_altitude_m(1013.25, 35.0, 25.0) > hot + 50


@pytest.fixture
def flown(tmp_path: Path) -> tuple[Store, ColumnCache, str]:
    keys = StoreKeys.generate()
    store = open_store(tmp_path / "f.db", keys, "f" * 32)
    cache = ColumnCache(tmp_path / "cache", keys)
    batch = scan(store, PACK, bytes(32), [FIXTURE])
    af = airframe(store, PACK)
    [res] = commit(store, PACK, cache, batch, propose(store, PACK, batch),
                   [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
    tiles.scan(store, cache, TILES)
    return store, cache, res.flights[0]


def test_a_flights_record_from_its_downloads(flown: tuple[Store, ColumnCache, str]) -> None:
    store, cache, flight = flown
    window = record._window(store, flight)  # pyright: ignore[reportPrivateUsage]
    assert window is not None
    t0, t1 = window
    row = store.conn.execute("SELECT tiles FROM flight_tiles WHERE flight_id = ?", (flight,))
    [own] = json.loads(row.fetchone()[0])
    near = tiles.neighbours(own, TILES)
    # the take-off elevation, read on the laptop, so the model's ground is the take-off's
    pos = cache.read(str(store.get("flight", flight)["flight_key"]), "POS", ["TimeUS", "Alt"])  # type: ignore[index]
    takeoff = float(pos.column("Alt").to_numpy()[0])
    calm = {**FIELDS, "surface_pressure": const(1000.0), "dew_point_2m": const(2.0)}
    assert record.current(store, cache, PACK, flight) == {"quality": "none", "coverage": 0.0,
                                                           "kp_max": None}  # fmt: skip
    for tile in near:
        for period in record.candidate_periods("forecast", t0, t1):
            start = int(np.datetime64(period, "ms").astype(np.int64))
            hours = [start + h * HOUR_MS for h in range(6)]
            downloads.keep(store, "forecast", tile, period,
                           reply(tile, 0.1, hours, calm, elevation=takeoff), 2)  # fmt: skip
    got = record.current(store, cache, PACK, flight)
    assert got is not None and got["quality"] == "forecast" and got["coverage"] == 1.0
    assert got["pressure_hpa"] == pytest.approx(1000.0)
    assert got["wind_ms"] is not None and got["wind_from_deg"] is not None
    assert 0 <= got["density_altitude_m"] < 600
    # ERA5 arrives for the flight's day: the record is made again from it
    day = record._day_label(t0)  # pyright: ignore[reportPrivateUsage]
    start = int(np.datetime64(day, "ms").astype(np.int64))
    hours = [start + h * HOUR_MS for h in range(24 + 1)]
    warm = {**calm, "temperature_2m": const(25.0)}
    for tile in near:
        downloads.keep(store, "reanalysis", tile, day,
                       reply(tile, 0.25, hours[:24], warm, elevation=takeoff), 3)  # fmt: skip
        nxt = record._day_label(start + 24 * HOUR_MS)  # pyright: ignore[reportPrivateUsage]
        midnight = reply(tile, 0.25, [start + 24 * HOUR_MS], warm, elevation=takeoff)
        downloads.keep(store, "reanalysis", tile, nxt, midnight, 3)
    again = record.current(store, cache, PACK, flight)
    assert again is not None and again["quality"] == "reanalysis"
    assert again["temperature_c"] == pytest.approx(25.0)
    # unchanged downloads: the kept record, not made again
    assert record.current(store, cache, PACK, flight) == again
    # space weather during the flight
    kp = [["time_tag", "Kp"], [iso(t0 - t0 % (3 * HOUR_MS)).replace("T", " "), "5.33"]]
    downloads.keep(store, "kp", "", record._hour_label(t0), json.dumps(kp).encode(), 4)  # pyright: ignore[reportPrivateUsage]
    assert record.current(store, cache, PACK, flight)["kp_max"] == 5.33  # type: ignore[index]


def test_the_record_holds_no_position(flown: tuple[Store, ColumnCache, str]) -> None:
    store, cache, flight = flown
    window = record._window(store, flight)  # pyright: ignore[reportPrivateUsage]
    assert window is not None
    for tile in tiles.download_list(store, TILES):
        for period in record.candidate_periods("forecast", *window):
            start = int(np.datetime64(period, "ms").astype(np.int64))
            hours = [start + h * HOUR_MS for h in range(6)]
            downloads.keep(store, "forecast", tile, period, reply(tile, 0.1, hours, FIELDS), 2)
    got = record.current(store, cache, PACK, flight)
    assert got is not None and got["coverage"] > 0
    allowed = {
        "quality", "coverage", "height_band_m", "wind_ms", "wind_max_ms", "wind_from_deg",
        "surface_wind_ms", "gust_max_ms", "precipitation_mm_h", "cloud_cover_pct",
        "boundary_layer_m", "temperature_c", "pressure_hpa", "humidity_pct", "dew_point_c",
        "density_altitude_m", "cloud_base_m", "density_altitude_top_m", "kp_max",
    }  # fmt: skip
    assert set(got) <= allowed
    key = str(store.get("flight", flight)["flight_key"])  # type: ignore[index]
    pos = cache.read(key, "POS", ["Lat", "Lng"], positions=True)
    lat, lon = pos.column("Lat").to_numpy()[0] * 1e-7, pos.column("Lng").to_numpy()[0] * 1e-7
    text = json.dumps(got)
    for value in (lat, lon):
        for places in (2, 3, 4):
            assert f"{value:.{places}f}" not in text


def test_the_record_method(tmp_path: Path) -> None:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, PACK)
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys())
    missing = call(server, out, 2, "flight.weather", flight_id="nope")
    assert missing["error"]["code"] == -32003 and missing["error"]["message"] == "no such flight"
    assert call(server, out, 3, "flight.weather", flight_id=7)["error"]["code"] == -32602
