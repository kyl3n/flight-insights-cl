"""The parameter audit: the four verdicts, values against the firmware's documentation,
failsafes, battery and voltage settings against the cell count, gains never tuned, and
the defaults kept with a snapshot."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from flight_builder import hover_flight

from flight_insights.analyzers import Finding, Registry
from flight_insights.analyzers.params_audit import PARAMS_AUDIT
from flight_insights.dataflash import read_dataflash
from flight_insights.history import from_log, load_snapshot, save_snapshot
from flight_insights.knowledge import load_pack
from flight_insights.store import StoreKeys, open_store

LOG = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0/copter-quad-hover.bin"
GAINS = {
    "ATC_RAT_RLL_P": 0.135, "ATC_RAT_RLL_I": 0.135, "ATC_RAT_RLL_D": 0.0036,
    "ATC_RAT_PIT_P": 0.135, "ATC_RAT_PIT_I": 0.135, "ATC_RAT_PIT_D": 0.0036,
}  # fmt: skip
SAFE = {  # failsafes and battery set up properly, for a 4-cell pack
    "FS_THR_ENABLE": 1.0, "FS_THR_VALUE": 975.0, "RC3_MIN": 1000.0, "FS_EKF_ACTION": 1.0,
    "FS_CRASH_CHECK": 1.0, "BATT_MONITOR": 4.0, "BATT_LOW_VOLT": 14.0, "BATT_CRT_VOLT": 13.2,
    "BATT_FS_LOW_ACT": 2.0, "BATT_FS_CRT_ACT": 1.0, "MOT_BAT_VOLT_MAX": 16.8,
    "MOT_BAT_VOLT_MIN": 13.2,
}  # fmt: skip
CELLS = {"cells": {"value": 4, "source": "BAT.Volt, MOT_BAT_VOLT_MAX"}}


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def audit(
    reg: Registry,
    params: dict[str, float],
    defaults: dict[str, float] | None = None,
    fp: dict[str, Any] | None = None,
    answers: dict[str, Any] | None = None,
) -> tuple[dict[str, Finding], dict[str, Any]]:
    flight = hover_flight({}, SAFE | params, CELLS | (fp or {}), answers, defaults)
    r = PARAMS_AUDIT.run(flight, reg)
    [ev] = [e for e in r.evidence if e.id == "params:audit"]
    return {f.code: f for f in r.findings}, dict(ev.values)


def test_a_set_up_aircraft_raises_nothing(reg: Registry) -> None:
    found, _ = audit(reg, {})
    assert found == {}


# --- the four verdicts ---------------------------------------------------------------------


def test_every_parameter_gets_a_verdict(reg: Registry) -> None:
    params = {"INS_GYRO_FILTER": 40.0, "LOG_BITMASK": 176126.0, "FRAME_CLASS": 1.0,
              "INS_ACCOFFS_X": 0.1, "MY_SCRIPT_GAIN": 2.0}  # fmt: skip
    defaults = {"INS_GYRO_FILTER": 20.0, "LOG_BITMASK": 176126.0, "FRAME_CLASS": 0.0,
                "INS_ACCOFFS_X": 0.0}  # fmt: skip
    fp: dict[str, Any] = {"frame": {"value": {}, "source": "FRAME_CLASS, FRAME_TYPE"}}
    found, ev = audit(reg, params, defaults, fp)
    assert "INS_GYRO_FILTER" in ev["unexplained"]  # a tuning value changed for no known reason
    assert ev["counts"]["default"] >= 1  # LOG_BITMASK as the firmware ships it
    assert ev["unknown"] == ["MY_SCRIPT_GAIN"]  # a custom build's own: unknown, not wrong
    assert found["params.unexplained"].severity == "info"
    # the frame (read by the fingerprint) and a calibration are explained
    assert not {"FRAME_CLASS", "INS_ACCOFFS_X"} & set(ev["unexplained"])


def test_a_firmware_default_outside_its_own_range_is_not_an_error(reg: Registry) -> None:
    found, ev = audit(reg, {"ATC_RAT_PIT_FF": 0.0}, {"ATC_RAT_PIT_FF": 0.0})  # range 0.05-0.5
    assert "params.out_of_range" not in found and "ATC_RAT_PIT_FF" not in ev["inconsistent"]


def test_a_chosen_value_outside_its_range_is_inconsistent(reg: Registry) -> None:
    params = {"ATC_RAT_RLL_P": 0.45, "INS_GYRO_FILTER": 300.0}  # ranges 0-0.35 and 0-256
    found, ev = audit(reg, params, {"ATC_RAT_RLL_P": 0.135, "INS_GYRO_FILTER": 20.0})
    setup, gain = found["params.out_of_range"], found["params.gain_out_of_range"]
    assert (setup.params["names"], gain.params["names"]) == (["INS_GYRO_FILTER"], ["ATC_RAT_RLL_P"])
    assert {"ATC_RAT_RLL_P", "INS_GYRO_FILTER"} <= set(ev["inconsistent"])
    assert gain.evidence[0].values["ATC_RAT_RLL_P"]["reason"] == "out_of_range"
    # a gain is judged when tuning, after the filters, so it never holds them back
    assert (reg.findings[setup.code].stage, reg.findings[gain.code].stage) == ("setup", "rate tune")


def test_simulator_settings_are_not_judged(reg: Registry) -> None:
    sitl = {"board": {"hal": "SITL", "family": "SITL"}}
    found, ev = audit(reg, {"SIM_PLD_YAW": 999.0}, {"SIM_PLD_YAW": 0.0}, sitl)
    assert "params.out_of_range" not in found and "SIM_PLD_YAW" not in ev["unexplained"]


# --- failsafes -------------------------------------------------------------------------


def test_a_radio_failsafe_switched_off_is_a_safety_finding(reg: Registry) -> None:
    found, ev = audit(reg, {"FS_THR_ENABLE": 0.0})
    assert found["params.rc_failsafe_off"].severity == "safety"
    assert "FS_THR_ENABLE" in ev["inconsistent"]


@pytest.mark.parametrize(
    ("value", "rc_min", "fails"),
    [(975.0, 1000.0, False), (905.0, 1000.0, True), (995.0, 1000.0, True), (990.0, 1000.0, True)],
)
def test_fs_thr_value_follows_the_arming_rule(
    reg: Registry, value: float, rc_min: float, fails: bool
) -> None:
    # AP_Arming_Copter: fails when RC3_MIN <= FS_THR_VALUE + 10 or FS_THR_VALUE < 910
    found, _ = audit(reg, {"FS_THR_VALUE": value, "RC3_MIN": rc_min})
    assert ("params.fs_thr_value" in found) is fails


def test_the_throttle_channel_comes_from_rcmap(reg: Registry) -> None:
    found, _ = audit(reg, {"RCMAP_THROTTLE": 1.0, "RC1_MIN": 980.0})
    assert found["params.fs_thr_value"].params["rc_min_param"] == "RC1_MIN"


def test_failsafes_that_only_report(reg: Registry) -> None:
    found, _ = audit(reg, {"FS_EKF_ACTION": 0.0, "FS_CRASH_CHECK": 0.0, "BATT_FS_LOW_ACT": 0.0})
    assert {"params.ekf_failsafe_report_only", "params.crash_check_off",
            "params.battery_warn_only"} <= set(found)  # fmt: skip


def test_the_critical_battery_failsafe(reg: Registry) -> None:
    none, _ = audit(reg, {"BATT_CRT_VOLT": 0.0, "BATT_CRT_MAH": 0.0})
    assert "params.battery_no_critical" in none
    warn, _ = audit(reg, {"BATT_FS_CRT_ACT": 0.0})
    assert warn["params.battery_critical_warn_only"].severity == "out"


def test_no_battery_monitor_no_battery_findings(reg: Registry) -> None:
    found, _ = audit(reg, {"BATT_MONITOR": 0.0, "BATT_FS_LOW_ACT": 0.0, "BATT_CRT_VOLT": 0.0})
    assert not any(c.startswith("params.battery") for c in found)


# --- settings made for another pack ---------------------------------------------------


def test_a_battery_threshold_for_another_pack(reg: Registry) -> None:
    flight = hover_flight({}, SAFE | {"BATT_LOW_VOLT": 10.5}, CELLS | {"cells": {"value": 12}})
    found = [
        f for f in PARAMS_AUDIT.run(flight, reg).findings
        if f.code == "params.battery_threshold_cells"
    ]  # fmt: skip
    assert [f.params for f in found] == [  # both set for a 3-cell pack, one finding each
        {"param": "BATT_LOW_VOLT", "value": "10.5", "per_cell": "0.88", "cells": 12},
        {"param": "BATT_CRT_VOLT", "value": "13.2", "per_cell": "1.1", "cells": 12},
    ]
    # a failsafe that can never fire leaves no battery protection at all
    assert all(f.severity == "safety" and f.changes == () for f in found)  # the team sets it


def test_voltage_scaling_for_another_pack_is_fixed_from_the_chemistry(reg: Registry) -> None:
    fp = {"cells": {"value": 6}}
    asked, _ = audit(reg, {"MOT_BAT_VOLT_MAX": 16.8, "MOT_BAT_VOLT_MIN": 13.2}, fp=fp)
    assert asked["params.motor_scaling_cells"].changes == ()  # no chemistry answer, no value
    found = PARAMS_AUDIT.run(
        hover_flight({}, SAFE | {"MOT_BAT_VOLT_MAX": 16.8, "MOT_BAT_VOLT_MIN": 13.2,
                                 "BATT_LOW_VOLT": 21.0, "BATT_CRT_VOLT": 19.8},
                     CELLS | fp, {"battery_chemistry": "LiPo"}),
        reg,
    ).findings  # fmt: skip
    changes = {c.param: c.proposed for f in found for c in f.changes}
    assert changes == {"MOT_BAT_VOLT_MAX": 25.2, "MOT_BAT_VOLT_MIN": 19.8}


# --- never tuned ------------------------------------------------------------------------


def test_rate_gains_at_their_defaults_were_never_tuned(reg: Registry) -> None:
    found, _ = audit(reg, GAINS, GAINS)
    assert found["params.never_tuned"].severity == "caution"
    tuned, _ = audit(reg, GAINS | {"ATC_RAT_RLL_P": 0.2}, GAINS)
    assert "params.never_tuned" not in tuned
    unknown, _ = audit(reg, GAINS)  # defaults not recorded: no telling
    assert "params.never_tuned" not in unknown


# --- defaults kept with the snapshot -------------------------------------------------------


def test_the_logs_defaults_survive_the_store(tmp_path: Path) -> None:
    with read_dataflash(LOG) as df:
        snap = from_log(df)
    assert snap.defaults
    keys = StoreKeys.generate()
    with open_store(tmp_path / "f.db", keys, "e" * 32) as store:
        back = load_snapshot(store, save_snapshot(store, snap, load_pack()))
    assert back.defaults == snap.defaults
    assert back.is_default("FS_THR_VALUE") == snap.is_default("FS_THR_VALUE")


# --- every battery monitor, the pack's capacity, arming and calibration ---------------------


def test_a_further_monitor_whose_failsafe_only_warns(reg: Registry) -> None:
    params = {"BATT2_MONITOR": 4.0, "BATT2_LOW_VOLT": 22.0, "BATT2_FS_LOW_ACT": 0.0}
    found, ev = audit(reg, params)
    f = found["params.other_battery_warn_only"]
    assert f.params == {"monitor": "BATT2", "param": "BATT2_FS_LOW_ACT", "level": "low"}
    assert "BATT2_FS_LOW_ACT" in ev["inconsistent"]
    # a monitor that only reports, with no thresholds, is left alone
    quiet, _ = audit(reg, {"BATT2_MONITOR": 9.0, "BATT2_FS_LOW_ACT": 0.0})
    assert "params.other_battery_warn_only" not in quiet


def test_the_capacity_is_the_packs(reg: Registry) -> None:
    found, _ = audit(reg, {"BATT_CAPACITY": 3300.0}, answers={"capacity_mah": 22000})
    f = found["params.battery_capacity"]
    assert [(c.param, c.current, c.proposed) for c in f.changes] == [
        ("BATT_CAPACITY", 3300.0, 22000.0)
    ]
    close, _ = audit(reg, {"BATT_CAPACITY": 21000.0}, answers={"capacity_mah": 22000})
    assert "params.battery_capacity" not in close  # within 10%
    unanswered, _ = audit(reg, {"BATT_CAPACITY": 3300.0})
    assert "params.battery_capacity" not in unanswered


def test_arming_that_never_checks_the_charge(reg: Registry) -> None:
    found, _ = audit(reg, {"BATT_ARM_VOLT": 0.0, "BATT_ARM_MAH": 0.0})
    assert found["params.battery_arm_unchecked"].severity == "info"
    checked, _ = audit(reg, {"BATT_ARM_VOLT": 15.6, "BATT_ARM_MAH": 0.0})
    assert "params.battery_arm_unchecked" not in checked


def test_an_analog_power_module_never_calibrated(reg: Registry) -> None:
    answers = {"power_module_calibrated": False}
    found, _ = audit(reg, {}, answers=answers)
    assert found["params.power_module_uncalibrated"].severity == "caution"
    smart, _ = audit(reg, {"BATT_MONITOR": 8.0}, answers=answers)  # DroneCAN: no scales
    assert "params.power_module_uncalibrated" not in smart
    done, _ = audit(reg, {}, answers={"power_module_calibrated": True})
    assert "params.power_module_uncalibrated" not in done


# --- failsafes switched off, arming checks skipped, thresholds from the pack --------------

LIPO = {"battery_chemistry": "LiPo"}


@pytest.mark.parametrize(
    ("name", "code", "default"),
    [("FS_VIBE_ENABLE", "params.vibe_failsafe_off", 1.0),
     ("FS_DR_ENABLE", "params.dead_reckoning_failsafe_off", 2.0),
     ("FS_EKF_THRESH", "params.ekf_failsafe_off", 0.8)],
)  # fmt: skip
def test_a_failsafe_switched_off_goes_back_to_the_firmwares_value(
    reg: Registry, name: str, code: str, default: float
) -> None:
    found, ev = audit(reg, {name: 0.0}, {name: default})
    assert [(c.param, c.current, c.proposed) for c in found[code].changes] == [(name, 0.0, default)]
    assert name in ev["inconsistent"]
    unknown, _ = audit(reg, {name: 0.0})  # no recorded default: nothing to propose
    assert unknown[code].changes == ()


def test_flown_from_the_ground_station_alone_needs_a_gcs_failsafe(reg: Registry) -> None:
    found, _ = audit(reg, {"RC_OPTIONS": 1.0, "FS_GCS_ENABLE": 0.0})
    assert found["params.gcs_failsafe_off"].severity == "safety"
    with_rc, _ = audit(reg, {"RC_OPTIONS": 0.0, "FS_GCS_ENABLE": 0.0})
    assert "params.gcs_failsafe_off" not in with_rc


def test_skipped_arming_checks_are_named_and_cleared(reg: Registry) -> None:
    found, _ = audit(reg, {"ARMING_SKIPCHK": float(1 << 2 | 1 << 11)})
    f = found["params.arming_checks_skipped"]
    assert f.params == {"checks": "Compass, Hardware safety switch"}
    assert [(c.param, c.proposed) for c in f.changes] == [("ARMING_SKIPCHK", 0.0)]


def test_battery_thresholds_come_from_the_pack(reg: Registry) -> None:
    # Mission Planner's starting values for a 4-cell LiPo (empty at 3.3 V a cell)
    params = {"BATT_LOW_VOLT": 0.0, "BATT_LOW_MAH": 0.0, "BATT_CRT_VOLT": 0.0,
              "BATT_CRT_MAH": 0.0, "BATT_ARM_VOLT": 0.0, "BATT_ARM_MAH": 0.0}  # fmt: skip
    found, _ = audit(reg, params, answers=LIPO)
    proposed = {
        c.param: c.proposed
        for code in ("params.battery_no_low", "params.battery_no_critical",
                     "params.battery_arm_unchecked")
        for c in found[code].changes
    }  # fmt: skip
    assert proposed == {"BATT_LOW_VOLT": 14.4, "BATT_CRT_VOLT": 14.0, "BATT_ARM_VOLT": 14.7}
    unknown, _ = audit(reg, params)  # chemistry not answered: the findings, no values
    assert unknown["params.battery_no_low"].changes == ()
