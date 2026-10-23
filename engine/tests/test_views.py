"""The propulsion and system views: traces reduced for display keep every spike, units
come from the log, and the views read a real flight over RPC."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from flight_insights.knowledge import load_pack
from flight_insights.views import BINS, envelope

FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"


def test_the_display_envelope_keeps_every_spike() -> None:
    t = np.linspace(0.0, 100.0, 200_001)
    v = np.sin(t)
    v[123_457] = 40.0  # one sample in two hundred thousand
    v[7] = np.nan
    e = envelope(t, v, 0.0, 100.0)
    assert len(e["t"]) == BINS and max(e["hi"]) == 40.0
    assert min(e["lo"]) == pytest.approx(-1.0, abs=1e-3) and max(e["hi"][:100]) <= 1.0
    assert all(lo <= hi for lo, hi in zip(e["lo"], e["hi"], strict=True))
    assert e["t"] == sorted(e["t"]) and 0.0 < e["t"][0] < e["t"][-1] < 100.0


def test_a_short_trace_is_drawn_as_it_is_inside_its_window() -> None:
    t = np.arange(10.0)
    e = envelope(t, t * 2, 2.0, 5.0)
    doubled = [4.0, 6.0, 8.0, 10.0]
    assert e == {"t": [2.0, 3.0, 4.0, 5.0], "lo": doubled, "hi": doubled}
    assert envelope(t, t, 50.0, 60.0) == {"t": [], "lo": [], "hi": []}


@pytest.fixture(scope="module")
def flown(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, Any, str]:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, load_pack())
    data = tmp_path_factory.mktemp("views")
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(data), keys=keys())
    pl = call(server, out, 2, "platform.create", name="HL", vehicle="copter")["result"]
    af = call(server, out, 3, "airframe.create", platform_id=pl["id"], name="HL-1")["result"]
    log = FIXTURE / "copter-heavylift-loaded.bin"
    scanned = call(server, out, 4, "import.scan", paths=[str(log)])["result"]
    [done] = call(
        server, out, 5, "import.commit", batch=scanned["batch"],
        decisions=[{"log": "f1", "airframe_id": af["id"]}],
    )["result"]  # fmt: skip
    return server, out, done["flights"][0]


def test_propulsion_shows_each_motor_and_the_battery(flown: tuple[Any, Any, str]) -> None:
    from test_rpc import call

    server, out, flight = flown
    view = call(server, out, 10, "flight.propulsion", flight_id=flight)["result"]
    motors = view["motors"]
    assert [m["number"] for m in motors] == list(range(1, 9))  # the octocopter
    for m in motors:
        assert len(m["output"]["t"]) <= BINS and 0 < m["hover_output"] < 100
        assert m["rpm"]["hi"] and m["hover_rpm"] > 0
        assert "temp" not in m and "curr" not in m  # placeholders in the simulator
    assert view["simulated"] and view["units"]["rpm"] == "rpm"
    assert view["units"]["output"] == "%" and 0 < view["saturation_pct"] <= 100
    bat = view["battery"]
    assert bat["volt"]["hi"] and bat["power"]["hi"] and view["units"]["volt"] == "V"
    assert bat["lowest_volt"] > 0
    # The log starts at arming here, so there is no resting voltage before take-off.
    assert bat["resting_volt"] is None or bat["lowest_volt"] <= bat["resting_volt"]
    start, end = view["window_s"]
    assert start < view["armed_s"][0] < view["armed_s"][1] < end
    names = [m["mode"] for m in view["modes"]]
    assert view["hover_s"] and {"GUIDED", "LAND"} <= set(names)
    assert all(a != b for a, b in pairwise(names))  # changes only


def test_a_zoomed_window_gets_its_own_bins(flown: tuple[Any, Any, str]) -> None:
    from test_rpc import call

    server, out, flight = flown
    whole = call(server, out, 20, "flight.propulsion", flight_id=flight)["result"]
    a, b = whole["hover_s"][0]
    zoomed = call(server, out, 21, "flight.propulsion", flight_id=flight, window_s=[a, b])
    view = zoomed["result"]
    assert view["shown_s"] == [a, b] and view["window_s"] == whole["window_s"]
    rpm = view["motors"][0]["rpm"]["t"]
    inside = [t for t in whole["motors"][0]["rpm"]["t"] if a <= t <= b]
    assert all(a <= t <= b for t in rpm) and len(rpm) > 2 * len(inside)
    cases = (([1, 2, 3], -32602), (["a", 1], -32602), ([1e9, 2e9], -32003))
    for n, (bad, code) in enumerate(cases):
        reply = call(server, out, 30 + n, "flight.system", flight_id=flight, window_s=bad)
        assert reply["error"]["code"] == code  # each request its own id: one shared output


def test_the_system_view_reads_load_timing_and_errors(flown: tuple[Any, Any, str]) -> None:
    from test_rpc import call

    server, out, flight = flown
    view = call(server, out, 11, "flight.system", flight_id=flight)["result"]
    assert view["load"]["hi"] and view["units"]["load"] == "%"
    assert view["long_loops"]["t"] and view["loop_period_us"] == pytest.approx(2500.0)
    dropped = view["log_dropped"]
    assert dropped["lo"] and min(dropped["lo"]) >= 0  # counted from arming, not the log start
    assert isinstance(view["errors"], list)
    for e in view["errors"]:
        assert e["subsystem"] and e["meaning"]
    missing = call(server, out, 12, "flight.system", flight_id="no-such-flight")
    assert missing["error"]["code"] == -32003  # BAD_INPUT
