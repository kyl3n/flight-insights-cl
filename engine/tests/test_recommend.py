"""The recommendation engine: validation in the firmware's dialect, the changes never
proposed automatically, conflicts, step limits, enable dependencies, stage gating, the
parameter file, and the heavy-lift fixture end to end."""

from __future__ import annotations

import fnmatch
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from flight_insights.analyzers import Registry
from flight_insights.analyzers.registry import LANGUAGES, PLACEHOLDER, STAGES
from flight_insights.imports import Decision, commit, propose, scan
from flight_insights.knowledge import load_pack
from flight_insights.params import Dialect, parse_params
from flight_insights.platforms.records import create_platform, platform_view, save_answers
from flight_insights.platforms.schema import load_schema
from flight_insights.recommend import (
    ExportError,
    build_plan,
    export_card,
    export_revert,
    export_step,
    export_undo,
    flight_plan,
    to_json,
)
from flight_insights.recommend.plan import Plan, Recommendation
from flight_insights.recommend.procedure import procedure, session
from flight_insights.store import StoreKeys, open_store
from flight_insights.store.cache import ColumnCache

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
IDS = count(1)
BASE = {  # a set-up quad, in canonical names
    "MOT_HOVER_LEARN": 0.0, "MOT_PWM_TYPE": 0.0, "LOG_BITMASK": 176126.0,
    "INS_HNTCH_ENABLE": 1.0, "INS_HNTCH_MODE": 1.0, "INS_HNTCH_FREQ": 80.0,
    "INS_HNTCH_OPTS": 0.0, "INS_GYRO_FILTER": 20.0, "ARMING_SKIPCHK": 0.0,
    "FS_THR_ENABLE": 1.0, "ATC_RATE_R_MAX": 360.0, "ATC_RAT_RLL_P": 0.1, "FFT_ENABLE": 0.0,
}  # fmt: skip


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def finding(
    code: str,
    *changes: tuple[str, float],
    severity: str = "caution",
    confidence: str = "high",
    stage: str | None = None,
    origin: str = "measured",
) -> dict[str, Any]:
    reg_stage = Registry.load(load_pack()).findings[code].stage if stage is None else stage
    return {
        "id": f"f{next(IDS)}", "analyzer": code.split(".")[0], "code": code,
        "severity": severity, "confidence": confidence, "params": {}, "evidence": [f"ev:{code}"],
        "changes": [
            {"param": p, "current": None, "proposed": v, "stage": reg_stage} for p, v in changes
        ],
        "stage": reg_stage, "origin": origin,
    }  # fmt: skip


def plan(
    reg: Registry, *findings: dict[str, Any], firmware: str = "4.7.2", **params: float
) -> Plan:
    dialect = Dialect(reg.pack, "copter", firmware)
    return build_plan("fl", list(findings), BASE | params, dialect, reg)


def one(p: Plan, param: str) -> Recommendation:
    [rec] = [r for r in p.recommendations if r.param == param]
    return rec


def note(rec: Recommendation) -> str:
    return rec.notes[-1].reason


# --- the rules file ------------------------------------------------------------------------


def test_every_rule_names_real_parameters_and_has_both_languages(reg: Registry) -> None:
    rules = reg.rules["recommend"]
    pack = load_pack()
    names = set(pack.metadata(pack.versions[-1], "copter"))
    patterns = [p for limit in rules["step_limits"] for p in limit["names"]]
    for rule in rules["never"].values():
        patterns += rule["names"] + rule.get("zero_means_unlimited", [])
    for p in patterns:
        assert fnmatch.filter(names, p), p
    assert set(rules["verify"]) == set(STAGES) == set(rules["risk"])
    for entry in [*rules["verify"].values(), *rules["texts"].values(), *rules["effects"].values()]:
        assert all(entry[lang].strip() for lang in LANGUAGES)
        assert len({frozenset(PLACEHOLDER.findall(entry[lang])) for lang in LANGUAGES}) == 1


# --- validation against the firmware's metadata --------------------------------------------


