"""The no-GNSS analyzer on ArduCopter 4.7.0 SITL flights (tests/fixtures/sitl/4.7.0/
copter-quad-nognss-*): a GNSS-off drill in wind with no other position source, and one
on optical flow as source set 2; and the readiness review of each flight's parameters,
which must agree with what the flight did."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_imports import airframe

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.gnss import GNSS
from flight_insights.analyzers.model import Result
from flight_insights.analyzers.no_gnss import NO_GNSS
from flight_insights.analyzers.no_gnss_readiness import NO_GNSS_READINESS
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
NAMES = ("copter-quad-nognss-drill", "copter-quad-nognss-flow")


@pytest.fixture(scope="module")
def judged(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Result, Result, Result]]:
    """Each flight's no-GNSS result, its GNSS result and its readiness."""
    pack = load_pack()
    reg = Registry.load(pack)
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("no_gnss")
    out: dict[str, tuple[Result, Result, Result]] = {}
    with open_store(folder / "no_gnss.db", keys, "e" * 32) as store:
        cache = ColumnCache(folder / "cache", keys)
        for name in NAMES:
            batch = scan(store, pack, bytes(32), [FIXTURES / f"{name}.bin"])
            af = airframe(store, pack, name)
            [res] = commit(store, pack, cache, batch, propose(store, pack, batch),
                           [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
            flight = FlightData.load(store, cache, pack, res.flights[0])
            out[name] = (NO_GNSS.run(flight, reg), GNSS.run(flight, reg),
                         NO_GNSS_READINESS.run(flight, reg))  # fmt: skip
    return out


def test_a_drill_with_no_other_source_gives_the_position_up(
    judged: dict[str, tuple[Result, Result, Result]],
) -> None:
    r, gnss, _ = judged["copter-quad-nognss-drill"]
    [f] = r.findings
    assert (f.code, f.params["what"]) == ("no_gnss.position_lost", "GNSS-off drill")
    ev = r.evidence[0].values
    assert ev["drill"] is True and 3 <= ev["detection_s"] <= 5 and 5 <= ev["absolute_s"] <= 9
    assert ev["mode"] == "LAND" and ev["mode_reason"] == "EKF_FAILSAFE"
    assert "FAILSAFE_EKFINAV" in ev["failsafes"]
    assert not any(e.id.endswith(":drift") for e in r.evidence)  # it claimed no position
    assert {m.metric: m.zone for m in r.metrics}["no_gnss.hold_s"] == "out"
    assert gnss.findings == []  # a drill is not a disturbance


def test_a_drill_on_optical_flow_keeps_a_position(
    judged: dict[str, tuple[Result, Result, Result]],
) -> None:
    r, gnss, _ = judged["copter-quad-nognss-flow"]
    [f] = r.findings
    assert (f.code, f.params["what"]) == ("no_gnss.position_held", "GNSS-off drill")
    ev = r.evidence[0].values
    assert ev["source_set"] == 2 and ev["using_gnss_at_start"] is False
    assert ev["kept_relative"] is True and ev["failsafes"] == [] and ev["mode"] is None
    drift = next(e for e in r.evidence if e.id == "no_gnss:outage:1:drift")
    assert drift.values["drift_m"] < 3  # optical flow held it in a 6 m/s wind
    # flow corrected the velocity, so the drift grows in step with the time
    assert ev["dead_reckoning_s"] is None and drift.values["dead_reckoning"] is False
    d, minutes = drift.values["drift_m"], ev["duration_s"] / 60 * 10  # d is rounded
    assert minutes / (d + 0.05) <= drift.values["minutes_to_tolerance"] <= minutes / (d - 0.05)
    metrics = {m.metric: (m.value, m.zone) for m in r.metrics}
    assert metrics["no_gnss.hold_s"][0] >= 30 and metrics["no_gnss.hold_s"][1] == "target"
    assert metrics["no_gnss.minutes_to_tolerance"] == (drift.values["minutes_to_tolerance"],
                                                       "target")  # fmt: skip
    assert gnss.findings == []


def readiness(r: Result) -> dict[str, dict[str, object]]:
    prefix = "no_gnss:readiness:"
    return {e.id[len(prefix) :]: dict(e.values) for e in r.evidence if e.id.startswith(prefix)}


def test_readiness_foresees_what_each_drill_did(
    judged: dict[str, tuple[Result, Result, Result]],
) -> None:
    """The drill with no other source gave its position up: readiness finds no source
    set for flying without GNSS. The flow flight kept one: readiness finds set 2 ready on
    the RC8 switch. Neither has drag fusion, so neither could dead-reckon."""
    *_, drill = judged["copter-quad-nognss-drill"]
    areas = readiness(drill)
    assert areas["position"]["status"] == "none"
    assert areas["pilot_fallback"]["drill_switch_channels"] == [7]
    *_, flow = judged["copter-quad-nognss-flow"]
    areas = readiness(flow)
    assert areas["position"]["status"] == "ready" and areas["position"]["sets"] == [2]
    assert areas["switching"]["switch_channels"] == [8]
    for r in (drill, flow):
        assert [f.code for f in r.findings] == ["no_gnss.drag_fusion_off"]
