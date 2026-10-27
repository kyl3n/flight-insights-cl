"""What would get the aircraft home without GNSS, ranked (spec: Flying without GNSS,
"Recommendations and recovery"): the catalogue cites a source for every claim, each
ranking rule on synthetic setups and measurements, and the SITL GNSS-off drills with no
other source and on optical flow, as the Navigation view and the plan see them."""

from __future__ import annotations

import copy
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from flight_builder import hover_flight
from test_no_gnss_readiness import DRAG, FLOW, STOCK
from test_recovery import NOTHING, SITL

from flight_insights import mitigations
from flight_insights.analyzers.no_gnss_readiness import NO_GNSS_READINESS
from flight_insights.analyzers.registry import Registry
from flight_insights.knowledge import KnowledgeError, load_pack
from flight_insights.mitigations import Outage, Setup
from flight_insights.recovery import build
from flight_insights.recovery_link import goal_met

PACK = load_pack()
FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
CARD = PACK.json("recovery/copter.json")
DEFAULTS = CARD["link"]["defaults"]
FLOW_FIRST = {"EK3_SRC1_VELXY": 5.0, "FLOW_TYPE": 6.0, "RNGFND1_TYPE": 8.0,
              "RNGFND1_ORIENT": 25.0}  # fmt: skip
VIO_SET_3 = {"EK3_SRC3_POSXY": 6.0, "EK3_SRC3_VELXY": 6.0, "VISO_TYPE": 2.0}


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(PACK)


@pytest.fixture(scope="module")
def doc(reg: Registry) -> dict[str, Any]:
    return mitigations.load(PACK, reg)


def setup_of(reg: Registry, answers: dict[str, Any] | None = None, /, **params: float) -> Setup:
    """A setup as the analysis judges it: the readiness review of STOCK and `params`."""
    p = STOCK | params
    r = NO_GNSS_READINESS.run(hover_flight({}, p, answers=answers), reg)
    areas = {e.id.split(":")[-1]: dict(e.values) for e in r.evidence}
    findings = [
        {"id": f"f{n}", "code": f.code, "severity": f.severity, "confidence": f.confidence,
         "params": dict(f.params), "evidence": [e.id for e in f.evidence]}
        for n, f in enumerate(r.findings)
    ]  # fmt: skip
    return Setup(areas, findings, p, answers or {})


def ranked(
    reg: Registry, doc: dict[str, Any], s: Setup, outages: list[Outage] | None = None,
    airframe: str = "A",
) -> dict[str, Any]:  # fmt: skip
    goal = mitigations.goal_check(reg.rules["failsafes"], DEFAULTS)
    return mitigations.rank(
        doc, s, reg.rules["no_gnss"]["readiness"], goal, outages or [], airframe
    )


def ids(result: dict[str, Any]) -> list[str]:
    return [m["id"] for m in result["items"]]


def item(result: dict[str, Any], mid: str) -> dict[str, Any]:
    [found] = [m for m in result["items"] if m["id"] == mid]
    return found


def outage(airframe: str = "A", sensors: tuple[str, ...] = (), kept: float = 6.9,
           drag: bool | None = False, rate: float | None = None,
           throughout: bool = False) -> Outage:  # fmt: skip
    return Outage(airframe, f"{airframe}-flight", frozenset(sensors), drag, True, kept,
                  throughout, rate)  # fmt: skip


# --- the catalogue ---------------------------------------------------------------------


WIKI = re.compile(r"https://ardupilot\.org/(copter|dev)/docs/([\w-]+)\.html")
SOURCE = re.compile(
    r"https://github\.com/ArduPilot/ardupilot/blob/Copter-4\.7\.0/(\S+?)(#L\d+(-L\d+)?)?$"
)