def test_a_valid_change_is_offered_with_its_reason_and_check(reg: Registry) -> None:
    p = plan(reg, finding("option.hover_learn.available", ("MOT_HOVER_LEARN", 2), severity="info"))
    rec = one(p, "MOT_HOVER_LEARN")
    assert (rec.status, rec.current, rec.proposed, rec.kind) == ("next", 0.0, 2.0, "performance")
    assert p.stage == "setup" and [s.params for s in p.steps] == [("MOT_HOVER_LEARN",)]
    out = to_json(p, reg)["recommendations"][0]
    assert out["verify"]["en"] and out["verify"]["uk"] and out["risk"] == "medium"
    assert out["source"] == "finding:option.hover_learn.available"
    assert out["evidence"] == ["ev:option.hover_learn.available"]


@pytest.mark.parametrize(
    ("param", "value", "reason"),
    [
        ("INS_GYRO_FILTER", 300.0, "out_of_range"),  # range 0 to 256
        ("MOT_PWM_TYPE", 12.0, "not_a_value"),  # no range: the listed values are all
        ("INS_HNTCH_OPTS", float(1 << 12), "undefined_bits"),
        ("INS_HNTCH_OPTS", 2.5, "undefined_bits"),
        ("NOT_A_PARAM", 1.0, "unknown_param"),
    ],
)
def test_invalid_values_are_blocked_with_the_reason(
    reg: Registry, param: str, value: float, reason: str
) -> None:
    p = plan(reg, finding("filter.better_settings", (param, value)))
    rec = one(p, param)
    assert (rec.status, note(rec)) == ("blocked", reason)
    assert p.steps == []


def test_suggested_values_beside_a_range_are_not_the_only_values(reg: Registry) -> None:
    rec = one(
        plan(reg, finding("filter.better_settings", ("ATC_RATE_R_MAX", 270.0))), "ATC_RATE_R_MAX"
    )
    assert rec.status != "blocked"  # 270 is in range though not a listed suggestion


def test_values_are_written_in_the_firmwares_own_names(reg: Registry) -> None:
    # PSC_JERK_D became PSC_D_JERK in 4.7.1; a 4.7.0 aircraft gets its own name
    p = plan(reg, finding("filter.better_settings", ("PSC_D_JERK", 6.0)), firmware="4.7.0")
    rec = one(p, "PSC_D_JERK")
    assert (rec.native, rec.status) == ("PSC_JERK_D", "next")
    assert (
        one(plan(reg, finding("filter.better_settings", ("PSC_D_JERK", 6.0))), "PSC_D_JERK").native
        == "PSC_D_JERK"
    )


def test_a_value_already_set_is_not_proposed(reg: Registry) -> None:
    p = plan(reg, finding("option.hover_learn.available", ("MOT_HOVER_LEARN", 0)))
    assert [r.param for r in p.recommendations] == [None]  # only the finding, as an action


# --- never proposed automatically -----------------------------------------------------------


@pytest.mark.parametrize(
    ("param", "value", "reason"),
    [
        ("ARMING_SKIPCHK", 4.0, "never_add_bits"),
        ("FS_THR_ENABLE", 0.0, "never_disable"),
        ("ATC_RATE_R_MAX", 720.0, "never_raise"),
        ("ATC_RATE_R_MAX", 0.0, "never_raise"),  # 0 is unlimited
    ],
)
def test_weakening_safety_is_never_proposed(
    reg: Registry, param: str, value: float, reason: str
) -> None:
    rec = one(plan(reg, finding("system.failsafe", (param, value), severity="safety")), param)
    assert (rec.status, note(rec)) == ("blocked", reason)


def test_lowering_a_limit_or_clearing_a_skip_is_allowed(reg: Registry) -> None:
    p = plan(
        reg,
        finding("system.failsafe", ("ATC_RATE_R_MAX", 180.0), ("ARMING_SKIPCHK", 0.0)),
        ARMING_SKIPCHK=4.0,
    )
    assert one(p, "ATC_RATE_R_MAX").status == one(p, "ARMING_SKIPCHK").status == "next"


