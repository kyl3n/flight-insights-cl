"""The failsafe analyzer on ArduCopter 4.7.0 SITL flights that lost a link or ran a pack
down (tests/fixtures/sitl/4.7.0/copter-quad-fs-*): each response judged against the
firmware's own logic, each failsafe timed from when its loss began, and losses with their
failsafe switched off."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.failsafes import FAILSAFES
from flight_insights.analyzers.model import Finding
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
NAMES = (
    "copter-quad-fs-gcs",
    "copter-quad-fs-battery",
    "copter-quad-fs-radio",
    "copter-quad-fs-off",
)


@pytest.fixture(scope="module")
def judged(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[Finding]]:
    pack = load_pack()
    reg = Registry.load(pack)
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("failsafes")
    out: dict[str, list[Finding]] = {}
    with open_store(folder / "fs.db", keys, "e" * 32) as store:
        cache = ColumnCache(folder / "cache", keys)
        for name in NAMES:
            batch = scan(store, pack, bytes(32), [FIXTURES / f"{name}.bin"])
            [res] = commit(store, pack, cache, batch, propose(store, pack, batch), [Decision("f1")])
            flight = FlightData.load(store, cache, pack, res.flights[0])
            result = FAILSAFES.run(flight, reg)
            assert result.skipped is None
            out[name] = result.findings
    return out


def summary(f: Finding) -> tuple[str, str, str]:
    return f.code.removeprefix("failsafe."), f.severity, f.confidence


def timing(f: Finding) -> dict[str, Any]:
    v = f.evidence[0].values
    return {k: v[k] for k in ("loss_from", "waited_s", "wait_s", "late_s")}


def test_the_gcs_failsafe_returns_five_seconds_after_the_gcs_was_last_heard(
    judged: dict[str, list[Finding]],
) -> None:
    [f] = judged["copter-quad-fs-gcs"]
    assert summary(f) == ("as_configured", "info", "high")
    assert (f.params["mode"], f.params["response"]) == ("GUIDED", "switched to RTL")
    t = timing(f)
    assert t["loss_from"] == "the ground station last seen (MAV.mgs)"
    assert t["wait_s"] == 5.0 and 5.0 < t["waited_s"] < 5.0 + 0.334  # one 3 Hz check
    assert t["late_s"] == 0.0


def test_each_battery_stage_fires_one_check_after_its_timer(
    judged: dict[str, list[Finding]],
) -> None:
    low, critical = judged["copter-quad-fs-battery"]
    assert (summary(low)[0], low.params["response"]) == ("as_configured", "switched to RTL")
    assert low.confidence == "medium"  # a first battery failsafe could be either stage
    assert summary(critical) == ("as_configured", "info", "high")  # one monitor: critical
    assert critical.params["response"] == "switched to LAND"
    assert timing(low) == {"loss_from": "Volt under BATT_LOW_VOLT 11 V", "waited_s": 2.1,
                           "wait_s": 2.0, "late_s": 0.0}  # fmt: skip
    assert timing(critical) == {"loss_from": "Volt under BATT_CRT_VOLT 9 V", "waited_s": 2.1,
                                "wait_s": 2.0, "late_s": 0.0}  # fmt: skip


def test_a_receiver_failsafe_falls_back_to_rtl_without_smartrtl(
    judged: dict[str, list[Finding]],
) -> None:
    [f] = judged["copter-quad-fs-radio"]
    assert summary(f) == ("fell_back", "caution", "high")
    assert (f.params["wanted"], f.params["response"]) == ("SMART_RTL", "switched to RTL")
    t = timing(f)
    assert t["loss_from"] == "the throttle RCIN.C3 under FS_THR_VALUE 975"
    assert 0 <= t["waited_s"] <= 0.1 and t["late_s"] == 0.0  # within a logged sample


def test_losses_with_their_failsafes_off_are_reported(judged: dict[str, list[Finding]]) -> None:
    by_failsafe = {f.params["failsafe"]: f for f in judged["copter-quad-fs-off"]}
    assert set(by_failsafe) == {"Ground station", "Radio"}
    gcs, radio = by_failsafe["Ground station"], by_failsafe["Radio"]
    # the pilot's radio was in hand (RC_OPTIONS bit 0 clear): the owner's call
    assert summary(gcs) == ("off_during_loss", "info", "high")
    assert gcs.params["setting"] == "FS_GCS_ENABLE 0"
    assert gcs.params["due_s"] == pytest.approx(gcs.params["at_s"] + 5.0, abs=0.1)
    # the receiver said it was lost, and nothing reacted
    assert summary(radio) == ("off_during_loss", "safety", "high")
    assert radio.params["setting"] == "FS_THR_ENABLE 0"
    assert radio.params["what"] == "the throttle under FS_THR_VALUE 975"
    assert radio.params["at_s"] > gcs.params["due_s"]  # the receiver went second