def test_every_claim_cites_a_wiki_page_or_the_firmware(doc: dict[str, Any]) -> None:
    assert doc["firmware"]["tag"] == "Copter-4.7.0"
    for sid, s in doc["sources"].items():
        wiki, code = WIKI.fullmatch(s["url"]), SOURCE.fullmatch(s["url"])
        assert wiki or code, sid
        if wiki:  # the page's own .rst, under the vehicle's or the common folder
            assert s["path"].endswith(f"/source/docs/{wiki.group(2)}.rst"), sid
            assert s["path"].split("/")[0] in ("common", "copter", "dev"), sid
        else:
            assert s["lines"] and code is not None, sid
    used = {
        src for m in doc["mitigations"]
        for part in (m["for_goal"], *m["needs"], m["expected"], *m["limits"])
        for src in part["sources"]
    } | set(doc["drill"]["sources"])  # fmt: skip
    assert used <= set(doc["sources"])
    assert set(doc["sources"]) - used <= {s for m in doc["mitigations"]
                                          for s in m["effect_sources"]}  # fmt: skip
    covered = {m["id"] for m in doc["mitigations"]}
    assert {"drag_fusion", "optical_flow", "visual_odometry", "slam", "second_gnss",
            "compass"} <= covered  # fmt: skip
    assert {m["id"]: m["grade"] for m in doc["mitigations"]}["slam"] == "research"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("mitigations", 0, "limits", 0, "sources"), []),  # a claim with no source
        (("mitigations", 1, "needs", 0, "sources"), ["no_such_page"]),
        (("mitigations", 0, "kind"), "magic"),
        (("mitigations", 0, "expected", "level"), "everything"),
        (("mitigations", 0, "closes", "findings"), [{"code": "no.such"}]),
        (("mitigations", 0, "effect_sources"), []),  # an effect on the goal needs a source
        (("sources", "t265", "url"), "http://example.com/t265"),
        (("drill", "procedure"), "no.such"),
    ],
)
def test_a_catalogue_that_cites_nothing_or_names_the_unknown_is_refused(
    reg: Registry, doc: dict[str, Any], path: tuple[str | int, ...], value: Any
) -> None:
    bad = copy.deepcopy(doc)
    target: Any = bad
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(KnowledgeError):
        mitigations.validate(bad, reg)


def test_the_goal_check_is_the_cards(reg: Registry) -> None:
    """goal_met plays what the recovery card plays for its goal."""
    for params in ({}, {"FS_THR_ENABLE": 0.0}, FLOW_FIRST, FLOW_FIRST | {"RC_OPTIONS": 1.0},
                   FLOW_FIRST | {"RC_OPTIONS": 1.0, "FS_GCS_ENABLE": 1.0}, DRAG):  # fmt: skip
        p = STOCK | params
        readiness = NO_GNSS_READINESS.run(hover_flight({}, p), reg)
        card = build(readiness, SITL, NOTHING, p, CARD, 10.0, "HL1", None,
                     failsafes=reg.rules["failsafes"])  # fmt: skip
        areas = {e.id.split(":")[-1]: e.values for e in readiness.evidence}
        keeps = 1 in areas["position"]["usable_sets"]
        dead = bool(areas["wind"]["drag_fusion"])
        assert goal_met(reg.rules["failsafes"], DEFAULTS, p, keeps, dead) == card["goal"]["met"]


# --- rule 1: what makes the goal reachable comes first ------------------------------------


def test_a_position_source_in_set_1_makes_the_goal_reachable(
    reg: Registry, doc: dict[str, Any]
) -> None:
    r = ranked(reg, doc, setup_of(reg))
    assert r["goal_now"] is False
    reaching = [m["id"] for m in r["items"] if m["makes_goal"]]
    assert reaching == ["optical_flow", "visual_odometry", "slam"]
    assert ids(r)[: len(reaching)] == reaching  # before everything that does not
    assert {m["rule"] for m in r["items"][: len(reaching)]} == {"goal"}
    # dead reckoning buys time, but with no way home measured the EKF failsafe still lands
    # it: not the goal
    assert item(r, "drag_fusion")["makes_goal"] is False


def test_drag_fusion_reaches_the_goal_when_the_link_comes_first_and_the_way_is_known(
    reg: Registry, doc: dict[str, Any]
) -> None:
    """With winning the link back first, the audit proposes FS_DR_TIMEOUT 1800 s; with
    that, dead reckoning lasts the airframe's longest flight and the trip home, so drag
    fusion makes the goal reachable. Not without the way home measured, nor at 30 s."""
    flown = (500.0, 1200.0)
    link_first = setup_of(reg, {"link_loss": "win_link_back"})

    def reach(s: Setup, f: tuple[float | None, float | None]) -> bool:
        goal = mitigations.goal_check(reg.rules["failsafes"], DEFAULTS, f)
        r = mitigations.rank(doc, s, reg.rules["no_gnss"]["readiness"], goal, [], "A")
        return bool(item(r, "drag_fusion")["makes_goal"])

    assert reach(link_first, flown) is True
    assert reach(link_first, (None, None)) is False  # how far it flies is not known
    assert reach(setup_of(reg), flown) is False  # 30 s: it lands short of home
    [effect] = [m["effect"] for m in doc["mitigations"] if m["id"] == "drag_fusion"]
    assert (
        effect["params_link_first"]["FS_DR_TIMEOUT"]
        == (reg.rules["audit"]["dr_timeout"]["win_link_back_s"])
    )  # one value: the audit proposes what the ranking assumes


