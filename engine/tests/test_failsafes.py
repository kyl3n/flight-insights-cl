"""Whether each failsafe did what its settings call for, judged against ArduCopter 4.7's
own decision logic (rules/failsafes.json), on flights built in memory."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pyarrow as pa
import pytest
from flight_builder import flight, table

from flight_insights.analyzers import ANALYZERS, FlightData, Registry
from flight_insights.analyzers.failsafes import FAILSAFES
from flight_insights.analyzers.model import Finding
from flight_insights.knowledge import Pack, load_pack

ARM, DISARM, LIFT = 10_000_000, 90_000_000, 13_000_000
T = 40_000_000  # when the failsafe fires
SUBSYS = {"radio": 5, "batt": 6, "gcs": 8, "ekf": 17}
MODES = {"STABILIZE": 0, "ALT_HOLD": 2, "AUTO": 3, "GUIDED": 4, "LOITER": 5, "RTL": 6,
         "LAND": 9, "BRAKE": 17, "SMART_RTL": 21, "AUTO_RTL": 27}  # fmt: skip
REASON = {"pilot": 1, "radio": 3, "batt": 4, "gcs": 5, "ekf": 6}
NOT_LANDED, LAND_COMPLETE = 28, 18
DISARM_METHOD = {"radio": 16, "gcs": 18, "batt": 7, "terminate": 20, "pilot": 2}


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


@pytest.fixture(scope="module")
def reg(pack: Pack) -> Registry:
    return Registry.load(pack)


def err(events: Sequence[tuple[int, str, int]]) -> pa.Table:
    """(time, failsafe, code): code 1 occurred, 0 resolved."""
    return table(
        [t for t, _, _ in events],
        {"Subsys": (pa.uint8(), [SUBSYS[k] for _, k, _ in events]),
         "ECode": (pa.uint8(), [c for _, _, c in events])},
    )  # fmt: skip


def mode(changes: Sequence[tuple[int, str, str]]) -> pa.Table:
    """(time, mode, why)."""
    return table(
        [t for t, _, _ in changes],
        {"Mode": (pa.uint8(), [MODES[m] for _, m, _ in changes]),
         "ModeNum": (pa.uint8(), [MODES[m] for _, m, _ in changes]),
         "Rsn": (pa.uint8(), [REASON[r] for _, _, r in changes])},
    )  # fmt: skip


def arm(disarm_at: int, how: str = "pilot") -> pa.Table:
    return table(
        [ARM, disarm_at],
        {"ArmState": (pa.uint8(), [1, 0]), "ArmChecks": (pa.uint32(), [0, 0]),
         "Method": (pa.uint8(), [DISARM_METHOD["pilot"], DISARM_METHOD[how]])},
    )  # fmt: skip


def ev(events: Sequence[tuple[int, int]]) -> pa.Table:
    return table([t for t, _ in events], {"Id": (pa.uint8(), [i for _, i in events])})


def build(
    start_mode: str,
    errors: Sequence[tuple[int, str, int]],
    changes: Sequence[tuple[int, str, str]] = (),
    params: Mapping[str, float] | None = None,
    landed: Sequence[tuple[int, int]] = ((LIFT, NOT_LANDED),),
    disarm_at: int = DISARM,
    disarm_how: str = "pilot",
    extra: Mapping[str, pa.Table] | None = None,
) -> FlightData:
    tables = {
        "ERR": err(errors),
        "MODE": mode([(ARM - 1_000_000, start_mode, "pilot"), *changes]),
        "ARM": arm(disarm_at, disarm_how),
        "EV": ev(landed),
        **(extra or {}),
    }
    return flight(tables, ARM, disarm_at, {"FS_OPTIONS": 0, **(params or {})})


def judged(reg: Registry, f: FlightData) -> list[Finding]:
    r = FAILSAFES.run(f, reg)
    assert r.skipped is None
    return r.findings


def one(reg: Registry, f: FlightData) -> Finding:
    [finding] = judged(reg, f)
    return finding


def verdict(f: Finding) -> tuple[str, str, str]:
    return f.code.removeprefix("failsafe."), f.params["response"], f.params["wanted"]


# --- the setting, and its fallbacks -----------------------------------------------------


def test_radio_failsafe_switching_to_rtl_is_as_configured(reg: Registry) -> None:
    f = one(reg, build("LOITER", [(T, "radio", 1), (T + 9_000_000, "radio", 0)],
                       [(T + 2_500, "RTL", "radio")], {"FS_THR_ENABLE": 1}))  # fmt: skip
    assert verdict(f) == ("as_configured", "switched to RTL", "RTL")
    assert (f.severity, f.confidence) == ("info", "high")
    assert (f.params["setting"], f.params["value"]) == ("FS_THR_ENABLE", "1")
    [e] = f.evidence
    assert e.values["responded_after_s"] == 0.003 and e.values["lasted_s"] == 9.0
    assert e.values["allowed"] == ["RTL", "LAND"] and e.values["left_open"] == []
    assert e.segment is not None and (e.segment.start_us, e.segment.end_us) == (T, T + 9_000_000)


def test_the_response_logged_in_the_same_microsecond_is_not_the_mode_it_fired_in(
    reg: Registry,
) -> None:
    # ArduCopter 4.7.0 SITL logs the error and its mode change with one timestamp
    f = one(reg, build("LOITER", [(T, "radio", 1)], [(T, "RTL", "radio")], {"FS_THR_ENABLE": 1}))
    assert verdict(f) == ("as_configured", "switched to RTL", "RTL")
    assert f.params["mode"] == "LOITER" and f.evidence[0].values["responded_after_s"] == 0.0
    # a change for another reason at that moment came first: the failsafe fired in it
    g = one(reg, build("LOITER", [(T, "radio", 1)], [(T, "RTL", "pilot")], {"FS_THR_ENABLE": 1}))
    assert verdict(g) == ("as_configured", "stayed in RTL", "RTL") and g.params["mode"] == "RTL"


def test_a_fallback_mode_is_a_caution_naming_what_could_not_start(reg: Registry) -> None:
    f = one(reg, build("LOITER", [(T, "radio", 1)], [(T + 2_500, "RTL", "radio")],
                       {"FS_THR_ENABLE": 4}))  # SmartRTL, else RTL  # fmt: skip
    assert verdict(f) == ("fell_back", "switched to RTL", "SMART_RTL")
    assert f.severity == "caution"


@pytest.mark.parametrize(
    ("kind", "param", "expected"),
    [("radio", "FS_THR_ENABLE", "as_configured"),  # radio 3: Land
     ("gcs", "FS_GCS_ENABLE", "fell_back")],  # GCS 3: SmartRTL, else RTL, else Land
)  # fmt: skip
def test_the_gcs_setting_is_numbered_differently_from_the_radios(
    reg: Registry, kind: str, param: str, expected: str
) -> None:
    f = one(reg, build("LOITER", [(T, kind, 1)], [(T + 2_500, "LAND", kind)], {param: 3}))
    assert f.code == f"failsafe.{expected}"


def test_a_response_the_settings_never_allow_is_unexpected(reg: Registry) -> None:
    f = one(reg, build("LOITER", [(T, "radio", 1)], [(T + 2_500, "RTL", "radio")],
                       {"FS_THR_ENABLE": 3}))  # Land  # fmt: skip
    assert verdict(f) == ("unexpected", "switched to RTL", "LAND")
    assert (f.severity, f.params["value"]) == ("out", "3")


def test_no_mode_change_when_one_was_due_is_unexpected(reg: Registry) -> None:
    f = one(reg, build("LOITER", [(T, "radio", 1)], [], {"FS_THR_ENABLE": 1}))
    assert verdict(f) == ("unexpected", "stayed in LOITER", "RTL")


def test_a_mode_change_by_the_pilot_is_not_the_failsafes_response(reg: Registry) -> None:
    f = one(reg, build("LOITER", [(T, "radio", 1)], [(T + 2_500, "RTL", "pilot")],
                       {"FS_THR_ENABLE": 1}))  # fmt: skip
    assert verdict(f)[:2] == ("unexpected", "stayed in LOITER")


def test_asking_for_the_mode_it_is_in_logs_no_change(reg: Registry) -> None:
    f = one(reg, build("RTL", [(T, "radio", 1)], [], {"FS_THR_ENABLE": 1}))
    assert verdict(f)[:2] == ("as_configured", "stayed in RTL")
    assert f.confidence == "high"  # whether RTL was landing changes nothing here
    landing = {"FS_THR_ENABLE": 4, "FS_OPTIONS": 8}  # SmartRTL; keep landing if landing
    g = one(reg, build("RTL", [(T, "radio", 1)], [(T, "SMART_RTL", "radio")], landing))
    assert verdict(g)[:2] == ("as_configured", "switched to SMART_RTL")
    assert (g.confidence, g.evidence[0].values["left_open"]) == ("medium", ["landing"])


# --- the firmware's deviations ----------------------------------------------------------


def test_on_the_ground_it_disarms(reg: Registry) -> None:
    landed = ((LIFT, NOT_LANDED), (T - 3_000_000, LAND_COMPLETE))
    f = one(reg, build("LOITER", [(T, "radio", 1)], [], {"FS_THR_ENABLE": 1}, landed,
                       disarm_at=T + 2_500, disarm_how="radio"))  # fmt: skip
    assert verdict(f) == ("as_configured", "disarmed", "disarming")


def test_in_the_arming_delay_it_disarms(reg: Registry) -> None:
    f = one(reg, build("LOITER", [(ARM + 500_000, "gcs", 1)], [], {"FS_GCS_ENABLE": 1}, (),
                       disarm_at=ARM + 502_500, disarm_how="gcs"))  # fmt: skip
    assert verdict(f)[:2] == ("as_configured", "disarmed")


def ctun(zero_from: int | None) -> pa.Table:
    times = list(range(ARM, DISARM, 100_000))
    thi = [0.0 if zero_from is not None and t >= zero_from else 0.45 for t in times]
    return table(times, {"ThI": (pa.float32(), thi)})


def test_in_stabilize_with_the_throttle_at_zero_it_disarms(reg: Registry) -> None:
    f = one(reg, build("STABILIZE", [(T, "radio", 1)], [], {"FS_THR_ENABLE": 1},
                       disarm_at=T + 2_500, disarm_how="radio",
                       extra={"CTUN": ctun(T - 1_000_000)}))  # fmt: skip
    assert verdict(f)[:2] == ("as_configured", "disarmed")
    assert "CTUN" in f.evidence[0].messages and "ThI" in f.evidence[0].fields


def test_in_stabilize_with_throttle_up_it_returns(reg: Registry) -> None:
    f = one(reg, build("STABILIZE", [(T, "radio", 1)], [(T + 2_500, "RTL", "radio")],
                       {"FS_THR_ENABLE": 1}, extra={"CTUN": ctun(None)}))  # fmt: skip
    assert verdict(f)[:2] == ("as_configured", "switched to RTL")
    assert f.confidence == "high"


@pytest.mark.parametrize(
    ("zero_for_us", "allowed", "confidence"),
    [(200_000, ["RTL", "LAND"], "high"),  # off zero within the 0.4 s debounce: flying
     (450_000, ["disarm", "RTL", "LAND"], "medium")],  # the stick's moment falls between samples
)  # fmt: skip
def test_a_throttle_dropped_lately_disarms_only_after_the_debounce(
    reg: Registry, zero_for_us: int, allowed: list[str], confidence: str
) -> None:
    f = one(reg, build("STABILIZE", [(T, "radio", 1)], [(T + 2_500, "RTL", "radio")],
                       {"FS_THR_ENABLE": 1}, extra={"CTUN": ctun(T - zero_for_us)}))  # fmt: skip
    assert (f.code, f.confidence) == ("failsafe.as_configured", confidence)
    assert f.evidence[0].values["allowed"] == allowed


def cmd(ids: Sequence[tuple[int, int]]) -> pa.Table:
    return table([t for t, _ in ids], {"CId": (pa.uint16(), [i for _, i in ids])})


def test_continue_in_auto_needs_its_fs_options_bit(reg: Registry) -> None:
    mission = {"CMD": cmd([(LIFT, 22), (T - 5_000_000, 16), (T - 1_000_000, 178)])}
    keep = one(reg, build("AUTO", [(T, "radio", 1)], [],
                          {"FS_THR_ENABLE": 1, "FS_OPTIONS": 1}, extra=mission))  # fmt: skip
    assert verdict(keep) == ("as_configured", "stayed in AUTO", "carrying on")
    assert keep.confidence == "high"  # a waypoint leg: not landing, not guided
    assert "CMD" in keep.evidence[0].messages  # withheld from the AI path by the policy
    drop = one(reg, build("AUTO", [(T, "radio", 1)], [], {"FS_THR_ENABLE": 1}, extra=mission))
    assert verdict(drop) == ("unexpected", "stayed in AUTO", "RTL")


def test_auto_rtl_is_not_auto_for_the_continue_bits(reg: Registry) -> None:
    f = one(reg, build("AUTO_RTL", [(T, "gcs", 1)], [], {"FS_GCS_ENABLE": 1, "FS_OPTIONS": 2}))
    assert verdict(f) == ("unexpected", "stayed in AUTO_RTL", "RTL")


def test_a_guided_leg_of_a_mission_continues_with_the_guided_bit(reg: Registry) -> None:
    mission = {"CMD": cmd([(LIFT, 22), (T - 5_000_000, 92)])}  # NAV_GUIDED_ENABLE
    f = one(reg, build("AUTO", [(T, "radio", 1)], [], {"FS_THR_ENABLE": 1, "FS_OPTIONS": 4},
                       extra=mission))  # fmt: skip
    assert verdict(f)[:2] == ("as_configured", "stayed in AUTO")


def test_continue_under_pilot_control_is_for_the_gcs_failsafe_only(reg: Registry) -> None:
    gcs = one(reg, build("LOITER", [(T, "gcs", 1)], [], {"FS_GCS_ENABLE": 1, "FS_OPTIONS": 16}))
    assert gcs.code == "failsafe.as_configured"
    radio = one(reg, build("LOITER", [(T, "radio", 1)], [], {"FS_THR_ENABLE": 1, "FS_OPTIONS": 16}))
    assert radio.code == "failsafe.unexpected"
    guided = one(reg, build("GUIDED", [(T, "gcs", 1)], [(T + 2_500, "RTL", "gcs")],
                            {"FS_GCS_ENABLE": 1, "FS_OPTIONS": 16}))  # fmt: skip
    assert guided.code == "failsafe.as_configured"  # Guided is an autopilot mode


def bat(healthy_until: int | None = None) -> pa.Table:
    times = list(range(ARM, DISARM, 100_000))
    h = [0 if healthy_until is not None and t > healthy_until else 1 for t in times]
    return table(times, {"Inst": (pa.uint8(), [0] * len(times)), "H": (pa.uint8(), h)})


BATTERY = {"BATT_MONITOR": 4, "BATT_FS_LOW_ACT": 2, "BATT_FS_CRT_ACT": 1}


def test_each_battery_stage_is_judged_on_its_own_action(reg: Registry) -> None:
    errors = [(T, "batt", 1), (T + 20_000_000, "batt", 1)]  # low, then critical
    changes = [(T + 2_500, "RTL", "batt"), (T + 20_002_500, "LAND", "batt")]
    low, critical = judged(reg, build("LOITER", errors, changes, BATTERY, extra={"BAT": bat()}))
    assert verdict(low) == ("as_configured", "switched to RTL", "RTL")
    assert verdict(critical) == ("as_configured", "switched to LAND", "LAND")
    assert low.params["setting"] == "BATT_FS_LOW_ACT, BATT_FS_CRT_ACT"
    assert low.params["value"] == "2, 1"
    assert "BAT" in low.evidence[0].messages and "H" in low.evidence[0].fields


def test_one_monitors_stages_only_rise(reg: Registry) -> None:
    errors = [(T, "batt", 1), (T + 20_000_000, "batt", 1)]
    changes = [(T, "RTL", "batt"), (T + 20_000_000, "LOITER", "pilot")]  # pilot takes over
    one_monitor = judged(reg, build("LOITER", errors, changes, BATTERY, extra={"BAT": bat()}))
    # the second can only be the critical stage, which lands: carrying on is unexpected
    assert [verdict(f)[0] for f in one_monitor] == ["as_configured", "unexpected"]
    assert one_monitor[1].params["wanted"] == "LAND"
    two = {**BATTERY, "BATT2_MONITOR": 4, "BATT2_FS_LOW_ACT": 0, "BATT2_FS_CRT_ACT": 1}
    two_monitors = judged(reg, build("LOITER", errors, changes, two, extra={"BAT": bat()}))
    # or the second monitor's low stage, which only warns
    second = two_monitors[1]
    assert (second.code, second.confidence) == ("failsafe.as_configured", "medium")
    assert second.evidence[0].values["left_open"] == ["battery_stage"]


def test_a_battery_monitor_gone_quiet_raises_a_failsafe_with_no_action(reg: Registry) -> None:
    quiet = one(reg, build("LOITER", [(T, "batt", 1)], [], BATTERY,
                           extra={"BAT": bat(healthy_until=T - 6_000_000)}))  # fmt: skip
    assert verdict(quiet) == ("as_configured", "stayed in LOITER", "carrying on")
    healthy = one(reg, build("LOITER", [(T, "batt", 1)], [], BATTERY, extra={"BAT": bat()}))
    assert healthy.code == "failsafe.unexpected"


def test_landing_with_a_battery_failsafe_that_lands_keeps_landing(reg: Registry) -> None:
    params = {**BATTERY, "BATT_FS_LOW_ACT": 1, "FS_THR_ENABLE": 1}
    errors = [(T, "batt", 1), (T + 5_000_000, "radio", 1)]
    changes = [(T + 2_500, "LAND", "batt")]
    battery, radio = judged(reg, build("LOITER", errors, changes, params, extra={"BAT": bat()}))
    assert verdict(battery)[:2] == ("as_configured", "switched to LAND")
    assert verdict(radio) == ("as_configured", "stayed in LAND", "LAND")
    # without the battery failsafe the radio's RTL is due: staying in Land means RTL failed
    alone = one(reg, build("LAND", [(T + 5_000_000, "radio", 1)], [], {"FS_THR_ENABLE": 1}))
    assert verdict(alone) == ("fell_back", "stayed in LAND", "RTL")


def test_continue_if_landing_applies_to_the_battery_only_with_an_action(reg: Registry) -> None:
    params = {**BATTERY, "BATT_FS_LOW_ACT": 0, "BATT_FS_CRT_ACT": 0, "FS_OPTIONS": 8}
    f = one(reg, build("LAND", [(T, "batt", 1)], [], params, extra={"BAT": bat()}))
    assert verdict(f) == ("as_configured", "stayed in LAND", "carrying on")
    with_action = one(reg, build("LAND", [(T, "batt", 1)], [], {**params, "BATT_FS_LOW_ACT": 2},
                                 extra={"BAT": bat()}))  # fmt: skip
    assert verdict(with_action) == ("as_configured", "stayed in LAND", "LAND")


def test_terminate_disarms(reg: Registry) -> None:
    params = {**BATTERY, "BATT_FS_LOW_ACT": 5}
    f = one(reg, build("LOITER", [(T, "batt", 1)], [], params, disarm_at=T + 2_500,
                       disarm_how="terminate", extra={"BAT": bat()}))  # fmt: skip
    assert verdict(f)[:2] == ("as_configured", "disarmed")


# --- the EKF failsafe -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "action", "change", "expected"),
    [("ALT_HOLD", 1, None, "as_configured"),  # needs no position: report only
     ("LOITER", 1, "LAND", "as_configured"),
     ("ALT_HOLD", 3, "LAND", "as_configured"),  # Land even in Stabilize
     ("LOITER", 2, "ALT_HOLD", "as_configured"),
     ("LOITER", 0, "LAND", "unexpected"),  # report only
     ("LAND", 1, None, "as_configured")],
)  # fmt: skip
def test_the_ekf_failsafe_acts_only_where_a_position_is_needed(
    reg: Registry, start: str, action: int, change: str | None, expected: str
) -> None:
    changes = [(T + 2_500, change, "ekf")] if change else []
    f = one(reg, build(start, [(T, "ekf", 1)], changes, {"FS_EKF_ACTION": action}))
    assert f.code == f"failsafe.{expected}"


def test_ekf_althold_lands_instead_with_the_radio_lost(reg: Registry) -> None:
    errors = [(T, "radio", 1), (T + 1_000_000, "ekf", 1)]
    changes = [(T + 2_500, "RTL", "radio"), (T + 1_002_500, "LAND", "ekf")]
    params = {"FS_THR_ENABLE": 1, "FS_EKF_ACTION": 2}
    radio, ekf = judged(reg, build("LOITER", errors, changes, params))
    assert radio.code == ekf.code == "failsafe.as_configured"
    assert ekf.params["wanted"] == "LAND"


# --- which failsafes are judged ---------------------------------------------------------


def test_each_failsafe_from_its_error_to_its_resolution(reg: Registry) -> None:
    errors = [
        (T, "radio", 1),
        (T + 3_000_000, "radio", 0),
        (T + 20_000_000, "radio", 1),
        (T - 30_000_000 - ARM, "radio", 1),
    ]  # the last before arming: not judged
    changes = [(T + 2_500, "RTL", "radio"), (T + 5_000_000, "LOITER", "pilot"),
               (T + 20_002_500, "RTL", "radio")]  # fmt: skip
    first, second = judged(reg, build("LOITER", errors, changes, {"FS_THR_ENABLE": 1}))
    assert first.evidence[0].values["lasted_s"] == 3.0
    assert second.evidence[0].values["lasted_s"] is None
    assert (first.evidence[0].id, second.evidence[0].id) == ("failsafe:1", "failsafe:2")


def test_only_copter_flights_are_judged(reg: Registry) -> None:
    f = build("LOITER", [(T, "radio", 1)], [], {"FS_THR_ENABLE": 1})
    plane = flight(dict(f.cache.tables), ARM, DISARM, f.params,  # type: ignore[union-attr]
                   fingerprint={"vehicle": "plane", "hover_windows": []})  # fmt: skip
    assert FAILSAFES.run(plane, reg).skipped is not None


def test_a_flight_without_failsafes_has_nothing_to_judge(reg: Registry) -> None:
    f = build("LOITER", [], [], {"FS_THR_ENABLE": 1})
    r = FAILSAFES.run(f, reg)
    assert r.findings == [] and r.skipped is None


def test_the_analyzer_runs_with_the_rest(reg: Registry) -> None:
    assert FAILSAFES in ANALYZERS
    for code in FAILSAFES.findings:
        text: Any = reg.findings[code]
        assert text.stage == "operations" and "{failsafe}" in text.title["uk"]
    rules = reg.rules["failsafes"]
    assert rules["sources"] and rules["calibrated"] is True
    for kind, rule in rules["failsafes"].items():
        assert kind in reg.names("4.7.0")["error_subsystems"].values()
        for action in rule.get("actions", {}).values():
            assert action in rules["chains"]