def test_a_blocked_change_leaves_its_finding_as_an_action(reg: Registry) -> None:
    p = plan(reg, finding("system.failsafe", ("FS_THR_ENABLE", 0.0), severity="out"))
    actions = [r for r in p.recommendations if r.param is None]
    assert [a.lead.code for a in actions] == ["system.failsafe"]
    assert actions[0].status == "next" and p.stage == "operations"


# --- conflicts -----------------------------------------------------------------------------


def test_bitmask_changes_to_different_bits_combine(reg: Registry) -> None:
    p = plan(
        reg,
        finding("filter.better_settings", ("INS_HNTCH_OPTS", 1.0)),
        finding("option.notch_all_imus.available", ("INS_HNTCH_OPTS", 8.0), severity="info"),
    )
    rec = one(p, "INS_HNTCH_OPTS")
    assert (rec.proposed, len(rec.sources), rec.status) == (9.0, 2, "next")


def test_stability_beats_performance_and_the_loser_is_shown(reg: Registry) -> None:
    p = plan(
        reg,
        finding("option.hover_learn.available", ("INS_GYRO_FILTER", 60.0), severity="info"),
        finding("filter.better_settings", ("INS_GYRO_FILTER", 40.0)),
    )
    rec = one(p, "INS_GYRO_FILTER")
    assert (rec.proposed, rec.status) == (40.0, "next")
    assert [a.proposed for a in rec.alternatives] == [60.0]


def test_equal_findings_that_disagree_are_disputed(reg: Registry) -> None:
    p = plan(
        reg,
        finding("filter.better_settings", ("INS_GYRO_FILTER", 40.0)),
        finding("filter.not_tracking", ("INS_GYRO_FILTER", 50.0)),
    )
    rec = one(p, "INS_GYRO_FILTER")
    assert (rec.status, note(rec)) == ("disputed", "disputed")
    assert p.steps == []  # nothing disputed goes in a file


def test_inference_against_a_measurement_is_disputed_not_overruled(reg: Registry) -> None:
    p = plan(
        reg,
        finding("filter.better_settings", ("INS_GYRO_FILTER", 40.0)),
        finding(
            "filter.better_settings", ("INS_GYRO_FILTER", 80.0), origin="ai", confidence="high"
        ),
    )
    rec = one(p, "INS_GYRO_FILTER")
    assert rec.status == "disputed" and rec.lead.origin == "measured"


# --- step limits, enables and reboots -------------------------------------------------------


def test_a_large_gain_change_is_split_across_flights(reg: Registry) -> None:
    rec = one(plan(reg, finding("filter.better_settings", ("ATC_RAT_RLL_P", 0.2))), "ATC_RAT_RLL_P")
    assert (rec.proposed, rec.target, note(rec)) == (0.125, 0.2, "split")
    assert rec.notes[-1].params["steps"] == 4


def test_a_switched_off_feature_is_enabled_first(reg: Registry) -> None:
    p = plan(reg, finding("filter.better_settings", ("INS_HNTCH_FREQ", 90.0)), INS_HNTCH_ENABLE=0.0)
    freq, enable = one(p, "INS_HNTCH_FREQ"), one(p, "INS_HNTCH_ENABLE")
    assert (enable.proposed, enable.step, freq.step) == (1.0, 1, 1)  # FREQ exists already
    assert "INS_HNTCH_ENABLE=1" in freq.requires and note(freq) == "needs_enable"


def test_parameters_that_appear_once_enabled_come_a_step_later(reg: Registry) -> None:
    p = plan(reg, finding("filter.better_settings", ("FFT_MINHZ", 60.0), ("FFT_MAXHZ", 300.0)))
    # FFT_ENABLE hides the FFT's other parameters until it is set (a refresh shows them),
    # and the FFT itself starts only after a reboot. The filter test flight's logging
    # goes in the file loaded last.
    assert [(s.params, s.after) for s in p.steps] == [
        (("FFT_ENABLE",), "refresh"),
        (("FFT_MAXHZ", "FFT_MINHZ", "INS_RAW_LOG_OPT", "LOG_BITMASK"), "reboot"),
    ]
    text = export_step(p, reg, 1, "mission planner")["text"]
    assert "FFT_ENABLE,1" in text and "FFT_MINHZ" not in text and "refresh" in text
    assert "reboot" not in text.split("FFT_ENABLE")[0]  # a refresh is enough between steps


