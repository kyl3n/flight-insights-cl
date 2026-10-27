"""Weather's engine methods (spec: Weather and wind). The download methods are the
shell's alone: the shell downloads the fleet's tiles on a fixed schedule, and the
interface has no use for where the fleet flies. `flight.weather` is the interface's: it
holds values only, never a tile or a coordinate.

| Method | What it does |
| --- | --- |
| weather.tiles | Every tile to download, when this laptop first listed it, each grid's step |
| weather.store | Keeps one download the shell received, as it came (or gzipped, as shared) |
| weather.held | Which downloads of a source are kept, by tile and period |
| weather.export | Writes one kept download, gzipped, to a file for the shell to share |
| weather.refresh | Makes again every flight's record whose downloads changed, after a round |
| flight.weather | A flight's record (values only), its measured wind, the gap, NOAA's Kp level |
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flight_insights.rpc.server import BAD_INPUT, INVALID_PARAMS, Context, RpcError, Server
from flight_insights.weather import downloads, measured, record, tiles

if TYPE_CHECKING:
    from flight_insights.rpc.methods import EngineState


def _text(params: dict[str, Any], name: str) -> str:
    value = params.get(name, "")
    if not isinstance(value, str):
        raise RpcError(INVALID_PARAMS, f"{name} must be text")
    return value


def _now_ms() -> int:
    return int(time.time() * 1000)


def add_weather_methods(server: Server, state: EngineState, guarded: Callable[[Any], Any]) -> None:
    @server.method("weather.tiles")
    @guarded
    def weather_tiles(ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
        store, cache = state.require_store(), state.cache
        assert cache is not None
        rules = tiles.TileRules.load(state.pack)
        tiles.scan(store, cache, rules, ctx.progress)
        added = downloads.note_tiles(store, tiles.download_list(store, rules), _now_ms())
        return {
            "tile_deg": rules.tile_deg,
            "grids_deg": dict(rules.grids_deg),
            "tiles": [{"tile": t, "added_ms": ms} for t, ms in added.items()],
        }

    @server.method("weather.store")
    @guarded
    def weather_store(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        path = Path(_text(params, "path"))
        if not path.is_file():
            raise RpcError(BAD_INPUT, "no such file")
        if path.stat().st_size > downloads.MAX_BYTES:
            raise RpcError(BAD_INPUT, "the download is larger than any reply the schedule asks for")
        gz = params.get("gzip", False)
        if not isinstance(gz, bool):
            raise RpcError(INVALID_PARAMS, "gzip must be true or false")
        try:
            size = downloads.keep(
                state.require_store(), _text(params, "source"), _text(params, "tile"),
                _text(params, "period"), path.read_bytes(), _now_ms(), gzipped=gz,
            )  # fmt: skip
        except downloads.DownloadError as e:
            raise RpcError(BAD_INPUT, str(e)) from None
        return {"bytes": size}

    @server.method("weather.held")
    @guarded
    def weather_held(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        try:
            got = downloads.held(
                state.require_store(), _text(params, "source"), _text(params, "since")
            )
        except downloads.DownloadError as e:
            raise RpcError(INVALID_PARAMS, str(e)) from None
        return {"held": [[t, p] for t, p in got]}

    @server.method("weather.export")
    @guarded
    def weather_export(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        path = Path(_text(params, "path"))
        if not path.is_absolute() or not path.parent.is_dir() or path.exists():
            raise RpcError(BAD_INPUT, "path must be a new file in a folder that exists")
        kept = downloads.packed(
            state.require_store(), _text(params, "source"), _text(params, "tile"),
            _text(params, "period"),
        )  # fmt: skip
        if kept is None:
            raise RpcError(BAD_INPUT, "that download is not kept here")
        path.write_bytes(kept)
        return {"bytes": len(kept)}

    @server.method("weather.refresh")
    @guarded
    def weather_refresh(ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
        store, cache = state.require_store(), state.cache
        assert cache is not None
        return {"made": record.refresh(store, cache, state.pack, progress=ctx.progress)}

    @server.method("flight.weather")
    @guarded
    def flight_weather(_ctx: Context, params: dict[str, Any]) -> dict[str, Any]:
        flight = _text(params, "flight_id")
        store, cache = state.require_store(), state.cache
        assert cache is not None
        if store.get("flight", flight) is None:
            raise RpcError(BAD_INPUT, "no such flight")
        tiles.scan(store, cache, tiles.TileRules.load(state.pack))
        made = record.current(store, cache, state.pack, flight)
        own = measured.measured(store, cache, state.pack, flight)
        kp = float(state.pack.json("rules/weather.json")["space_weather"]["kp_elevated"])
        return {
            "record": made, "measured": own, "difference": measured.difference(own, made),
            "kp_elevated": kp,
        }  # fmt: skip
