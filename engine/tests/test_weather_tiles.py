"""The weather tiles: each flight's positions snapped on the laptop to 2 degree tiles,
only tile names leaving the engine, the ring around each flown tile, the date line and
the poles, and each source's grid of points across a tile."""

from __future__ import annotations

import json
import re
import shutil
from itertools import count
from pathlib import Path
from typing import Any, cast

import numpy as np
import pyarrow as pa
import pytest
from test_imports import airframe

from flight_insights.ai.tripwire import flight_points
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import Pack, load_pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store, open_store
from flight_insights.store.keys import StoreKeys
from flight_insights.weather import tiles
from flight_insights.weather.tiles import TileError, TileRules

PACK = load_pack()
RULES = TileRules.load(PACK)
FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
LOGS = ("copter-quad-fs-radio.bin", "copter-quad-fs-off.bin")
ids = count(7000)


def at(*points: tuple[float, float]) -> list[str]:
    lat, lon = np.array([p[0] for p in points]), np.array([p[1] for p in points])
    return tiles.tiles_of(lat, lon, RULES)


def test_tiles_are_named_by_their_south_west_corner() -> None:
    assert RULES.tile_deg == 2 and RULES.neighbours == 1
    assert at((50.5, 30.5)) == ["N50E030"]
    assert at((51.999, 31.999)) == ["N50E030"]
    assert at((-33.9, -58.4)) == ["S34W060"]
    assert at((0.5, -0.5)) == ["N00W002"]
    assert at((-0.5, 0.5)) == ["S02E000"]
    # the edges: the north pole is in the top row, 180 E is 180 W
    assert at((90.0, 10.0)) == ["N88E010"] and at((-90.0, 10.0)) == ["S90E010"]
    assert at((10.0, 180.0)) == at((10.0, -180.0)) == ["N10W180"]
    # no fix (0, 0), and nonsense, is no position
    assert at((0.0, 0.0), (float("nan"), 3.0), (95.0, 3.0), (3.0, 200.0)) == []


def test_every_tile_name_reads_back_to_its_corner() -> None:
    names = {tiles.name(lat, lon) for lat in range(-90, 90, 2) for lon in range(-180, 180, 2)}
    assert len(names) == 90 * 180
    for n in names:
        assert re.fullmatch(r"[NS]\d{2}[EW]\d{3}", n)
        assert tiles.name(*tiles.corner(n)) == n
    for bad in ("N90E000", "N00E180", "n50e030", "N5E030", "N50E30", "X50E030"):
        with pytest.raises(TileError):
            tiles.corner(bad)


def test_a_few_stray_positions_add_no_tile() -> None:
    """A GNSS glitch or a spoofed jump puts a handful of positions far away: a tile
    counts only with 1% of the flight's positions."""
    home = [(50.5 + i * 1e-4, 30.5) for i in range(199)]
    assert at(*home, (10.0, 10.0)) == ["N50E030"]  # 1 in 200
    assert at(*home[:99], (10.0, 10.0)) == ["N10E010", "N50E030"]  # 1 in 100


def test_the_ring_round_a_tile() -> None:
    assert tiles.neighbours("N50E030", RULES) == [
        "N48E028", "N48E030", "N48E032", "N50E028", "N50E030", "N50E032",
        "N52E028", "N52E030", "N52E032",
    ]  # fmt: skip
    # across the date line
    ring = tiles.neighbours("S20E178", RULES)
    assert len(ring) == 9 and {"S20E176", "S20E178", "S20W180", "S22W180", "S18W180"} <= set(ring)
    assert set(tiles.neighbours("S20W180", RULES)) >= {"S20E178", "S20W178"}
    # no row beyond the poles
    assert len(tiles.neighbours("N88E000", RULES)) == 6
    assert len(tiles.neighbours("S90W180", RULES)) == 6
    assert all(r.startswith(("S90", "S88")) for r in tiles.neighbours("S90W180", RULES))