def test_a_backend_parameter_waits_for_a_reboot_after_its_type(reg: Registry) -> None:
    # The firmware creates a battery monitor's backend at boot, so its own parameters
    # appear only after a reboot; its common ones on a refresh.
    p = plan(
        reg,
        finding(
            "params.battery_warn_only",
            ("BATT2_MONITOR", 4.0), ("BATT2_AMP_PERVLT", 36.0), ("BATT2_LOW_VOLT", 21.0),
        ),
    )  # fmt: skip
    assert [(s.params, s.after) for s in p.steps] == [
        (("BATT2_MONITOR",), "reboot_refresh"),
        (("BATT2_AMP_PERVLT", "BATT2_LOW_VOLT"), "reboot"),
    ]
    text = export_step(p, reg, 1, "mission planner")["text"]
    assert "reboot the flight controller and refresh" in text


def test_a_gate_that_names_a_device_is_left_to_a_person(reg: Registry) -> None:
    p = plan(reg, finding("params.battery_warn_only", ("BATT2_LOW_VOLT", 21.0)))
    rec = one(p, "BATT2_LOW_VOLT")
    assert rec.status == "blocked" and note(rec) == "needs_choice"
    assert not [r for r in p.recommendations if r.param == "BATT2_MONITOR"]


def test_gates_behind_gates_come_in_order(reg: Registry) -> None:
    # ARSPD_ENABLE shows ARSPD_TYPE, which shows ARSPD_PIN: three steps, refreshing between.
    p = plan(reg, finding("params.out_of_range", ("ARSPD_TYPE", 2.0), ("ARSPD_PIN", 15.0)))
    assert [(s.params, s.after) for s in p.steps] == [
        (("ARSPD_ENABLE",), "refresh"),
        (("ARSPD_TYPE",), "refresh"),
        (("ARSPD_PIN",), "fly"),
    ]


def test_a_reboot_parameter_says_so(reg: Registry) -> None:
    p = plan(reg, finding("option.dshot_output.available", ("MOT_PWM_TYPE", 6.0), severity="info"))
    rec = one(p, "MOT_PWM_TYPE")
    assert rec.reboot and "reboot" in rec.requires and p.steps[-1].after == "reboot"


def test_a_parameter_read_only_at_start_up_gets_a_reboot(reg: Registry) -> None:
    # LOG_FILE_BUFSIZE carries no reboot flag in the metadata, but the logger sizes its
    # buffer once, at start-up (rules/recommend.json boot_only)
    p = plan(reg, finding("logging.dropouts", ("LOG_FILE_BUFSIZE", 80.0)))
    rec = one(p, "LOG_FILE_BUFSIZE")
    assert rec.reboot and p.steps[-1].after == "reboot"


def test_every_boot_only_parameter_is_real_and_sourced(reg: Registry) -> None:
    meta = reg.pack.metadata("4.7.0", "copter")
    for name, source in reg.rules["recommend"]["boot_only"]["params"].items():
        assert name in meta and not meta[name].get("reboot_required") and source


def test_a_filter_test_flight_lifts_the_logging_cap(reg: Registry) -> None:
    p = plan(
        reg,
        finding("filter.better_source", ("INS_HNTCH_MODE", 3.0)),
        LOG_FILE_RATEMAX=50.0,
    )
    rec = one(p, "LOG_FILE_RATEMAX")
    assert rec.proposed == 0.0 and rec.temporary and rec.revert == 50.0


def test_changes_from_one_finding_require_each_other(reg: Registry) -> None:
    p = plan(
        reg, finding("filter.better_source", ("INS_HNTCH_MODE", 3.0), ("INS_HNTCH_FREQ", 60.0))
    )
    assert one(p, "INS_HNTCH_MODE").requires == ["INS_HNTCH_FREQ=60"]


