"""The weather the shell downloads, kept in the encrypted store: when each tile was first
listed, each reply kept as it came under its source, tile and UTC period, and what is
refused: a reply that is not JSON, the provider's error reply, a name that is not a
tile, a period off the schedule."""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from flight_insights.store.db import Store, open_store
from flight_insights.store.keys import StoreKeys
from flight_insights.weather import downloads
from flight_insights.weather.downloads import DownloadError

ids = count(9000)
REPLY = json.dumps(
    [{"latitude": 50.0 + i / 10, "longitude": 30.0, "hourly": {"wind_speed_10m": [3.1] * 6}}
     for i in range(20)]
).encode()  # fmt: skip


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return open_store(tmp_path / "w.db", StoreKeys.generate(), "a" * 32)


def test_a_tile_keeps_the_time_it_was_first_listed(store: Store) -> None:
    assert downloads.note_tiles(store, ["N50E030", "N48E030"], 1000) == {
        "N48E030": 1000, "N50E030": 1000
    }  # fmt: skip
    got = downloads.note_tiles(store, ["N50E030", "N52E030"], 5000)
    assert got == {"N50E030": 1000, "N52E030": 5000}


def test_each_reply_is_kept_as_it_came(store: Store) -> None:
    size = downloads.keep(store, "forecast", "N50E030", "2026-10-26T06", REPLY, 7)
    assert 0 < size < len(REPLY)  # compressed
    assert downloads.body(store, "forecast", "N50E030", "2026-10-26T06") == REPLY
    downloads.keep(store, "reanalysis", "N50E030", "2026-09", b"{}", 8)
    downloads.keep(store, "reanalysis", "N50E030", "2026-10-19", b"{}", 9)
    downloads.keep(store, "kp", "", "2026-10-26T06", b"[]", 9)
    assert downloads.held(store, "reanalysis") == [
        ("N50E030", "2026-09"),
        ("N50E030", "2026-10-19"),
    ]
    assert downloads.held(store, "reanalysis", since="2026-10") == [("N50E030", "2026-10-19")]
    assert downloads.held(store, "kp") == [("", "2026-10-26T06")]
    # fetched again: the newer reply replaces it
    downloads.keep(store, "forecast", "N50E030", "2026-10-26T06", b'{"again": 1}', 10)
    assert downloads.body(store, "forecast", "N50E030", "2026-10-26T06") == b'{"again": 1}'
    assert downloads.body(store, "forecast", "N50E030", "2026-10-26T12") is None


@pytest.mark.parametrize(
    ("source", "tile", "period", "body", "why"),
    [
        ("radar", "N50E030", "2026-10", b"{}", "source"),
        ("kp", "N50E030", "2026-10", b"[]", "no tile"),
        ("forecast", "", "2026-10", b"{}", "tile"),
        ("forecast", "50.1,30.2", "2026-10", b"{}", "tile"),
        ("forecast", "N50E030", "2026-13", b"{}", "period"),
        ("forecast", "N50E030", "2026-10-32", b"{}", "period"),
        ("forecast", "N50E030", "2026-10-26T24", b"{}", "period"),
        ("forecast", "N50E030", "2026-10-26T06:00", b"{}", "period"),
        ("forecast", "N50E030", "2026-10-26T06", b"<html>busy</html>", "not JSON"),
        ("forecast", "N50E030", "2026-10-26T06", b'{"error": true, "reason": "x"}', "error"),
    ],
)  # fmt: skip
def test_what_is_refused(
    store: Store, source: str, tile: str, period: str, body: bytes, why: str
) -> None:  # fmt: skip
    with pytest.raises(DownloadError, match=why):
        downloads.keep(store, source, tile, period, body, 1)
    assert downloads.held(store, "forecast") == []


def test_the_shell_hands_over_a_file(tmp_path: Path) -> None:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out)
    call(server, out, next(ids), "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path),
         keys=keys())  # fmt: skip

    def rpc(method: str, **params: Any) -> dict[str, Any]:
        return call(server, out, next(ids), method, **params)

    reply = tmp_path / "reply.json"
    reply.write_bytes(REPLY)
    stored = rpc("weather.store", source="forecast", tile="N50E030", period="2026-10-26T06",
                 path=str(reply))  # fmt: skip
    assert stored["result"]["bytes"] > 0
    assert rpc("weather.held", source="forecast")["result"] == {
        "held": [["N50E030", "2026-10-26T06"]]
    }
    bad = rpc("weather.store", source="forecast", tile="N50E030", period="soon", path=str(reply))
    assert bad["error"]["code"] == -32003 and "period" in bad["error"]["message"]
    missing = rpc("weather.store", source="forecast", tile="N50E030", period="2026-10",
                  path=str(tmp_path / "gone.json"))  # fmt: skip
    assert missing["error"]["message"] == "no such file"
    assert rpc("weather.held", source="radar")["error"]["code"] == -32602
