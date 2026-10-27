"""The recovery card's link sections: what the aircraft does when it loses its control
link, with GNSS and without, played through ArduCopter 4.7.0's own failsafes, and what its
flights measured."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from flight_builder import hover_flight
from test_no_gnss_readiness import DRAG, FLOW, STOCK
from test_recovery import NOTHING, SITL, keys, text

from flight_insights.analyzers.no_gnss_readiness import NO_GNSS_READINESS
from flight_insights.analyzers.registry import Registry
from flight_insights.knowledge import load_pack
from flight_insights.recovery import build, card_text
from flight_insights.recovery_link import (
    GCS,
    RADIO,
    Kind,
    LinkMeasured,
    State,
    act,
    both_lost,
)

PACK = load_pack()
DOC = PACK.json("recovery/copter.json")
STATION = {"RC_OPTIONS": 1.0}  # a joystick through the ground station, the receiver ignored


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(PACK)


def card(reg: Registry, link: LinkMeasured | None = None, /, **params: float) -> dict[str, Any]:
    p = STOCK | params
    readiness = NO_GNSS_READINESS.run(hover_flight({}, p), reg)
    return build(readiness, SITL, NOTHING, p, DOC, 10.0, "HL1", "a3f9c2d1", None,
                 link=link, failsafes=reg.rules["failsafes"])  # fmt: skip


def will(c: dict[str, Any], section: str) -> list[str]:
    return text(c, section, "will")


def test_a_ground_station_that_flies_it_loses_both_at_once(reg: Registry) -> None:
    c = card(reg, **STATION, FS_THR_ENABLE=1.0, FS_GCS_ENABLE=1.0)
    assert [s["id"] for s in c["sections"]] == ["gnss_lost", "spoofing", "link_lost"]
    # the joystick's overrides stop, so the radio failsafe goes first; RTL again at 5 s
    # changes nothing
    assert will(c, "link_lost") == [
        "lose the pilot's control and the telemetry together: it is flown from the ground "
        "station, over the one link",
        "fly home and land (RTL) after 1 s (radio failsafe), whatever mode it is flown in",
        "land where it is, drifting with the wind (Land) if GNSS is lost too",
    ]
    assert keys(c, "link_lost") == [
        "step.link_antennas", "step.link_back_station", "step.link_clear",
    ]  # fmt: skip
    assert keys(c, "link_lost", "facts") == ["fact.link_unmeasured"]


def test_each_failsafe_acts_on_the_mode_the_one_before_left(reg: Registry) -> None:
    """Landing after the radio failsafe, the ground-station failsafe still asks for RTL:
    only FS_OPTIONS 'continue if landing' keeps it landing (events.cpp)."""
    c = card(reg, **STATION, FS_THR_ENABLE=3.0, FS_GCS_ENABLE=1.0, RC_FS_TIMEOUT=2.0)
    assert will(c, "link_lost")[1] == (
        "land where it is (Land) after 2 s (radio failsafe), then fly home and land (RTL) "
        "after 5 s (ground-station failsafe), whatever mode it is flown in"
    )
    kept = card(reg, **STATION, FS_THR_ENABLE=3.0, FS_GCS_ENABLE=1.0, FS_OPTIONS=8.0)
    assert will(kept, "link_lost")[1] == (
        "land where it is (Land) after 1 s (radio failsafe), whatever mode it is flown in"
    )


def test_continuing_under_pilot_control_with_no_one_flying_it(reg: Registry) -> None:
    """The radio failsafe off and FS_OPTIONS 'continue in pilot modes' (bit 4): flown by
    joystick, nothing brings it back; if GNSS goes too, the EKF failsafe's AltHold (the
    radio failsafe not in force) leaves it drifting with no one flying it."""
    c = card(reg, **STATION, FS_THR_ENABLE=0.0, FS_GCS_ENABLE=1.0, FS_OPTIONS=16.0,
             FS_EKF_ACTION=2.0)  # fmt: skip
    assert will(c, "link_lost")[1:] == [
        "hold where it is with no one flying it: its failsafe settings let it, flown in Loiter",
        "hold its height with no one flying it, drifting with the wind: its failsafe settings "
        "let it, flown in AltHold",
        "fly home and land (RTL) after 5 s (ground-station failsafe), flown in Guided or Auto",
        "hold its height with no one flying it, drifting with the wind (AltHold) if GNSS is "
        "lost too",
    ]


def test_fs_options_unset_is_the_firmwares_default_continue_under_pilot_control(
    reg: Registry,
) -> None:
    """FS_OPTIONS defaults to 16 in 4.7.0, though its description says 0 (Parameters.cpp);
    the simulator checks flew it: flown by joystick, a GCS failsafe leaves Loiter and
    AltHold alone and returns from Guided and Auto."""
    p = {k: v for k, v in STOCK.items() if k != "FS_OPTIONS"}
    readiness = NO_GNSS_READINESS.run(hover_flight({}, p | STATION), reg)
    c = build(readiness, SITL, NOTHING, p | STATION | {"FS_THR_ENABLE": 0.0, "FS_GCS_ENABLE": 1.0},
              DOC, 10.0, "HL1", None, None, failsafes=reg.rules["failsafes"])  # fmt: skip
    assert will(c, "link_lost")[1:3] == [
        "hold where it is with no one flying it: its failsafe settings let it, flown in Loiter",
        "hold its height with no one flying it, drifting with the wind: its failsafe settings "
        "let it, flown in AltHold",
    ]
    assert will(c, "link_lost")[3].startswith("fly home and land (RTL) after 5 s")


def test_with_the_radio_failsafe_in_force_the_ekf_failsafe_lands_instead(reg: Registry) -> None:
    rules = reg.rules["failsafes"]
    p = {**DOC["link"]["defaults"], **STATION, "FS_EKF_ACTION": 2.0, "FS_GCS_ENABLE": 1.0}
    [end] = both_lost(rules, p, State("LOITER"), True, True, False, True)
    assert end == State("LAND", position=False, radio=True)
    alone = act(rules, p, State("LOITER", position=False), "FAILSAFE_EKFINAV")
    assert alone.mode == "ALT_HOLD"


def test_no_failsafe_on_says_so_and_what_it_does(reg: Registry) -> None:
    c = card(reg, **STATION, FS_THR_ENABLE=0.0, FS_GCS_ENABLE=0.0)
    lines = will(c, "link_lost")
    assert lines[1] == "hold where it is with no one flying it: no failsafe acts, flown in Loiter"
    assert lines[3] == "carry on with its last command (Guided): no failsafe acts, flown in Guided"
    assert lines[-1].endswith("if GNSS is lost too, by its mode and which is lost first")
    assert "step.link_no_failsafe" in keys(c, "link_lost")


def test_a_pilots_radio_and_the_telemetry_are_lost_apart(reg: Registry) -> None:
    c = card(reg, FS_THR_ENABLE=1.0, FS_THR_VALUE=950.0, FS_GCS_ENABLE=1.0, FS_OPTIONS=1.0)
    assert [s["id"] for s in c["sections"]][2:] == ["radio_lost", "telemetry_lost"]
    assert will(c, "radio_lost") == [
        "fly home and land (RTL) after 1 s (radio failsafe), flown in Loiter, AltHold or Guided",
        "carry on with the mission (Auto): its failsafe settings let it, flown in Auto",
        "act at once instead if the receiver sends a failsafe throttle under 950 (FS_THR_VALUE)",
        "not notice the loss at all if the receiver holds its last values instead: set it to stop "
        "sending when the signal is lost",
        "land where it is, drifting with the wind (Land) if GNSS is lost too",
    ]
    assert keys(c, "radio_lost")[:2] == ["step.radio_antennas", "step.link_back_switch"]
    # FS_OPTIONS bit 0 is the radio's: in Auto the ground-station failsafe still returns
    assert will(c, "telemetry_lost") == [
        "fly home and land (RTL) after 5 s (ground-station failsafe), whatever mode it is flown in",
    ]
    quiet = card(reg, FS_THR_ENABLE=1.0, FS_GCS_ENABLE=0.0)
    assert will(quiet, "telemetry_lost")[0] == (
        "carry on under the pilot's control: no failsafe acts, flown in Loiter or AltHold"
    )


def test_a_first_source_set_that_keeps_a_position_flies_home_without_gnss(
    reg: Registry,
) -> None:
    flow_first = {k: v for k, v in FLOW.items() if not k.startswith("EK3_SRC2")}
    c = card(reg, **flow_first, EK3_SRC1_POSXY=0.0, EK3_SRC1_VELXY=5.0, FS_THR_ENABLE=1.0)
    assert will(c, "radio_lost")[-1] == (
        "do the same if GNSS is lost too: its first source set keeps a position without it"
    )


def test_dead_reckoning_heads_home_before_the_ekf_failsafe_lands_it(reg: Registry) -> None:
    """Drag fusion on and FS_DR_ENABLE 2 (RTL): wherever it starts and whichever goes
    first, its position runs out and it lands drifting."""
    rules = reg.rules["failsafes"]
    p = {**DOC["link"]["defaults"], **STOCK, **DRAG, "FS_DR_ENABLE": 2.0, "FS_THR_ENABLE": 4.0}
    for start in ("LOITER", "AUTO", "ALT_HOLD"):
        ends = both_lost(rules, p, State(start), True, False, True, True)
        assert {(e.mode, e.position) for e in ends} == {("LAND", False)}, start
    assert act(rules, p, State("LOITER"), "FAILSAFE_DEADRECKON").mode == "RTL"
    assert act(rules, p, State("ALT_HOLD"), "FAILSAFE_DEADRECKON").mode == "ALT_HOLD"


def test_a_mode_that_needs_a_position_is_refused_without_one(reg: Registry) -> None:
    rules = reg.rules["failsafes"]
    p = {**DOC["link"]["defaults"], "FS_THR_ENABLE": 4.0}  # SmartRTL, then RTL, then Land
    assert act(rules, p, State("LOITER"), RADIO).mode == "SMART_RTL"
    assert act(rules, p, State("LOITER", position=False), RADIO).mode == "LAND"
    gcs = {**DOC["link"]["defaults"], "FS_GCS_ENABLE": 7.0}  # Brake, then Land
    assert act(rules, gcs, State("AUTO"), GCS).mode == "BRAKE"
    assert act(rules, gcs, State("AUTO", position=False), GCS).mode == "LAND"


def test_what_its_flights_measured(reg: Registry) -> None:
    measured = LinkMeasured(
        radio=Kind({"as_configured": 2}, unanswered=1, slowest_s=1.2, wait_s=1.0),
        gcs=Kind({"as_configured": 1, "fell_back": 1}, slowest_s=5.3, wait_s=5.0),
        gcs_unheard_s=3.8, gcs_timeout_s=5.0,
    )  # fmt: skip
    c = card(reg, measured, **STATION, FS_GCS_ENABLE=1.0)
    assert text(c, "link_lost", "facts") == [
        "Its flights logged 2 radio failsafes: 2 as configured, 0 on a fallback mode, 0 not as "
        "configured.",
        "The slowest radio failsafe acted 1.2 s after the loss began (its wait: 1 s).",
        "1 losses of the radio link went unanswered by its failsafe.",
        "Its flights logged 2 ground-station failsafes: 1 as configured, 1 on a fallback mode, "
        "0 not as configured.",
        "The slowest ground-station failsafe acted 5.3 s after the loss began (its wait: 5 s).",
        "On one flight the ground station went unheard for 3.8 s, short of its 5 s failsafe wait.",
    ]


def test_the_link_sections_in_both_languages(reg: Registry) -> None:
    c = card(reg, **STATION, FS_THR_ENABLE=3.0, FS_GCS_ENABLE=1.0, FS_OPTIONS=16.0)
    for lang in ("en", "uk"):
        printed = card_text(c, lang)
        assert "{" not in printed and "}" not in printed
    assert "Ground-station link lost" in card_text(c)
    assert "Втрата зв'язку з наземною станцією" in card_text(c, "uk")


def test_the_link_sections_from_its_own_failsafe_flights(tmp_path: Path) -> None:
    """The SITL flights that lost the ground station (FS_GCS_ENABLE 1), the pilot's radio
    (FS_THR_ENABLE 4) and the radio with its failsafe off, on one airframe: the card reads
    how each went, and plays the latest parameters."""
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
    for i, name in enumerate(("copter-quad-fs-gcs", "copter-quad-fs-radio", "copter-quad-fs-off")):
        scanned = call(server, out, 10 + i, "import.scan",
                       paths=[str(fixture / f"{name}.bin")])["result"]  # fmt: skip
        [done] = call(server, out, 20 + i, "import.commit", batch=scanned["batch"],
                      decisions=[{"log": "f1", "airframe_id": af["id"]}])["result"]  # fmt: skip
        call(server, out, 30 + i, "analysis.run", flight_id=done["flights"][0])
    c = call(server, out, 8, "airframe.recovery_card", airframe_id=af["id"])["result"]
    assert [s["id"] for s in c["sections"]] == [
        "gnss_lost", "spoofing", "radio_lost", "telemetry_lost", "motor_lost",
    ]  # fmt: skip
    facts = keys(c, "radio_lost", "facts")
    assert facts[0] == "fact.link_failsafes" and "fact.link_unanswered" in facts
    [radio] = [p for s in c["sections"] if s["id"] == "radio_lost" for p in s["facts"]
               if p["key"] == "fact.link_failsafes"]  # fmt: skip
    # SmartRTL had no path back from where the radio was lost: RTL, its fallback
    assert radio["params"]["n"] == 1 and radio["params"]["fell_back"] == 1
    assert keys(c, "telemetry_lost", "facts")[0] == "fact.link_failsafes"
    call(server, out, 9, "engine.shutdown")