# --- stages ---------------------------------------------------------------------------------


def test_only_the_earliest_stage_is_offered(reg: Registry) -> None:
    p = plan(
        reg,
        finding("logging.missing_messages", ("LOG_BITMASK", 176126.0 + 2**19)),
        finding("filter.better_settings", ("INS_GYRO_FILTER", 40.0)),
    )
    assert p.stage == "evidence"
    assert one(p, "LOG_BITMASK").status == "next"
    waiting = one(p, "INS_GYRO_FILTER")
    assert (waiting.status, waiting.notes[-1].params) == ("waiting", {"stage": "evidence"})


def test_a_mechanical_problem_holds_back_tuning_but_not_safety(reg: Registry) -> None:
    p = plan(
        reg,
        finding("vibration.high", severity="out"),
        finding("filter.better_settings", ("INS_GYRO_FILTER", 40.0)),
        finding("system.crash_check", ("FS_CRASH_CHECK", 1.0), severity="safety"),
        FS_CRASH_CHECK=0.0,
    )
    assert p.stage == "mechanical"
    assert one(p, "INS_GYRO_FILTER").status == "waiting"
    assert one(p, "FS_CRASH_CHECK").status == "next"  # safety surfaces at any stage
    [action] = [r for r in p.recommendations if r.param is None]
    assert action.status == "next" and action.lead.code == "vibration.high"
    assert p.recommendations[0].kind == "safety"


def test_a_better_option_never_holds_back_a_fix(reg: Registry) -> None:
    p = plan(
        reg,
        finding("option.hover_learn.available", ("MOT_HOVER_LEARN", 2), severity="info"),
        finding("filter.better_settings", ("INS_GYRO_FILTER", 40.0)),
        finding("option.notch_all_imus.available", ("FFT_ENABLE", 1.0), severity="info",
                stage="autotune"),
    )  # fmt: skip
    assert p.stage == "filters"
    assert one(p, "MOT_HOVER_LEARN").status == one(p, "INS_GYRO_FILTER").status == "next"
    assert one(p, "FFT_ENABLE").status == "waiting"  # a later stage still waits


def test_a_change_set_answers_the_findings_it_explains(reg: Registry) -> None:
    flag = finding("filter.not_tracking")
    better = finding("filter.better_settings", ("INS_HNTCH_MODE", 3.0), severity="info")
    better["params"] = {"explains": ["filter.not_tracking"]}
    p = plan(reg, flag, better)
    rec = one(p, "INS_HNTCH_MODE")
    assert rec.kind == "stability" and rec.lead.code == "filter.not_tracking"
    lasting = [r.param for r in p.recommendations if not r.temporary]
    assert lasting == ["INS_HNTCH_MODE"]  # no separate action


def test_info_findings_without_changes_are_not_actions(reg: Registry) -> None:
    assert plan(reg, finding("filter.model_mismatch", severity="info")).recommendations == []


# --- the parameter file ---------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["mission planner", "qgroundcontrol"])
def test_the_file_reads_back_as_the_changed_values_only(reg: Registry, kind: Any) -> None:
    p = plan(
        reg,
        finding("option.hover_learn.available", ("MOT_HOVER_LEARN", 2), severity="info"),
        finding(
            "option.battery_voltage_scaling.available", ("MOT_BAT_VOLT_MAX", 25.2), severity="info"
        ),
    )
    out = export_step(p, reg, 1, kind, "uk")
    parsed = parse_params(out["text"])
    assert parsed.problems == []
    assert parsed.values() == {"MOT_HOVER_LEARN": 2.0, "MOT_BAT_VOLT_MAX": 25.2}
    assert out["filename"].endswith(".param" if kind == "mission planner" else ".params")
    assert "Наступний журнал має показати:" in out["text"]  # in the chosen language
    if kind == "mission planner":
        line = next(e for e in parsed.entries if e.name == "MOT_HOVER_LEARN")
        assert line.comment  # the reason rides on the line