def test_each_sources_grid_covers_the_tiles_once() -> None:
    assert dict(RULES.grids_deg) == {"forecast": 0.1, "reanalysis": 0.25}
    forecast = tiles.grid("N50E030", 0.1, RULES)
    assert len(forecast) == 400 and forecast[0] == (50.0, 30.0) and forecast[-1] == (51.9, 31.9)
    assert len(tiles.grid("N50E030", 0.25, RULES)) == 64
    # four tiles side by side make one even lattice: no point twice, none missing
    four = [p for t in ("N50E030", "N50E032", "N52E030", "N52E032")
            for p in tiles.grid(t, 0.1, RULES)]  # fmt: skip
    assert len(four) == len(set(four)) == 1600
    lats = sorted({p[0] for p in four})
    assert len(lats) == 40 and np.allclose(np.diff(lats), 0.1)
    assert tiles.grid("S90W180", 0.25, RULES)[0] == (-90.0, -180.0)


def test_rules_that_cannot_tile_the_globe_are_refused() -> None:
    class Rules:
        def __init__(self, **change: Any) -> None:
            self.doc = {**PACK.json("rules/weather.json"), **change}

        def json(self, _rel: str) -> Any:
            return self.doc

    with pytest.raises(TileError, match="dividing 180"):
        TileRules.load(cast(Pack, Rules(tile_deg=7)))
    with pytest.raises(TileError, match="does not fit"):
        TileRules.load(cast(Pack, Rules(grids_deg={"forecast": 0.3})))


def _position_field(name: str, unit: str) -> pa.Field[Any]:
    meta = {b"unit": unit.encode(), b"multiplier": b"1e-07", b"position": b"1"}
    return pa.field(name, pa.int32(), metadata=meta)


def _positions(lat: list[float], lon: list[float], status: list[int] | None = None) -> pa.Table:
    fields = [_position_field("Lat", "deglatitude"), _position_field("Lng", "deglongitude")]
    columns: list[Any] = [[round(v * 1e7) for v in lat], [round(v * 1e7) for v in lon]]
    if status is not None:
        fields.append(pa.field("Status", pa.uint8()))
        columns.append(status)
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