def test_nothing_makes_the_goal_while_the_link_failsafes_would_not_bring_it_home(
    reg: Registry, doc: dict[str, Any]
) -> None:
    r = ranked(reg, doc, setup_of(reg, FS_THR_ENABLE=0.0))
    assert r["goal_now"] is False and not any(m["makes_goal"] for m in r["items"])
    assert ids(r)[0] == "optical_flow"  # by its expected level, then the cheapest work


def test_a_goal_already_met_leaves_only_what_else_helps(reg: Registry, doc: dict[str, Any]) -> None:
    r = ranked(reg, doc, setup_of(reg, **FLOW_FIRST))
    assert r["goal_now"] is True
    assert "optical_flow" not in ids(r)  # in place: set 1 keeps a position
    assert not any(m["makes_goal"] for m in r["items"])


# --- rule 2: measured benefit, else the expected one -------------------------------------


def test_a_measured_gain_ranks_above_an_expected_benefit(
    reg: Registry, doc: dict[str, Any]
) -> None:
    fleet = [outage(kept=6.9), outage(sensors=("external_nav",), kept=40.0, throughout=True)]
    r = ranked(reg, doc, setup_of(reg), fleet)
    vio = item(r, "visual_odometry")
    assert vio["basis"] == "measured" and vio["rule"] == "goal"
    assert vio["measured"]["scope"] == "airframe"
    assert (vio["measured"]["with"]["hold_s"], vio["measured"]["without"]["hold_s"]) == (40.0, 6.9)
    # measured beats the cheaper but only expected optical flow, inside the goal group
    assert ids(r)[:3] == ["visual_odometry", "optical_flow", "slam"]
    flow = item(r, "optical_flow")
    assert flow["basis"] == "expected" and flow["measured"]["with"] is None
    assert flow["measured"]["without"]["hold_s"] == 6.9  # the side that was measured


def test_larger_gains_first_and_equal_gains_by_the_lower_drift(
    reg: Registry, doc: dict[str, Any]
) -> None:
    fleet = [outage(kept=5.0), outage(sensors=("optical_flow",), kept=30.0, rate=0.4),
             outage(sensors=("external_nav",), kept=45.0, rate=2.0)]  # fmt: skip
    assert ids(ranked(reg, doc, setup_of(reg), fleet))[:2] == ["visual_odometry", "optical_flow"]
    fleet[2] = outage(sensors=("external_nav",), kept=30.0, rate=2.0)  # the same gain
    assert ids(ranked(reg, doc, setup_of(reg), fleet))[:2] == ["optical_flow", "visual_odometry"]


def test_measured_on_the_platform_when_this_airframe_flew_one_side_only(
    reg: Registry, doc: dict[str, Any]
) -> None:
    fleet = [outage("A", kept=6.9), outage("B", ("optical_flow",), kept=30.2, throughout=True)]
    flow = item(ranked(reg, doc, setup_of(reg), fleet, "A"), "optical_flow")
    assert flow["basis"] == "measured" and flow["measured"]["scope"] == "platform"
    assert flow["measured"]["with"]["throughout"] is True
    # an airframe that flew both sides itself is measured on its own
    fleet += [outage("A", ("optical_flow",), kept=20.0)]
    flow = item(ranked(reg, doc, setup_of(reg), fleet, "A"), "optical_flow")
    assert flow["measured"]["scope"] == "airframe" and flow["measured"]["with"]["hold_s"] == 20.0