def test_nothing_to_export_is_an_error(reg: Registry) -> None:
    with pytest.raises(ExportError):
        export_step(plan(reg), reg, 1, "mission planner")


# --- the heavy-lift fixture end to end ------------------------------------------------------


def test_the_heavy_lift_plan_and_its_file(tmp_path: Path) -> None:
    pack = load_pack()
    schema = load_schema(pack)
    keys = StoreKeys.generate()
    with open_store(tmp_path / "fleet.db", keys, "e" * 32) as store:
        cache = ColumnCache(tmp_path / "cache", keys)
        platform = create_platform(store, schema, "HL", "copter")
        version = platform_view(store, schema, platform)["reference_build"]["version"]
        save_answers(
            store, schema, platform, version, {"payload": "variable", "esc_firmware": "BLHeli_32"}
        )
        batch = scan(store, pack, bytes(32), [FIXTURES / "copter-heavylift-loaded.bin"])
        decision = Decision("f1", new_airframe=(platform, "HL1"))
        [res] = commit(store, pack, cache, batch, propose(store, pack, batch), [decision])
        p = flight_plan(store, cache, pack, res.flights[0])
    reg = Registry.load(pack)
    out = to_json(p, reg)
    by_param = {r["param"]: r for r in out["recommendations"] if r["param"]}
    assert by_param["MOT_HOVER_LEARN"]["proposed"] == 2.0
    for r in out["recommendations"]:  # every recommendation shows why and what it rests on
        assert r["why"]["en"] and r["why"]["uk"] and r["evidence"]
    for s in p.steps:
        parsed = parse_params(export_step(p, reg, s.number, "mission planner")["text"])
        assert parsed.problems == [] and set(parsed.values()) == set(s.params)
        meta = Dialect(pack, "copter", p.firmware).meta
        assert all(name in meta for name in parsed.values())


def test_the_plan_and_its_file_over_rpc(tmp_path: Path) -> None:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, load_pack())
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys())
    platform = call(server, out, 2, "platform.create", name="Quad", vehicle="copter")["result"]
    af = call(server, out, 3, "airframe.create", platform_id=platform["id"], name="Q-1")["result"]
    scanned = call(server, out, 4, "import.scan", paths=[str(FIXTURES / "copter-quad-hover.bin")])[
        "result"
    ]
    [done] = call(
        server, out, 5, "import.commit", batch=scanned["batch"],
        decisions=[{"log": "f1", "airframe_id": af["id"]}],
    )["result"]  # fmt: skip
    flight = done["flights"][0]
    p = call(server, out, 6, "recommend.plan", flight_id=flight)["result"]
    assert p["stage"] == "evidence" and p["steps"][0]["params"]
    text = call(
        server, out, 7, "recommend.export", flight_id=flight, kind="qgroundcontrol", step=1,
        accepted_only=False,
    )["result"]  # fmt: skip
    assert set(parse_params(text["text"]).values()) == set(p["steps"][0]["params"])
    bad = [
        call(server, out, 8, "recommend.export", flight_id=flight, kind="csv")["error"]["code"],
        call(server, out, 9, "recommend.export", flight_id=flight, kind="qgroundcontrol",
             step=9)["error"]["code"],
        call(server, out, 10, "recommend.plan", flight_id="none")["error"]["code"],
    ]  # fmt: skip
    assert bad == [-32602, -32003, -32003]
    kept = call(server, out, 11, "recommend.history", airframe_id=af["id"])["result"]
    assert {h["id"] for h in kept} == {r["id"] for r in p["recommendations"] if "id" in r}
    # Only what a person accepted goes in the file: nothing yet.
    nothing = call(server, out, 16, "recommend.export", flight_id=flight, kind="mission planner")
    assert nothing["error"]["code"] == -32003
    offered = next(r for r in p["recommendations"] if r["status"] == "next" and r["param"])
    rid = offered["id"]
    assert offered["record_version"] == 1
    done = call(server, out, 12, "recommend.set_status", id=rid, status="accepted", version=1)
    assert done["result"]["status"] == "accepted"
    stale = call(server, out, 13, "recommend.set_status", id=rid, status="applied", version=1)
    graded = call(server, out, 14, "recommend.set_status", id=rid, status="confirmed", version=2)
    assert (stale["error"]["code"], graded["error"]["code"]) == (-32004, -32003)
    target = tmp_path / "accepted.param"  # written where the person chose
    saved = call(
        server, out, 17, "recommend.export", flight_id=flight, kind="mission planner",
        path=str(target),
    )["result"]  # fmt: skip
    written = parse_params(target.read_text(encoding="utf-8")).values()
    assert written == {offered["param"]: offered["proposed"]} and saved["path"] == str(target)
    wrong = call(server, out, 18, "recommend.export", flight_id=flight, kind="mission planner",
                 path=str(tmp_path / "x.txt"))  # fmt: skip
    assert wrong["error"]["code"] == -32602
    # The procedure: the session card, and the file that takes the test-flight logging out.
    assert p["session"] and all(r["procedure"] for r in p["recommendations"] if r["param"])
    card = call(
        server, out, 19, "recommend.export", flight_id=flight, file="card", lang="uk",
        accepted_only=False,
    )["result"]  # fmt: skip
    assert card["filename"].endswith("-card.txt") and "\n1. " in card["text"]
    revert = call(
        server, out, 20, "recommend.export", flight_id=flight, file="revert",
        kind="mission planner", accepted_only=False,
    )["result"]  # fmt: skip
    assert "INS_RAW_LOG_OPT" in parse_params(revert["text"]).values()
    other = call(server, out, 21, "recommend.export", flight_id=flight, file="log")
    assert other["error"]["code"] == -32602
    call(server, out, 15, "engine.shutdown")


