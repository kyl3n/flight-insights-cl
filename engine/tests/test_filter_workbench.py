"""The filter workbench: the flight's own spectra and numbers, a candidate's prediction,
the spectrogram pooled for display only, and what the RPC refuses."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from test_rpc import LAPTOP, Output, call, keys

from flight_insights.filters.workbench import _pool  # pyright: ignore[reportPrivateUsage]
from flight_insights.knowledge import load_pack
from flight_insights.rpc import build_server
from flight_insights.rpc.server import Server

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"


def test_pooling_averages_neighbours_and_keeps_short_axes() -> None:
    a = np.arange(10, dtype=np.float64)[None, :]
    assert _pool(a, 5, 1).tolist() == [[0.5, 2.5, 4.5, 6.5, 8.5]]
    assert _pool(a, 20, 1) is a


@pytest.fixture(scope="module")
def bench(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Server, Output, dict[str, str]]]:
    out = Output()
    server = build_server(out, load_pack())
    data = tmp_path_factory.mktemp("wb")
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(data), keys=keys())
    pl = call(server, out, 2, "platform.create", name="HL", vehicle="copter")["result"]
    af = call(server, out, 3, "airframe.create", platform_id=pl["id"], name="HL1")["result"]
    flights: dict[str, str] = {}
    for n, name in enumerate(("copter-heavylift-loaded.bin", "plane-circuit.bin")):
        scanned = call(server, out, 10 + n, "import.scan", paths=[str(FIXTURES / name)])["result"]
        [done] = call(
            server, out, 20 + n, "import.commit", batch=scanned["batch"],
            decisions=[{"log": "f1", "airframe_id": af["id"]}],
        )["result"]  # fmt: skip
        flights[name] = done["flights"][0]
    yield server, out, flights
    call(server, out, 99, "engine.shutdown")


def wb(server: Server, out: Output, rid: int, **params: Any) -> dict[str, Any]:
    return call(server, out, rid, "filter.workbench", **params)


def test_the_flights_own_filters_match_the_analyzer(
    bench: tuple[Server, Output, dict[str, str]],
) -> None:
    server, out, flights = bench
    r = wb(server, out, 30, flight_id=flights["copter-heavylift-loaded.bin"])["result"]
    assert (r["segment"], r["rate_hz"], r["flown_from"]) == ("hover", 1000.0, "logged")
    cur = r["current"]
    assert cur["lag_deg"] == pytest.approx(93.5, abs=0.1)  # the analyzer's lag_now
    assert cur["centres_hz"][0] == pytest.approx(201.8, abs=0.1)  # where the notch sat
    assert len(r["before_db"]) == 3 and len(r["before_db"][0]) == len(r["freqs"])
    assert r["params"]["INS_HNTCH_BW"] == 90.0 and "candidate" not in r
    sg = r["spectrogram"]
    assert len(sg["t_s"]) <= 240 and len(sg["freqs"]) <= 256 and len(sg["db"]) == len(sg["t_s"])
    assert any(v is not None for v in sg["motor_hz"])  # ESC RPM over the spectrogram


def test_a_candidate_is_predicted_on_the_same_data(
    bench: tuple[Server, Output, dict[str, str]],
) -> None:
    server, out, flights = bench
    fid = flights["copter-heavylift-loaded.bin"]
    r = wb(server, out, 31, flight_id=fid, candidate={"INS_GYRO_FILTER": 30, "INS_HNTCH_BW": 40})
    cand, cur = r["result"]["candidate"], r["result"]["current"]
    assert cand["lag_deg"] < cur["lag_deg"]  # a higher low-pass costs less lag
    assert cand["noise_db"] > cur["noise_db"]  # and lets more noise through
    assert (cand["lowpass_hz"], cand["bandwidth_hz"]) == (30.0, 40.0)
    off = wb(server, out, 32, flight_id=fid, candidate={"INS_HNTCH_ENABLE": 0})["result"]
    assert off["candidate"]["notches"] == 0 and off["candidate"]["centres_hz"] == []


def test_the_rpc_refuses_what_it_cannot_try(bench: tuple[Server, Output, dict[str, str]]) -> None:
    server, out, flights = bench
    fid = flights["copter-heavylift-loaded.bin"]
    assert (
        wb(server, out, 33, flight_id=fid, candidate={"ATC_RAT_RLL_P": 0.2})["error"]["code"]
        == -32602
    )
    assert (
        wb(server, out, 34, flight_id=fid, candidate={"INS_HNTCH_BW": "wide"})["error"]["code"]
        == -32602
    )
    plane = wb(server, out, 35, flight_id=flights["plane-circuit.bin"])
    assert plane["error"]["code"] == -32003  # no pre-filter gyro data
    assert wb(server, out, 36, flight_id="none")["error"]["code"] == -32003
