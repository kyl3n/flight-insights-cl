"""Weather's engine methods (spec: Weather and wind). Shell-only: the shell downloads the
fleet's tiles on a fixed schedule, and the interface has no use for where the fleet
flies.

| Method | What it does |
| --- | --- |
| weather.tiles | Every tile to download (each flight's and those around it), each grid's step |
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from flight_insights.rpc.server import Context, Server
from flight_insights.weather import tiles

if TYPE_CHECKING:
    from flight_insights.rpc.methods import EngineState


def add_weather_methods(server: Server, state: EngineState, guarded: Callable[[Any], Any]) -> None:
    @server.method("weather.tiles")
    @guarded
    def weather_tiles(ctx: Context, _params: dict[str, Any]) -> dict[str, Any]:
        store, cache = state.require_store(), state.cache
        assert cache is not None
        rules = tiles.TileRules.load(state.pack)
        tiles.scan(store, cache, rules, ctx.progress)
        return {
            "tile_deg": rules.tile_deg,
            "grids_deg": dict(rules.grids_deg),
            "tiles": tiles.download_list(store, rules),
        }