# --- procedures ---------------------------------------------------------------------------------


def test_a_filter_change_brings_its_test_flight_logging_and_takes_it_out_again(
    reg: Registry,
) -> None:
    p = plan(
        reg,
        finding("filter.better_settings", ("INS_HNTCH_BW", 30.0)),
        INS_RAW_LOG_OPT=0.0,
    )
    bitmask, raw = one(p, "LOG_BITMASK"), one(p, "INS_RAW_LOG_OPT")
    assert bitmask.temporary and raw.temporary
    assert int(bitmask.proposed or 0) == 176126 | 1 << 19 | 1 << 21  # raw IMU, fast notch logging
    assert (raw.proposed, raw.revert) == (9.0, 0.0)  # primary gyro, before and after the filters
    assert bitmask.revert == 176126.0
    assert not one(p, "INS_HNTCH_BW").requires  # test-flight settings tie nothing together
    revert = parse_params(export_revert(p, reg, "mission planner")["text"]).values()
    assert revert == {"LOG_BITMASK": 176126.0, "INS_RAW_LOG_OPT": 0.0}
    # INS_HNTCH_BW had no value in this flight's parameters, so there is nothing to put back
    undo = parse_params(export_undo(p, reg, "mission planner")["text"]).values()
    assert undo == {"LOG_BITMASK": 176126.0, "INS_RAW_LOG_OPT": 0.0}


def test_a_lasting_bit_stays_when_the_test_flight_logging_comes_out(reg: Registry) -> None:
    p = plan(
        reg,
        finding("filter.better_settings", ("INS_HNTCH_BW", 30.0)),
        finding("logging.missing_messages", ("LOG_BITMASK", float(176126 | 1 << 12))),
        INS_RAW_LOG_OPT=9.0,
    )
    rec = one(p, "LOG_BITMASK")
    assert not rec.temporary  # a lasting bit is asked for too
    assert int(rec.proposed or 0) == 176126 | 1 << 12 | 1 << 19 | 1 << 21
    assert rec.revert == float(176126 | 1 << 12)  # only the test flight's bits come out


def test_blocked_changes_bring_no_test_flight_logging(reg: Registry) -> None:
    p = plan(reg, finding("filter.better_settings", ("INS_GYRO_FILTER", 300.0)))
    assert [r.param for r in p.recommendations] == ["INS_GYRO_FILTER"]