def test_drag_fusion_is_measured_on_gnss_alone_with_it_on_and_off(
    reg: Registry, doc: dict[str, Any]
) -> None:
    fleet = [outage(kept=6.9, drag=False), outage(kept=19.0, drag=True),
             outage(sensors=("optical_flow",), kept=30.0, drag=True)]  # fmt: skip
    drag = item(ranked(reg, doc, setup_of(reg), fleet), "drag_fusion")
    assert drag["basis"] == "measured" and drag["rule"] == "measured"
    with_, without = drag["measured"]["with"], drag["measured"]["without"]
    assert (with_["hold_s"], without["hold_s"]) == (19.0, 6.9)


def test_expected_benefits_rank_by_the_catalogues_levels(
    reg: Registry, doc: dict[str, Any]
) -> None:
    r = ranked(reg, doc, setup_of(reg, FS_THR_ENABLE=0.0))
    levels = [doc["levels"].index(m["expected"]["level"]) for m in r["items"]]
    assert levels == sorted(levels)
    assert ids(r).index("drag_fusion") < ids(r).index("compass") < ids(r).index("second_gnss")


def test_a_measured_gain_of_nothing_ranks_after_the_expected_ones(
    reg: Registry, doc: dict[str, Any]
) -> None:
    fleet = [outage(kept=7.0, drag=False), outage(kept=6.0, drag=True)]
    r = ranked(reg, doc, setup_of(reg), fleet)
    drag = item(r, "drag_fusion")
    assert drag["rule"] == "measured_no_gain" and drag["basis"] == "measured"
    assert ids(r)[-1] == "drag_fusion"


# --- rule 3: the cheapest work left ------------------------------------------------------


def test_equal_benefits_go_cheapest_first(reg: Registry, doc: dict[str, Any]) -> None:
    r = ranked(reg, doc, setup_of(reg))
    # all three keep a position, none measured: hardware, a companion computer, research
    assert [item(r, i)["left"] for i in ids(r)[:3]] == ["hardware", "companion_computer",
                                                         "research"]  # fmt: skip
    # both make a loss less likely: a setting before a receiver
    assert ids(r).index("jamming_expected") < ids(r).index("second_gnss")


def test_the_work_left_is_what_is_ranked(reg: Registry, doc: dict[str, Any]) -> None:
    """A fitted second receiver needs only its settings; one that is not, hardware."""
    fitted = ranked(reg, doc, setup_of(reg, {"gnss_count": 2}, GPS2_TYPE=0.0))
    assert item(fitted, "second_gnss")["left"] == "settings"
    assert item(ranked(reg, doc, setup_of(reg)), "second_gnss")["left"] == "hardware"


# --- in place, and what each closes -----------------------------------------------------


def test_what_is_in_place_is_left_out(reg: Registry, doc: dict[str, Any]) -> None:
    on = ranked(reg, doc, setup_of(reg, **DRAG, GPS2_TYPE=1.0, GPS_AUTO_SWITCH=1.0,
                                   EK3_OPTIONS=1.0, COMPASS_MOTCT=2.0, FLTMODE2=2.0))  # fmt: skip
    assert not {"drag_fusion", "second_gnss", "jamming_expected", "compass",
                "pilot_fallback"} & set(ids(on))  # fmt: skip
    # drag fusion with the dead-reckoning failsafe off: only that setting is left
    partial = item(ranked(reg, doc, setup_of(reg, **DRAG, FS_DR_ENABLE=0.0)), "drag_fusion")
    assert (partial["status"], partial["left"]) == ("partial", "settings")
    # a helicopter has no drag fusion: nothing to offer
    heli = ranked(reg, doc, setup_of(reg, FRAME_CLASS=6.0))
    assert "drag_fusion" not in ids(heli)


def test_a_source_on_a_switch_is_offered_for_set_1_only_when_that_reaches_the_goal(
    reg: Registry, doc: dict[str, Any]
) -> None:
    r = ranked(reg, doc, setup_of(reg, **FLOW, RC8_OPTION=90.0))
    flow = item(r, "optical_flow")
    assert (flow["status"], flow["left"], flow["on_sets"]) == ("on_switch", "settings", [2])
    assert flow["makes_goal"] and ids(r)[0] == "optical_flow"
    assert flow["closes"] == {"areas": [], "findings": []}
    off = ranked(reg, doc, setup_of(reg, **FLOW, RC8_OPTION=90.0, FS_THR_ENABLE=0.0))
    assert "optical_flow" not in ids(off)  # in set 1 it would change nothing more


