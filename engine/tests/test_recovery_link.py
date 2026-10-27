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
    Return,
    State,
    act,
    both_lost,
    dead_reckoned_return,
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
        "hold where it is with no one flying it: its failsafe settings let it, flown in Loiter "
        "or PosHold",
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
        "hold where it is with no one flying it: its failsafe settings let it, flown in Loiter "
        "or PosHold",
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
    assert lines[1] == (
        "hold where it is with no one flying it: no failsafe acts, flown in Loiter or PosHold"
    )
    assert lines[3] == "carry on with its last command (Guided): no failsafe acts, flown in Guided"
    assert lines[-1].endswith("if GNSS is lost too, by its mode and which is lost first")
    assert "step.link_no_failsafe" in keys(c, "link_lost")


def test_a_pilots_radio_and_the_telemetry_are_lost_apart(reg: Registry) -> None:
    c = card(reg, FS_THR_ENABLE=1.0, FS_THR_VALUE=950.0, FS_GCS_ENABLE=1.0, FS_OPTIONS=1.0)
    assert [s["id"] for s in c["sections"]][2:] == ["radio_lost", "telemetry_lost"]
    assert will(c, "radio_lost") == [
        "fly home and land (RTL) after 1 s (radio failsafe), flown in Loiter, PosHold, AltHold "
        "or Guided",
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
        "carry on under the pilot's control: no failsafe acts, flown in Loiter, PosHold or AltHold"
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
        "Logged radio failsafes: 2 (2 as configured, 0 on a fallback mode, 0 not as configured).",
        "The slowest radio failsafe acted 1.2 s after the loss began (its wait: 1 s).",
        "Losses of the radio link no failsafe answered: 1.",
        "Logged ground-station failsafes: 2 (1 as configured, 1 on a fallback mode, "
        "0 not as configured).",
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


def alerts(c: dict[str, Any]) -> dict[str, bool]:
    return {s["id"]: s["alert"] for s in c["sections"]}


def test_a_link_that_leaves_it_with_no_one_flying_it_is_an_alert(reg: Registry) -> None:
    """Checked in SITL (tools/sim/check_card.py): flown from the ground station with the
    radio failsafe off and FS_OPTIONS at its default, losing the link in Loiter or
    AltHold changes nothing. The printed card marks that section."""
    safe = card(reg, **STATION, FS_THR_ENABLE=1.0, FS_GCS_ENABLE=1.0)
    assert alerts(safe) == {"gnss_lost": False, "spoofing": False, "link_lost": False}
    left = card(reg, **STATION, FS_THR_ENABLE=0.0, FS_GCS_ENABLE=1.0)
    assert alerts(left)["link_lost"] is True
    assert any("with no one flying it" in w for w in will(left, "link_lost"))


def test_a_pilots_radio_carries_on_when_only_the_telemetry_goes(reg: Registry) -> None:
    c = card(reg, FS_THR_ENABLE=1.0, FS_GCS_ENABLE=0.0)
    assert alerts(c)["telemetry_lost"] is False  # the pilot still flies it
    assert alerts(c)["radio_lost"] is False  # RTL, and landing in place if GNSS goes too


def test_a_loss_no_failsafe_answered_is_an_alert_whatever_the_settings(reg: Registry) -> None:
    unanswered = LinkMeasured(gcs=Kind({"as_configured": 0}, 1, None, 5.0))
    c = card(reg, unanswered, **STATION, FS_THR_ENABLE=1.0, FS_GCS_ENABLE=1.0)
    assert alerts(c)["link_lost"] is True


def test_gnss_lost_with_the_ekf_failsafe_off_is_an_alert(reg: Registry) -> None:
    c = card(reg, **STATION, FS_THR_ENABLE=1.0, FS_GCS_ENABLE=1.0, FS_EKF_ACTION=0.0)
    assert alerts(c)["gnss_lost"] is True


def test_the_goal_is_met_only_when_it_flies_home_with_both_lost(reg: Registry) -> None:
    """Getting home with the pilot's control and GNSS both lost, from every mode and
    whichever goes first: what the Navigation view shows as the fleet goal."""
    stock = card(reg, FS_THR_ENABLE=1.0)
    assert stock["goal"]["section"] == "radio_lost"
    assert stock["goal"]["met"] is False  # RTL needs a position: it lands where it is
    assert [p["key"] for p in stock["goal"]["will"]] == ["link.any_mode", "link.gnss_too"]
    assert all("goal" not in s for s in stock["sections"])
    flow_first = {k: v for k, v in FLOW.items() if not k.startswith("EK3_SRC2")}
    first = {**flow_first, "EK3_SRC1_POSXY": 0.0, "EK3_SRC1_VELXY": 5.0}
    home = card(reg, **first, FS_THR_ENABLE=1.0)
    assert home["goal"]["met"] is True
    assert home["goal"]["will"][-1]["key"] == "link.gnss_keeps"
    assert card(reg, **first, FS_THR_ENABLE=0.0)["goal"]["met"] is False  # nothing acts
    assert card(reg, **first, FS_THR_ENABLE=3.0)["goal"]["met"] is False  # it lands
    station = card(reg, **STATION, FS_THR_ENABLE=1.0)
    assert station["goal"]["section"] == "link_lost" and station["goal"]["met"] is False


# --- home on dead reckoning ---------------------------------------------------------------

STARTS_ALL = ("LOITER", "POSHOLD", "ALT_HOLD", "GUIDED", "AUTO")
FLOWN = (500.0, 1200.0)  # 500 m from where it armed, 20 minutes at most


def dr_card(reg: Registry, flown: tuple[float | None, float | None] = FLOWN, /,
            **params: float) -> dict[str, Any]:  # fmt: skip
    p = STOCK | DRAG | {"FS_THR_ENABLE": 1.0} | params
    readiness = NO_GNSS_READINESS.run(hover_flight({}, p), reg)
    return build(readiness, SITL, NOTHING, p, DOC, 10.0, "HL1", "a3f9c2d1", None,
                 failsafes=reg.rules["failsafes"], flown=flown)  # fmt: skip


def test_the_way_home_on_dead_reckoning() -> None:
    """The climb to RTL_ALT_M at WP_SPD_UP, then home at RTL_SPEED_MS, or WP_SPD where that
    is 0; the firmware's defaults where the parameters do not hold them."""
    defaults = DOC["link"]["defaults"]
    assert dead_reckoned_return({}, defaults, 500.0, 1200.0) == Return(15 / 2.5 + 50.0, 1200.0)
    faster = dead_reckoned_return({"RTL_SPEED_MS": 5.0}, defaults, 500.0, 1200.0)
    assert faster is not None and faster.trip_s == 6.0 + 100.0
    assert dead_reckoned_return({}, defaults, None, 1200.0) is None  # never measured


def test_dead_reckoning_gets_it_home_when_the_timeout_outlasts_the_way_home(
    reg: Registry,
) -> None:
    """FS_DR_TIMEOUT 1800 (the link's recovery first): from every mode, whichever goes
    first, it ends in RTL on a dead-reckoned position. Flown in AltHold with GNSS lost
    first, nothing sends it home until the link goes, any time in the flight: dead
    reckoning must last the longest flight and then the trip."""
    rules = reg.rules["failsafes"]
    p = {**DOC["link"]["defaults"], **STOCK, **DRAG, "FS_DR_ENABLE": 2.0, "FS_THR_ENABLE": 1.0}
    back = Return(56.0, 1200.0)
    for start in STARTS_ALL:
        ends = both_lost(rules, p | {"FS_DR_TIMEOUT": 1800.0}, State(start), True, False,
                         True, True, back)  # fmt: skip
        assert {(e.mode, e.position, e.dead_reckoning) for e in ends} == {("RTL", True, True)}, (
            start
        )
    # 1200 s outlasts the trip but not a whole flight and the trip: AltHold lands
    short = both_lost(rules, p | {"FS_DR_TIMEOUT": 1200.0}, State("ALT_HOLD"), True, False,
                      True, True, back)  # fmt: skip
    assert ("LAND", False) in {(e.mode, e.position) for e in short}
    loiter = both_lost(rules, p | {"FS_DR_TIMEOUT": 1200.0}, State("LOITER"), True, False,
                       True, True, back)  # fmt: skip
    assert {e.mode for e in loiter} == {"RTL"}  # sent home at once: the trip is enough
    # without the way home measured, the timeout ends it as before
    unknown = both_lost(rules, p | {"FS_DR_TIMEOUT": 1800.0}, State("LOITER"), True, False,
                        True, True)  # fmt: skip
    assert {(e.mode, e.position) for e in unknown} == {("LAND", False)}


def test_the_goal_met_on_dead_reckoning_says_so_and_warns(reg: Registry) -> None:
    c = dr_card(reg, FS_DR_TIMEOUT=1800.0)
    assert c["goal"]["met"] is True and c["goal"]["by"] == "dead_reckoning"
    assert c["goal"]["trip_s"] == 56
    assert will(c, "radio_lost")[-1] == (
        "fly home on dead reckoning and land (RTL), its position drifting if GNSS is lost too"
    )
    steps = text(c, "radio_lost")
    assert steps[-1].startswith("After a return home on dead reckoning it may land up to")
    [gnss] = [s for s in c["sections"] if s["id"] == "gnss_lost"]
    assert gnss["will"][0]["key"] == "will.dead_reckon_home"
    assert "about 56 s from the farthest it has flown" in gnss["will"][0]["text"]["en"]
    assert gnss["steps"][-1]["key"] == "step.dead_reckoned_home"
    for lang in ("en", "uk"):
        assert "{" not in card_text(c, lang)


def test_dead_reckoning_short_of_the_way_home_or_unmeasured(reg: Registry) -> None:
    short = dr_card(reg, FS_DR_TIMEOUT=30.0)
    assert short["goal"]["met"] is False and short["goal"]["by"] is None
    [gnss] = [s for s in short["sections"] if s["id"] == "gnss_lost"]
    assert gnss["will"][0]["key"] == "will.dead_reckon_short"
    assert all(p["key"] != "step.dead_reckoned_home" for s in short["sections"]
               for p in s["steps"])  # fmt: skip
    unmeasured = dr_card(reg, (None, None), FS_DR_TIMEOUT=1800.0)
    assert unmeasured["goal"]["met"] is False and unmeasured["goal"]["trip_s"] is None
    [gnss] = [s for s in unmeasured["sections"] if s["id"] == "gnss_lost"]
    assert gnss["will"][0]["key"] == "will.dead_reckon"


def test_a_trusted_position_home_is_not_dead_reckoning(reg: Registry) -> None:
    flow_first = {k: v for k, v in FLOW.items() if not k.startswith("EK3_SRC2")}
    c = dr_card(reg, **flow_first, EK3_SRC1_POSXY=0.0, EK3_SRC1_VELXY=5.0,
                FS_DR_TIMEOUT=1800.0)  # fmt: skip
    assert c["goal"]["met"] is True and c["goal"]["by"] == "position"
