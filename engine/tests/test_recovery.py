"""The recovery card: what the aircraft does when GNSS is lost or spoofed, and the
operator's steps, from its parameters, its readiness and its measured outages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from flight_builder import hover_flight
from test_no_gnss_readiness import DRAG, FLOW, POSITION_MODES, STOCK

from flight_insights.analyzers.no_gnss_readiness import NO_GNSS_READINESS
from flight_insights.analyzers.registry import Registry
from flight_insights.knowledge import load_pack
from flight_insights.recovery import Measured, build, card_text

PACK = load_pack()
DOC = PACK.json("recovery/copter.json")
SITL = Measured(outages=2, hold_s=6.9, detection_s=3.9)  # the SITL drills, on GNSS alone
NOTHING = Measured()


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(PACK)


def make(reg: Registry, **params: float) -> dict[str, Any]:
    return measured_as(reg, SITL, NOTHING, **params)


def measured_as(
    reg: Registry, alone: Measured, on_set: Measured, **params: float
) -> dict[str, Any]:
    p = STOCK | params
    readiness = NO_GNSS_READINESS.run(hover_flight({}, p), reg)
    return build(readiness, alone, on_set, p, DOC, 10.0, "HL1", "a3f9c2d1")


def keys(card: dict[str, Any], section: str, part: str = "steps") -> list[str]:
    [s] = [s for s in card["sections"] if s["id"] == section]
    return [p["key"] for p in s[part]]


def text(card: dict[str, Any], section: str, part: str = "steps", lang: str = "en") -> list[str]:
    [s] = [s for s in card["sections"] if s["id"] == section]
    return [p["text"][lang] for p in s[part]]


def test_a_stock_quad_holds_briefly_then_lands_and_is_flown_home_by_hand(reg: Registry) -> None:
    c = make(reg, FLTMODE2=2.0)
    assert text(c, "gnss_lost", "will") == [
        "hold its estimate for a short time, then the EKF failsafe will land it in place if "
        "it is in a mode that needs a position"
    ]
    assert keys(c, "gnss_lost", "facts") == ["fact.detection", "fact.hold", "fact.no_fallback_set"]
    assert text(c, "gnss_lost")[0] == "Switch to AltHold."  # AltHold before Stabilize
    assert keys(c, "gnss_lost") == ["step.mode", "step.heading", "step.fly_home", "step.land"]
    assert keys(c, "spoofing") == ["spoof.mode_first", "spoof.no_reject", "spoof.then"]
    assert c["fallback_mode"] == "AltHold"
    assert c["gaps"] == ["no_gnss.drag_fusion_off"]


def test_a_flow_set_on_a_switch_is_how_it_gets_home(reg: Registry) -> None:
    c = make(reg, **FLOW, **DRAG, RC8_OPTION=90.0, RC7_OPTION=65.0)
    # on its own it dead-reckons on the wind, and the failsafe acts at once
    assert keys(c, "gnss_lost", "will") == ["will.dead_reckon"]
    assert "(RTL)" in text(c, "gnss_lost", "will")[0]
    assert text(c, "gnss_lost") == [
        "Switch to source set 2 with RC8, and wait for the ground station's message that the "
        "source changed.",
        "Fly home in Loiter on optical flow, ready to take over in Stabilize.",
        "Land by sight; do not wait for GNSS to return.",
    ]
    assert text(c, "gnss_lost", lang="uk")[1] == (
        "Летіть додому в Loiter (джерела: оптичний потік), будучи готовими перебрати керування "
        "в Stabilize."
    )
    assert keys(c, "spoofing") == ["spoof.mode_first", "spoof.reject_switch", "spoof.then"]
    assert "RC7 high" in text(c, "spoofing")[1]


def test_without_a_switch_the_flow_set_is_named_and_flown_by_hand(reg: Registry) -> None:
    c = make(reg, **FLOW)
    assert keys(c, "gnss_lost") == [
        "step.no_switch", "step.mode", "step.heading", "step.fly_home", "step.land",
    ]  # fmt: skip


def test_a_first_source_set_that_copes_just_keeps_flying(reg: Registry) -> None:
    flow_first = {k: v for k, v in FLOW.items() if not k.startswith("EK3_SRC2")}
    c = make(reg, EK3_SRC1_POSXY=0.0, EK3_SRC1_VELXY=5.0, **flow_first)
    assert keys(c, "gnss_lost", "will") == ["will.keeps_set"]
    assert keys(c, "gnss_lost") == ["step.fly_home_set", "step.land"]


def test_flown_from_the_ground_station(reg: Registry) -> None:
    c = make(reg, **POSITION_MODES, RC_OPTIONS=33.0)
    assert keys(c, "gnss_lost")[0] == "step.mode_station"
    assert keys(c, "spoofing")[0] == "spoof.mode_station"
    no_mode = make(reg, **POSITION_MODES)
    assert keys(no_mode, "gnss_lost")[0] == "step.no_mode" and no_mode["fallback_mode"] is None


def test_a_heading_that_goes_with_gnss_means_flying_by_sight(reg: Registry) -> None:
    c = make(reg, EK3_SRC1_YAW=2.0)
    assert keys(c, "gnss_lost") == ["step.mode", "step.no_heading", "step.land"]


def test_rejecting_gnss_lands_it_when_the_ekf_failsafe_lands_from_every_mode(
    reg: Registry,
) -> None:
    c = make(reg, RC7_OPTION=65.0, FS_EKF_ACTION=3.0)
    assert keys(c, "spoofing") == [
        "spoof.mode_first", "spoof.reject_switch", "spoof.ekf_lands", "spoof.then",
    ]  # fmt: skip
    assert text(c, "gnss_lost", "will")[0].endswith("land it in place, in every mode")


def test_an_ekf_failsafe_switched_off(reg: Registry) -> None:
    assert keys(make(reg, FS_EKF_THRESH=0.0), "gnss_lost", "will") == ["will.ekf_off"]


def test_measured_drift_gives_the_range_and_nothing_measured_says_so(reg: Registry) -> None:
    drifting = measured_as(reg, Measured(outages=1, hold_s=30.0, drift_m_per_min=5.0), NOTHING)
    [fact] = [t for t in text(drifting, "gnss_lost", "facts") if "min before" in t]
    assert fact == (
        "On its own, about 2 min before its estimate is 10 m off (drift 5 m a minute, measured)."
    )
    assert keys(measured_as(reg, NOTHING, NOTHING), "gnss_lost", "facts")[0] == (
        "fact.hold_unmeasured"
    )


def test_a_hold_on_optical_flow_is_never_claimed_for_the_aircraft_on_its_own(
    reg: Registry,
) -> None:
    """As in the SITL flow drill: 30 s held on source set 2, which the aircraft only uses
    once the operator switches to it."""
    flow = Measured(outages=1, hold_s=30.2, drift_m_per_min=0.2)
    c = measured_as(reg, NOTHING, flow, **FLOW, RC8_OPTION=90.0)
    assert keys(c, "gnss_lost", "facts") == [
        "fact.hold_unmeasured", "fact.hold_set", "fact.range_set",
    ]  # fmt: skip
    assert text(c, "gnss_lost", "facts")[1:] == [
        "On source set 2 it kept a position it can fly on for at least 30.2 s (measured; "
        "outages and drills: 1).",
        "On source set 2, about 50 min before its estimate is 10 m off (drift 0.2 m a minute, "
        "measured).",
    ]
    # without a set to switch to, a hold measured on one says nothing about this aircraft
    assert "fact.hold_set" not in keys(measured_as(reg, NOTHING, flow), "gnss_lost", "facts")


def test_the_printed_card_in_both_languages(reg: Registry) -> None:
    c = make(reg, **FLOW, **DRAG, RC8_OPTION=90.0, RC7_OPTION=65.0)
    for lang in ("en", "uk"):
        printed = card_text(c, lang)
        assert "{" not in printed and "}" not in printed
        assert printed.startswith(c["title"][lang]) and printed.rstrip().endswith(c["footer"][lang])
    assert "Valid while the parameters stay a3f9c2d1." in card_text(c)


def test_every_wording_has_both_languages_and_no_stray_placeholders() -> None:
    for key, text_ in DOC["texts"].items():
        assert set(text_) == {"en", "uk"}, key
        for lang in ("en", "uk"):
            assert sorted(set(_placeholders(text_["en"]))) == sorted(
                set(_placeholders(text_[lang]))
            ), (key, lang)


def _placeholders(t: str) -> list[str]:
    import re

    return re.findall(r"\{(\w+)\}", t)


def test_the_card_over_rpc(tmp_path: Path) -> None:
    from test_rpc import LAPTOP, Output, call
    from test_rpc import keys as store_keys

    from flight_insights.rpc import build_server

    fixture = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
    out = Output()
    server = build_server(out, PACK)
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path),
         keys=store_keys())  # fmt: skip
    pl = call(server, out, 2, "platform.create", name="Quad", vehicle="copter")["result"]
    af = call(server, out, 3, "airframe.create", platform_id=pl["id"], name="Q-1")["result"]
    empty = call(server, out, 4, "airframe.recovery_card", airframe_id=af["id"])
    assert empty["error"]["code"] == -32003 and "parameters" in empty["error"]["message"]
    flights: list[str] = []
    for i, name in enumerate(("copter-quad-nognss-drill", "copter-quad-nognss-flow")):
        scanned = call(server, out, 10 + i, "import.scan",
                       paths=[str(fixture / f"{name}.bin")])["result"]  # fmt: skip
        [done] = call(server, out, 20 + i, "import.commit", batch=scanned["batch"],
                      decisions=[{"log": "f1", "airframe_id": af["id"]}])["result"]  # fmt: skip
        call(server, out, 30 + i, "analysis.run", flight_id=done["flights"][0])
        flights.append(done["flights"][0])
    c = call(server, out, 8, "airframe.recovery_card", airframe_id=af["id"])["result"]
    assert c["airframe"] == "Q-1" and c["flight"] == flights[1]  # the latest parameters
    assert [s["id"] for s in c["sections"]] == ["gnss_lost", "spoofing"]
    assert c["text"]["en"].startswith("Q-1: recovery card")
    # the drill with no other source on its own; the flow drill on source set 2
    alone, on_set = c["measured"]["alone"], c["measured"]["on_set"]
    assert alone["outages"] == 1 and 5 <= alone["hold_s"] <= 9 and 3 <= alone["detection_s"] <= 5
    assert on_set["outages"] == 1 and on_set["hold_s"] >= 30 and on_set["drift_m_per_min"] < 1
    assert keys(c, "gnss_lost") == ["step.switch_set", "step.fly_home_set", "step.land"]
    assert keys(c, "spoofing")[1] == "spoof.reject_switch"  # RC7, the GPS-disable switch
    bad = call(server, out, 9, "airframe.recovery_card", airframe_id="nope")
    assert bad["error"]["code"] == -32003
    call(server, out, 10, "engine.shutdown")