def test_each_mitigation_closes_its_own_gaps_and_findings(
    reg: Registry, doc: dict[str, Any]
) -> None:
    # flow named in set 2 with no rangefinder, vision in set 3, and nothing to select them
    s = setup_of(reg, EK3_SRC2_VELXY=5.0, FLOW_TYPE=6.0, **VIO_SET_3)
    s = replace(s, areas=dict(s.areas))
    r = ranked(reg, doc, s)
    codes = {m["id"]: m["closes"]["findings"] for m in r["items"]}
    by_id = {f["id"]: f for f in s.findings}
    flow = [by_id[i]["code"] for i in codes["optical_flow"]]
    vio = [by_id[i]["code"] for i in codes["visual_odometry"]]
    assert flow == ["no_gnss.source_not_fitted"]
    assert by_id[codes["optical_flow"][0]]["params"]["missing"].startswith("RNGFND")
    # the switch finding names the usable set, vision's: VISO_TYPE is set
    assert vio == ["no_gnss.no_source_switch"]
    # set 3 keeps a position, so of the areas only switching has a gap
    assert [a["area"] for a in item(r, "optical_flow")["closes"]["areas"]] == ["switching"]
    assert [by_id[i]["code"] for i in codes["drag_fusion"]] == ["no_gnss.drag_fusion_off"]
    heading = ranked(reg, doc, setup_of(reg, EK3_SRC1_YAW=2.0))
    compass = item(heading, "compass")
    assert compass["left"] == "settings" and compass["closes"]["areas"] == [
        {"area": "heading", "status": "gap"}
    ]


def test_outages_are_classed_by_the_source_set_they_were_flown_on(reg: Registry) -> None:
    rules = reg.rules["no_gnss"]["readiness"]
    position = {f"EK3_SRC{n}_{f}": 0.0 for n in (2, 3) for f in ("POSXY", "VELXY")}
    position |= {"EK3_SRC1_POSXY": 3.0, "EK3_SRC1_VELXY": 3.0, "EK3_SRC2_VELXY": 5.0}
    evidence = {
        "no_gnss:readiness:position": position,
        "no_gnss:readiness:wind": {"drag_fusion": False},
        "no_gnss:outage:1": {"kept_s": 30.2, "kept_relative": True, "source_set": 2, "drill": True},
        "no_gnss:outage:1:drift": {"drift_m": 0.1, "drift_m_per_min": 0.2},
        "no_gnss:outage:2": {"kept_s": 6.9, "source_set": None, "drill": False},
    }
    first, second = mitigations.outages_of(evidence, rules, "A", "F")
    assert (first.sensors, first.throughout, first.rate) == (frozenset({"optical_flow"}), True, 0.2)
    assert (second.sensors, second.throughout, second.drag) == (frozenset(), False, False)
    del evidence["no_gnss:readiness:position"]  # the readiness review was skipped
    assert mitigations.outages_of(evidence, rules, "A", "F") == []


