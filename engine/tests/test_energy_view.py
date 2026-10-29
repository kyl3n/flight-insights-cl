"""The Energy section of the Propulsion view (energy_view.py, RPC flight.energy).

- Synthetic: four ESCs reporting their voltage and current split the packs' energy into
  the ESCs' input, the harness and the avionics, by the arithmetic of the inputs; ESCs that
  report more than the packs give, or a motor without telemetry, or the simulator, give no
  split, with why; battery heat is the current squared times the logged resistance.
- On ArduPilot's simulator (Callisto, the heavy-lift X8): the figures are the energy
  analyzer's, the parts add up to what the cells gave, the speed card's fast run is the
  fast analyzer's, and the flight sits on its airframe's curve, while the loaded card
  entered at its light weight sits well above it and raises the anomaly findings.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from flight_builder import ARM, DISARM, hover_flight
from test_perf_store import CALLISTO, FIXTURES, PACK
from test_power import battery, escs

from flight_insights.analyzers import Registry, steady
from flight_insights.analyzers.battery_power import supply
from flight_insights.energy_view import breakdown
from flight_insights.performance.efficiency import HOVER, LEVEL

REG = Registry.load(PACK)
PHASES = {k: 0.0 for k in (*steady.PHASES, "untiled")}


QUAD = {f"SERVO{n}_FUNCTION": 32.0 + n for n in range(1, 5)}  # motors 1 to 4


def split(tables: dict[str, Any], sim: bool = False) -> dict[str, Any]:
    flight = hover_flight(tables, QUAD, {"board": {"hal": "SITL"}} if sim else None)
    packs = supply(flight, REG.rules["batteries"])
    assert packs is not None
    used = (
        float(
            np.trapezoid(
                packs.power[(packs.t >= ARM) & (packs.t <= DISARM)],
                packs.t[(packs.t >= ARM) & (packs.t <= DISARM)] / 1e6,
            )
        )
        / 3600
    )
    return breakdown(flight, REG, packs, _no_tiles(), np.array([], str), used,
                      PHASES | {"untiled": used})  # fmt: skip


def _no_tiles() -> steady.Tiles:
    empty = np.zeros(0)
    return steady.Tiles(empty.astype(np.int64), empty.astype(np.int64), empty, empty,
                        np.zeros((0, 3)), np.zeros((0, 3)), empty, empty, empty)  # fmt: skip


def parts(b: dict[str, Any]) -> dict[str, float]:
    return {p["part"]: p["wh"] for p in b["parts"]}


def test_the_escs_split_the_packs_energy() -> None:
    # 40 A from a 50 V pack of 0.02 ohm: 49.2 V, 1968 W; four ESCs at 48.8 V and 9 A take
    # 1756.8 W; the harness loses 0.4 V at 40 A, 16 W; the avionics get the other 195.2 W
    b = split({"BAT": battery(), "ESC": escs(48.8, 9.0)})
    got = parts(b)
    assert b["split"] and b["split_why"] is None
    total = got["untiled"] + got["harness"] + got["avionics"]
    assert total == pytest.approx(b["terminal_wh"], abs=0.02)
    assert got["harness"] / total == pytest.approx(16 / 1968, rel=0.1)
    assert got["avionics"] / total == pytest.approx(195.2 / 1968, rel=0.05)
    # the cells gave the heat too: 40 A squared through 0.02 ohm, 32 W
    assert got["heat"] / total == pytest.approx(32 / 1968, rel=0.1)
    assert b["cells_wh"] == pytest.approx(b["terminal_wh"] + got["heat"], abs=0.02)


@pytest.mark.parametrize(
    ("tables", "sim", "why"),
    [
        ({"ESC": escs(49.9, 12.0)}, False, "reads high"),  # 2395 W from ESCs, 1968 from packs
        ({"ESC": escs(48.8, 9.0, motors=3)}, False, "not every motor"),
        ({"ESC": escs(48.8, 9.0)}, True, "simulator"),
        ({}, False, "no ESC voltage"),
    ],
)
def test_no_split_without_whole_and_believable_esc_telemetry(
    tables: dict[str, Any], sim: bool, why: str
) -> None:
    b = split({"BAT": battery(), **tables}, sim)
    assert not b["split"] and why in b["split_why"]
    assert {"harness", "avionics"}.isdisjoint(parts(b))
    assert parts(b)["untiled"] == pytest.approx(b["terminal_wh"], abs=0.1)


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    from test_rpc import LAPTOP, Output, call
    from test_rpc import keys as store_keys

    from flight_insights.rpc import build_server

    out = Output()
    srv = build_server(out, PACK)
    folder: Path = tmp_path_factory.mktemp("energy")
    call(srv, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(folder),
         keys=store_keys())  # fmt: skip
    pl = call(srv, out, 2, "platform.create", name="Callisto", vehicle="copter")["result"]
    version = pl["reference_build"]["version"]
    call(srv, out, 3, "platform.save", id=pl["id"], build_version=version, values=CALLISTO)
    af = call(srv, out, 4, "airframe.create", platform_id=pl["id"], name="C-1")["result"]["id"]
    flights: dict[str, str] = {}
    analyses: dict[str, Any] = {}
    got: dict[str, Any] = {}
    flown = (
        ("sweep", 32.5),
        ("light", 32.5),
        ("loaded", 40.5),
        ("discharge", 32.5),
        ("sweep-loaded", 32.5),
    )  # the last entered at the light weight: 40.5 kg flown
    for i, (name, kg) in enumerate(flown):
        if name == "sweep-loaded":  # the clean flights judged on a model of clean flights
            got |= {n: call(srv, out, 50 + k, "flight.energy", flight_id=fid)
                    for k, (n, fid) in enumerate(flights.items())}  # fmt: skip
        scanned = call(
            srv, out, 10 + i, "import.scan", paths=[str(FIXTURES / f"copter-heavylift-{name}.bin")]
        )["result"]
        decision = {"log": "f1", "airframe_id": af, "takeoff_kg": kg}
        [done] = call(srv, out, 20 + i, "import.commit", batch=scanned["batch"],
                      decisions=[decision])["result"]  # fmt: skip
        flights[name] = done["flights"][0]
        analyses[name] = call(srv, out, 30 + i, "analysis.run",
                              flight_id=flights[name])["result"]  # fmt: skip
    got["sweep-loaded"] = call(srv, out, 59, "flight.energy", flight_id=flights["sweep-loaded"])
    bad = call(srv, out, 60, "flight.energy", flight_id="no-such-flight")
    return {"got": got, "analyses": analyses, "bad": bad}


def evidence(analysis: dict[str, Any], eid: str) -> dict[str, Any]:
    [e] = [e for e in analysis["evidence"] if e["id"] == eid]
    return e["values"]


def test_the_figures_are_the_energy_analyzers(server: dict[str, Any]) -> None:
    for name, reply in server["got"].items():
        v = reply["result"]
        ev = evidence(server["analyses"][name], "energy:flight")
        f = v["figures"]
        assert f["used_wh"] == pytest.approx(ev["used_wh"], abs=0.1)
        assert f["flown_m"] == ev["flown_m"] and f["used_mah"] == ev["used_mah"]
        assert f["wh_per_km"] == ev["wh_per_km"]
        # BATT_CAPACITY until the packs' capacity is measured over enough flights
        assert f["capacity_mah"] and 0 < f["share_pct"] < 100


def test_the_parts_add_up_to_what_the_cells_gave(server: dict[str, Any]) -> None:
    for reply in server["got"].values():
        b = reply["result"]["breakdown"]
        assert not b["split"] and "simulator" in b["split_why"]
        got = parts(b)
        phases = sum(v for k, v in got.items() if k != "heat")
        assert phases == pytest.approx(b["terminal_wh"], abs=0.5)
        assert b["cells_wh"] == pytest.approx(b["terminal_wh"] + got["heat"], abs=0.2)


def test_power_over_time_is_reduced_for_display_with_the_phases(
    server: dict[str, Any],
) -> None:
    v = server["got"]["sweep"]["result"]
    start, end = v["window_s"]
    assert (
        0 < len(v["power"]["t"]) <= 600 and start <= v["power"]["t"][0] < v["power"]["t"][-1] <= end
    )
    assert all(lo <= hi for lo, hi in zip(v["power"]["lo"], v["power"]["hi"], strict=True))
    spans = v["phases"]
    assert {s["phase"] for s in spans} <= set(steady.PHASES) and "level" in {
        s["phase"] for s in spans
    }
    assert all(a["end_s"] <= b["start_s"] for a, b in pairwise(spans))
    assert v["armed_s"][0] <= spans[0]["start_s"] and spans[-1]["end_s"] <= v["armed_s"][1]


def test_the_fast_run_is_the_fast_analyzers(server: dict[str, Any]) -> None:
    v = server["got"]["sweep"]["result"]
    seg = evidence(server["analyses"]["sweep"], "fast:segment")
    assert v["fast"]["speed_ms"] == seg["speed_ms"] and len(v["fast"]["spans_s"]) == seg["spans"]
    assert v["watch"]["values"]["speed_ms"] == seg["speed_ms"]
    metrics = {m["metric"] for m in v["watch"]["metrics"]}
    assert {"fast.speed_ms", "fast.vibe_p95", "fast.output_max"} <= metrics


def test_a_flight_on_its_airframes_curve(server: dict[str, Any]) -> None:
    c = server["got"]["sweep"]["result"]["on_curve"]
    assert c["why"] is None and c["mass_kg"] == 32.5 and c["level"] in ("A", "B")
    curve = c["curve"]
    assert curve["airspeed_ms"][0] == 0 and curve["airspeed_ms"][-1] >= 16
    assert all(lo <= p <= hi for lo, p, hi in
               zip(curve["low_w"], curve["power_w"], curve["high_w"], strict=True))  # fmt: skip
    a = c["against"]
    assert abs(a["excess_pct"]) < a["threshold_pct"] and a["others"] == 3
    assert a["interval_pct"][0] <= a["excess_pct"] <= a["interval_pct"][1]
    assert c["hover"]["measured_w"] > 0 and c["hover"]["standard_w"] > 0
    assert server["got"]["sweep"]["result"]["findings"] == []


def test_a_weight_entered_wrong_sits_above_the_curve(server: dict[str, Any]) -> None:
    v = server["got"]["sweep-loaded"]["result"]
    a = v["on_curve"]["against"]
    # 25% more thrust than entered: the simulator's motors draw power in proportion to it
    assert a["excess_pct"] > 25 and a["interval_pct"][0] > a["threshold_pct"]
    assert {f["code"] for f in v["findings"]} == {HOVER, LEVEL}


def test_no_such_flight(server: dict[str, Any]) -> None:
    assert "error" in server["bad"]