def test_gps_with_a_3d_fix_stands_in_for_the_estimator(tmp_path: Path) -> None:
    cache = ColumnCache(tmp_path, StoreKeys.generate())
    assert tiles.flight_tiles(cache, "none", RULES) is None  # not on this laptop
    lat, lon = [-12.5] * 50 + [40.5] * 50, [130.5] * 50 + [-3.5] * 50
    cache.write_table("gps", "GPS", _positions(lat, lon, [2] * 50 + [3] * 50))
    assert tiles.flight_tiles(cache, "gps", RULES) == ["N40W004"]  # 2D fixes left out
    cache.write_table("both", "GPS", _positions(lat, lon, [3] * 100))
    cache.write_table("both", "POS", _positions([-12.5], [130.5]))
    assert tiles.flight_tiles(cache, "both", RULES) == ["S14E130"]  # the estimator's
    cache.write_table("nofix", "POS", _positions([0.0], [0.0]))
    cache.write_table("nofix", "GPS", _positions([0.0], [0.0], [1]))
    assert tiles.flight_tiles(cache, "nofix", RULES) == []


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> tuple[Store, ColumnCache, list[str]]:
    data = tmp_path_factory.mktemp("tiles")
    keys = StoreKeys.generate()
    store = open_store(data / "t.db", keys, "f" * 32)
    cache = ColumnCache(data / "cache", keys)
    flights: list[str] = []
    for log in LOGS:
        batch = scan(store, PACK, bytes(32), [FIXTURES / log])
        af = airframe(store, PACK, log)
        [res] = commit(store, PACK, cache, batch, propose(store, PACK, batch),
                       [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
        flights += res.flights
    return store, cache, flights


def _key(store: Store, flight_id: str) -> str:
    row = store.get("flight", flight_id)
    assert row is not None
    return str(row["flight_key"])


def _own(store: Store, flight_id: str) -> list[str]:
    row = store.conn.execute(
        "SELECT tiles FROM flight_tiles WHERE flight_id = ?", (flight_id,)
    ).fetchone()
    return [] if row is None else list(json.loads(row[0]))


def test_each_flight_is_tiled_where_it_flew(fleet: tuple[Store, ColumnCache, list[str]]) -> None:
    store, cache, flights = fleet
    assert tiles.scan(store, cache, RULES) == 2
    assert tiles.scan(store, cache, RULES) == 0  # once each
    for flight in flights:
        [own] = _own(store, flight)
        # checked against the positions read another way: the tripwire's reader
        points = np.array(flight_points(cache, _key(store, flight)))
        lat, lon = np.median(points[:, 0]), np.median(points[:, 1])
        assert own == tiles.name(int(lat // 2 * 2), int(lon // 2 * 2))
    flown = {t for f in flights for t in _own(store, f)}
    want = sorted({t for tile in flown for t in tiles.neighbours(tile, RULES)})
    assert tiles.download_list(store, RULES) == want and len(want) == 9 * len(flown)


def test_a_flight_is_tiled_once_its_log_is_here(
    fleet: tuple[Store, ColumnCache, list[str]], tmp_path: Path
) -> None:
    store, cache, flights = fleet
    tiles.scan(store, cache, RULES)
    before = tiles.download_list(store, RULES)
    folder = cache.path(_key(store, flights[0]), "x").parent
    shutil.move(folder, tmp_path / "away")
    with store.transaction() as conn:
        conn.execute("DELETE FROM flight_tiles WHERE flight_id = ?", (flights[0],))
    assert tiles.scan(store, cache, RULES) == 0 and _own(store, flights[0]) == []
    shutil.move(tmp_path / "away", folder)
    assert tiles.scan(store, cache, RULES) == 1
    assert tiles.download_list(store, RULES) == before
    # new rules find every flight's tiles again
    wider = TileRules(2, 2, 0.05, 3, RULES.grids_deg)
    assert tiles.scan(store, cache, wider) == 2
    assert len(tiles.download_list(store, wider)) == 25 * len(
        {t for f in flights for t in _own(store, f)}
    )
    assert tiles.scan(store, cache, RULES) == 2


def test_the_shell_gets_tile_names_and_nothing_else(tmp_path: Path) -> None:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, PACK)
    call(server, out, next(ids), "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path),
         keys=keys())  # fmt: skip

    def rpc(method: str, **params: Any) -> Any:
        reply = call(server, out, next(ids), method, **params)
        assert "error" not in reply, reply
        return reply["result"]

    assert rpc("weather.tiles") == {
        "tile_deg": 2, "grids_deg": {"forecast": 0.1, "reanalysis": 0.25}, "tiles": []
    }  # fmt: skip
    log = FIXTURES / LOGS[0]
    pl = rpc("platform.create", name="Q", vehicle="copter")
    af = rpc("airframe.create", platform_id=pl["id"], name="Q-1")
    scanned = rpc("import.scan", paths=[str(log)])
    rpc("import.commit", batch=scanned["batch"], decisions=[{"log": "f1", "airframe_id": af["id"]}])
    got = rpc("weather.tiles")
    names = [t["tile"] for t in got["tiles"]]
    assert len(names) == 9 and all(tiles.TILE.match(t) for t in names)
    assert set(got) == {"tile_deg", "grids_deg", "tiles"}
    assert all(set(t) == {"tile", "added_ms"} for t in got["tiles"])
    text = json.dumps({**got, "tiles": names})  # added_ms: a time, kept apart
    numbers = re.findall(r"-?\d+\.\d+", text)
    assert sorted(numbers) == ["0.1", "0.25"]  # no position in any form