def test_the_session_card_is_in_order(reg: Registry) -> None:
    p = plan(
        reg,
        finding("filter.better_source", ("INS_HNTCH_MODE", 3.0), ("INS_HNTCH_REF", 1.0)),
        INS_RAW_LOG_OPT=0.0,
    )
    card = session(p, reg)
    kinds = [s.kind for s in card]
    assert kinds[0] == "set" and "reboot" in kinds  # REF needs a reboot in 4.7
    order = [
        "set",
        "reboot",
        "temporary",
        "ground_check",
        "fly",
        "abort",
        "verify",
        "revert",
        "undo",
    ]
    assert [k for k in order if k in kinds] == sorted(dict.fromkeys(kinds), key=order.index)
    motor_test = next(s for s in card if s.kind == "ground_check")
    assert "propellers off" in motor_test.text["en"]  # ESC RPM: every motor must report it
    aborts = [s.text["en"] for s in card if s.kind == "abort"]
    assert any("oscillates" in a for a in aborts)
    text = export_card(p, reg, "uk")["text"]
    assert text.splitlines()[0].startswith("Flight Insights") and "1. " in text


def test_a_step_that_does_not_apply_is_left_out(reg: Registry) -> None:
    # The motor test that proves ESC RPM is only for a notch following ESC RPM.
    p = plan(reg, finding("filter.better_settings", ("INS_HNTCH_BW", 30.0)))  # throttle mode
    assert not [s for s in session(p, reg) if s.kind == "ground_check"]


def test_a_better_option_that_needs_new_firmware_is_an_action_with_steps(reg: Registry) -> None:
    p = plan(reg, finding("option.bdshot_enable.hardware", severity="info"))
    [action] = p.recommendations
    assert action.param is None and action.kind == "performance"
    steps = procedure(p, reg, action)
    assert [s.kind for s in steps][:2] == ["firmware", "hardware"]
    assert "Bluejay" in steps[0].text["en"]


def test_every_procedure_renders_in_both_languages(reg: Registry) -> None:
    for code, template in reg.procedures.items():
        for s in template["steps"]:
            assert set(s["text"]) == {"en", "uk"}, code
    for key in (
        "set",
        "refresh",
        "reboot_refresh",
        "reboot",
        "temporary",
        "revert",
        "undo",
        "card_header",
    ):
        assert set(reg.procedure_texts[key]) == {"en", "uk"}


def test_test_flight_settings_name_real_bits(reg: Registry) -> None:
    meta = reg.pack.metadata("4.7.0", "copter")
    for code, template in reg.procedures.items():
        for add in template["add_temporary"]:
            assert add["param"] in meta, code
            bits: dict[str, str] = meta[add["param"]].get("bitmask") or {}
            assert all(str(b) in bits for b in add.get("set_bits", ())), code
    names = meta["LOG_BITMASK"]["bitmask"]
    assert (names["19"], names["21"]) == ("Raw IMU", "Fast harmonic notch logging")


def test_each_step_is_worded_with_its_own_findings_values(reg: Registry) -> None:
    lead = finding("filter.better_source", ("INS_HNTCH_MODE", 3.0))
    lead["params"] = {
        "current": "throttle",
        "better": "ESC telemetry",
        "explains": ["filter.better_settings"],
    }
    settings = finding("filter.better_settings", severity="info")
    settings["params"] = {"lag_new": 12.5, "lag_hz": 20, "lag_now": 18.0}
    checks = [s.text["en"] for s in session(plan(reg, lead, settings), reg) if s.kind == "verify"]
    assert any("12.5° at 20 Hz" in c for c in checks)  # from the settings finding, not the lead
    assert any("following ESC telemetry" in c for c in checks)
    bare = [
        s.text["en"]
        for s in session(plan(reg, finding("filter.better_settings", ("INS_HNTCH_BW", 30.0))), reg)
    ]
    assert not any("{" in line for line in bare)  # a finding without the values leaves the step out
