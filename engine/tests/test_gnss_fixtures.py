"""The GNSS analyzer on ArduCopter 4.7.0 SITL flights (tests/fixtures/sitl/4.7.0/
copter-quad-gnss-*): a jammed receiver, a 60 m position step with the signal healthy,
and an 80 m drift over 60 s that the estimator follows; and a calm hover with none."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_imports import airframe

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.gnss import GNSS
from flight_insights.analyzers.model import Result
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
NAMES = ("copter-quad-gnss-jam", "copter-quad-gnss-step", "copter-quad-gnss-ramp",
         "copter-quad-hover")  # fmt: skip


@pytest.fixture(scope="module")
def judged(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Result]:
    pack = load_pack()
    reg = Registry.load(pack)
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("gnss")
    out: dict[str, Result] = {}
    with open_store(folder / "gnss.db", keys, "e" * 32) as store:
        cache = ColumnCache(folder / "cache", keys)
        for name in NAMES:
            batch = scan(store, pack, bytes(32), [FIXTURES / f"{name}.bin"])
            af = airframe(store, pack, name)
            [res] = commit(store, pack, cache, batch, propose(store, pack, batch),
                           [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
            out[name] = GNSS.run(FlightData.load(store, cache, pack, res.flights[0]), reg)
    return out


def events(r: Result) -> list[tuple[str, str]]:
    return [(f.code, f.confidence) for f in r.findings]


def test_a_calm_hover_has_none(judged: dict[str, Result]) -> None:
    r = judged["copter-quad-hover"]
    assert r.findings == []
    assert {m.metric: m.zone for m in r.metrics}["gnss.disturbed_s"] == "target"


def test_a_jammed_receiver_is_jamming(judged: dict[str, Result]) -> None:
    r = judged["copter-quad-gnss-jam"]
    assert events(r) == [("gnss.jamming", "medium")]  # no u-blox status in SITL
    ev = r.evidence[0].values
    assert {"fix", "sats", "rejected"} <= set(ev["channels"])
    assert ev["fewest_sats"] <= 3 and ev["rejected_s"] > 5
    assert "EKFINAV failsafe" in r.findings[0].params["response"]
    assert ev["mode"] == "LAND" and ev["mode_reason"] == "EKF_FAILSAFE"


def test_a_position_step_with_a_healthy_signal_is_spoofing(judged: dict[str, Result]) -> None:
    r = judged["copter-quad-gnss-step"]
    assert [c for c, _ in events(r)] == ["gnss.spoofing"]
    ev = r.evidence[0].values
    assert {"rejected", "glitch", "track"} <= set(ev["channels"])
    assert ev["highest_test_ratio"] > 10  # 60 m against a few metres of expected error
    assert "fix" not in ev["channels"] and "sats" not in ev["channels"]
    position = next(e for e in r.evidence if e.id == "gnss:event:1:position")
    assert position.values["largest_track_gap_m"] == pytest.approx(60, abs=3)


def test_a_drift_the_estimator_follows_then_the_jump_back(judged: dict[str, Result]) -> None:
    r = judged["copter-quad-gnss-ramp"]
    assert [c for c, _ in events(r)] == ["gnss.spoofing", "gnss.spoofing"]
    drift, jump = r.evidence[0].values, next(
        e.values for e in r.evidence if e.id == "gnss:event:2")  # fmt: skip
    assert drift["channels"] == ["drift"] and drift["followed_drift"] is True
    assert drift["duration_s"] > 40 and 0.3 < drift["largest_innovation_mps"] < 1.0
    assert "never rejected it" in r.findings[0].params["response"]
    assert {"rejected", "track"} <= set(jump["channels"])