# --- the SITL drills ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def drills(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The drill with no other source on Q-01 and the drill on optical flow on Q-02, one
    platform; Q-03 flew the jammed flight and no drill. Each flight's Navigation view
    and plan."""
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, PACK)
    n = iter(range(1, 1000))

    def c(method: str, **params: Any) -> Any:
        reply = call(server, out, next(n), method, **params)
        assert "result" in reply, reply
        return reply["result"]

    data = tmp_path_factory.mktemp("mitigations")
    c("engine.unlock", laptop_id=LAPTOP, data_dir=str(data), keys=keys())
    platform = c("platform.create", name="Quad", vehicle="copter")["id"]
    flights: dict[str, str] = {}
    for log, frame in (("copter-quad-nognss-drill", "Q-01"), ("copter-quad-nognss-flow", "Q-02"),
                       ("copter-quad-gnss-jam", "Q-03")):  # fmt: skip
        af = c("airframe.create", platform_id=platform, name=frame)["id"]
        batch = c("import.scan", paths=[str(FIXTURES / f"{log}.bin")])["batch"]
        [done] = c("import.commit", batch=batch, decisions=[{"log": "f1", "airframe_id": af}])
        flights[log] = done["flights"][0]
        c("analysis.run", flight_id=flights[log])
    return {
        log: {"view": c("flight.navigation", flight_id=fid),
              "plan": c("recommend.plan", flight_id=fid), "flight": fid}
        for log, fid in flights.items()
    }  # fmt: skip


def test_the_flow_airframe_holds_longer_than_the_one_with_no_other_source(
    drills: dict[str, Any],
) -> None:
    alone = drills["copter-quad-nognss-drill"]["view"]["mitigations"]
    flow = drills["copter-quad-nognss-flow"]["view"]["mitigations"]
    measured = item(flow, "optical_flow")["measured"]
    with_flow, without = measured["with"], measured["without"]
    assert measured["scope"] == "platform"  # Q-02 flew flow, Q-01 and Q-03 GNSS alone
    assert with_flow["hold_s"] >= 30 and with_flow["throughout"] is True
    assert without["hold_s"] < 10 and without["throughout"] is False
    assert with_flow["hold_s"] > without["hold_s"]
    assert with_flow["drift_m_per_min"] is not None and with_flow["drift_m_per_min"] < 1
    # the same numbers rank optical flow for the airframe with no other source
    assert item(alone, "optical_flow")["measured"] == measured


def test_the_airframe_with_no_other_source_gets_what_reaches_the_goal_first(
    drills: dict[str, Any],
) -> None:
    m = drills["copter-quad-nognss-drill"]["view"]["mitigations"]
    assert m["goal_now"] is False
    goal = [x["makes_goal"] for x in m["items"]]
    assert goal[0] and goal == sorted(goal, reverse=True)  # every one that reaches it first
    first = m["items"][0]
    assert (first["id"], first["basis"], first["rule"]) == ("optical_flow", "measured", "goal")
    assert first["left"] == "hardware" and first["status"] == "missing"
    assert {a["area"] for a in first["closes"]["areas"]} == {"position", "switching"}
    assert ids(m)[1:3] == ["visual_odometry", "slam"]
    drag = item(m, "drag_fusion")
    assert [f["code"] for f in drag["closes"]["findings"]] == ["no_gnss.drag_fusion_off"]
    assert [s["kind"] for s in drag["steps"]] == ["ground_check", "fly", "abort", "verify"]
    for x in m["items"]:
        assert x["sources"] and all(s["url"].startswith("https://") for s in x["sources"])
        assert x["for_goal"]["en"] and x["for_goal"]["uk"]


def test_the_flow_airframe_is_offered_its_flow_in_set_1(drills: dict[str, Any]) -> None:
    m = drills["copter-quad-nognss-flow"]["view"]["mitigations"]
    flow = m["items"][0]
    assert (flow["id"], flow["status"], flow["left"], flow["on_sets"]) == (
        "optical_flow", "on_switch", "settings", [2]
    )  # fmt: skip
    assert flow["makes_goal"] and flow["basis"] == "measured"


def test_the_drill_is_planned_until_one_is_measured(drills: dict[str, Any]) -> None:
    def offered(log: str) -> list[str]:
        plan = drills[log]["plan"]
        return [
            r["status"]
            for r in plan["recommendations"]
            if r["param"] is None and r["sources"][0]["code"] == "followup.gnss_drill"
        ]

    for log in ("copter-quad-nognss-drill", "copter-quad-nognss-flow"):
        drill = drills[log]["view"]["mitigations"]["drill"]
        assert drill["measured"] == [drills[log]["flight"]] and drill["offered"] is False
        assert offered(log) == []
    jam = drills["copter-quad-gnss-jam"]
    drill = jam["view"]["mitigations"]["drill"]
    assert drill["measured"] == [] and drill["offered"] is True
    assert [s["kind"] for s in drill["steps"]] == ["ground_check", "fly", "abort", "verify"]
    assert any("aux function 65" in s["title"] or "65" in s["title"] for s in drill["sources"])
    assert offered("copter-quad-gnss-jam") in (["next"], ["waiting"])
    [rec] = [r for r in jam["plan"]["recommendations"]
             if r["sources"][0]["code"] == "followup.gnss_drill"]  # fmt: skip
    checks = [c for s in rec["procedure"] if s["kind"] == "verify" for c in s["checks"]]
    assert checks == [{"check": "has_metric", "metric": "no_gnss.hold_s", "required": True}]
    assert [s["kind"] for s in rec["procedure"]].count("fly") == 1  # the drill's own flight
