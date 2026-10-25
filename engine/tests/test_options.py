"""Better options: the catalog's integrity and coverage, the three outcomes on synthetic
flights, and the heavy-lift fixture with its platform's setup answers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import ARM, DISARM, hover_flight, table

from flight_insights.analyzers import Finding, Registry, run_analysis
from flight_insights.analyzers.options import OPTIONS
from flight_insights.imports import Decision, commit, propose, scan
from flight_insights.knowledge import load_pack
from flight_insights.platforms.records import create_platform, platform_view, save_answers
from flight_insights.platforms.schema import load_schema
from flight_insights.store import StoreKeys, open_store
from flight_insights.store.cache import ColumnCache

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
QUAD = {f"SERVO{k}_FUNCTION": float(32 + k) for k in range(1, 5)}
SET_UP = {  # nothing to improve, so each test changes one thing
    "MOT_PWM_TYPE": 6.0, "SERVO_BLH_BDMASK": 15.0, "MOT_HOVER_LEARN": 2.0,
    "MOT_BAT_VOLT_MAX": 25.2, "LOG_FILE_DSRMROT": 1.0, "FSTRATE_ENABLE": 1.0,
    "INS_HNTCH_ENABLE": 1.0, "INS_HNTCH_MODE": 3.0, "INS_HNTCH_OPTS": 8 | 4,
}  # fmt: skip


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def run(
    reg: Registry,
    answers: dict[str, Any] | None = None,
    fp: dict[str, Any] | None = None,
    tables: dict[str, pa.Table] | None = None,
    /,
    **params: float,
) -> dict[str, Finding]:
    p = QUAD | SET_UP | {k: float(v) for k, v in params.items()}
    flight = hover_flight(tables or {}, p, fp, answers)
    return {f.code: f for f in OPTIONS.run(flight, reg).findings}


def changes(f: Finding) -> dict[str, tuple[float | None, float | None]]:
    return {c.param: (c.current, c.proposed) for c in f.changes}


# --- the catalog ---------------------------------------------------------------------------


def test_every_option_is_explained_covered_and_asks_real_questions(reg: Registry) -> None:
    fields = set(load_schema(load_pack()).fields)
    covered = [o for s in reg.coverage.values() for o in s["options"]]
    assert sorted(covered) == sorted(reg.options)  # each option in exactly one subsystem
    for oid, o in reg.options.items():
        for code in (f"option.{oid}.available", f"option.{oid}.hardware"):
            text = reg.findings[code]
            assert text.title["en"] and text.title["uk"] and text.action["uk"]
        assert o.sources and all(s["title"] for s in o.sources)
    for fact, q in reg.questions.items():
        assert q["field"] in fields, fact  # answers go into the platform setup
    uncovered = [s for s, v in reg.coverage.items() if not v["options"] and not v.get("also")]
    assert "estimator_sources" in uncovered  # gaps are listed, not silent
    assert reg.coverage["plane"]["phase"] == 3 and reg.coverage["heli"]["phase"] == 4


# --- the three outcomes -------------------------------------------------------------------------


def test_a_well_set_up_aircraft_has_nothing_to_offer(reg: Registry) -> None:
    assert run(reg, {"esc_firmware": "BLHeli_32", "payload": "variable"}) == {}


def test_pwm_with_unknown_esc_firmware_asks(reg: Registry) -> None:
    found = run(reg, MOT_PWM_TYPE=0)
    q = found["option.question.esc_dshot_capable"]
    assert q.params == {"field": "esc_firmware", "options": ["dshot_output"]}


def test_pwm_with_dshot_esc_firmware_is_available_now(reg: Registry) -> None:
    f = run(reg, {"esc_firmware": "Bluejay"}, MOT_PWM_TYPE=0)["option.dshot_output.available"]
    assert changes(f) == {"MOT_PWM_TYPE": (0.0, 6.0)}


def test_bidirectional_dshot_needs_capable_esc_firmware(reg: Registry) -> None:
    hardware = run(reg, {"esc_firmware": "BLHeli_S"}, SERVO_BLH_BDMASK=0)
    assert set(hardware) == {"option.bdshot_enable.hardware"}
    available = run(reg, {"esc_firmware": "AM32"}, SERVO_BLH_BDMASK=0)[
        "option.bdshot_enable.available"
    ]
    assert changes(available) == {"SERVO_BLH_BDMASK": (0.0, 15.0)}  # the four motor outputs
    assert available.severity == "caution"  # benefit 3: the notch can follow every motor


def test_an_answer_that_settles_nothing_is_not_asked_again(reg: Registry) -> None:
    assert run(reg, {"esc_firmware": "other"}, MOT_PWM_TYPE=0) == {}


def test_hover_learning_for_a_variable_payload(reg: Registry) -> None:
    f = run(reg, {"payload": "variable", "esc_firmware": "AM32"}, MOT_HOVER_LEARN=0)
    assert changes(f["option.hover_learn.available"]) == {"MOT_HOVER_LEARN": (0.0, 2.0)}
    assert run(reg, {"payload": "fixed", "esc_firmware": "AM32"}, MOT_HOVER_LEARN=0) == {}
    asked = run(reg, {"esc_firmware": "AM32"}, MOT_HOVER_LEARN=0)
    assert set(asked) == {"option.question.variable_payload"}


def test_battery_voltage_scaling_from_cells_and_chemistry(reg: Registry) -> None:
    fp = {"cells": {"value": 6}}
    f = run(reg, {"battery_chemistry": "LiPo"}, fp, MOT_BAT_VOLT_MAX=0)
    assert changes(f["option.battery_voltage_scaling.available"]) == {
        "MOT_BAT_VOLT_MAX": (0.0, 25.2), "MOT_BAT_VOLT_MIN": (None, 19.8),
    }  # fmt: skip
    asked = run(reg, None, fp, MOT_BAT_VOLT_MAX=0)
    assert asked["option.question.batt_volt_max"].params["field"] == "battery_chemistry"


def pm(load_pct: float) -> pa.Table:
    t = np.arange(ARM, DISARM, 1_000_000)
    return table(
        t.tolist(), {"Load": (pa.uint16(), [int(load_pct * 10)] * len(t))},
        {"Load": {b"unit": b"%", b"multiplier": b"0.1"}},
    )  # fmt: skip


def test_notches_on_every_imu_need_processor_time(reg: Registry) -> None:
    fp = {"imu_count": {"value": 2}, "board": {"hal": "ChibiOS", "family": "H7"}}
    free = run(reg, {}, fp, {"PM": pm(30)}, INS_HNTCH_OPTS=4)["option.notch_all_imus.available"]
    assert changes(free) == {"INS_HNTCH_OPTS": (4.0, 12.0)}
    assert "option.notch_all_imus.available" not in run(
        reg, {}, fp, {"PM": pm(80)}, INS_HNTCH_OPTS=4
    )
    sitl = fp | {"board": {"hal": "SITL", "family": "SITL"}}
    assert "option.notch_all_imus.available" not in run(
        reg, {}, sitl, {"PM": pm(30)}, INS_HNTCH_OPTS=4
    )


def test_a_slow_main_loop_goes_back_to_400_hz_when_the_processor_has_room(reg: Registry) -> None:
    fp = {"board": {"hal": "ChibiOS", "family": "F7"}}
    free = run(reg, {}, fp, {"PM": pm(30)}, SCHED_LOOP_RATE=300)[
        "option.loop_rate_default.available"
    ]
    assert changes(free) == {"SCHED_LOOP_RATE": (300.0, 400.0)}
    # 60% at 300 Hz could be 80% at 400 Hz: no room
    assert "option.loop_rate_default.available" not in run(
        reg, {}, fp, {"PM": pm(60)}, SCHED_LOOP_RATE=300
    )
    assert "option.loop_rate_default.available" not in run(reg, {}, fp, {"PM": pm(30)})


@pytest.mark.parametrize(("firmware", "esc_type"), [("Bluejay", 4.0), ("BLHeli_32", 3.0)])
def test_esc_temperatures_over_bidirectional_dshot(
    reg: Registry, firmware: str, esc_type: float
) -> None:
    found = run(reg, {"esc_firmware": firmware}, None, None, SERVO_DSHOT_ESC=0)
    assert changes(found["option.edt_enable.available"]) == {"SERVO_DSHOT_ESC": (0.0, esc_type)}
    on = run(reg, {"esc_firmware": firmware}, None, None, SERVO_DSHOT_ESC=esc_type)
    assert "option.edt_enable.available" not in on
    asked = run(reg, {}, None, None, SERVO_DSHOT_ESC=0)
    assert "edt_enable" in asked["option.question.esc_edt_capable"].params["options"]


def test_the_escs_as_a_second_battery_monitor(reg: Registry) -> None:
    t = np.arange(ARM, DISARM, 100_000)
    cols = {"Instance": (pa.uint8(), [0] * len(t)), "Curr": (pa.float32(), [9.0] * len(t))}
    esc = table(t.tolist(), cols, {"Curr": {b"unit": b"A", b"multiplier": b"1.0"}})
    fp = {"board": {"hal": "ChibiOS", "family": "H7"}}
    found = run(reg, {}, fp, {"ESC": esc}, BATT2_MONITOR=0)
    assert changes(found["option.esc_battery_monitor.available"]) == {"BATT2_MONITOR": (0.0, 9.0)}
    taken = run(reg, {}, fp, {"ESC": esc}, BATT2_MONITOR=0, BATT_MONITOR=9)
    assert "option.esc_battery_monitor.available" not in taken
    assert "option.esc_battery_monitor.available" not in run(reg, {}, fp, {}, BATT2_MONITOR=0)


def test_options_rank_by_the_platforms_priority(reg: Registry) -> None:
    fp = {"board": {"hal": "ChibiOS", "family": "H7"}}
    answers = {"payload": "variable", "esc_firmware": "AM32", "priority": "agility"}
    params = QUAD | SET_UP | {"FSTRATE_ENABLE": 0.0, "MOT_HOVER_LEARN": 0.0}
    found = OPTIONS.run(hover_flight({"PM": pm(20)}, params, fp, answers), reg).findings
    assert [f.params["option"] for f in found] == ["fast_rate_loop", "hover_learn"]


# --- the heavy-lift fixture with its platform's answers ------------------------------------------


def test_the_heavy_lift_with_its_setup_answers(tmp_path: Path) -> None:
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
        out = run_analysis(store, cache, pack, res.flights[0])
    found = {f["code"]: f for f in out["findings"] if f["analyzer"] == "options"}
    assert {"option.hover_learn.available", "option.dshot_output.available"} <= set(found)
    learn = {c["param"]: c["proposed"] for c in found["option.hover_learn.available"]["changes"]}
    assert learn == {"MOT_HOVER_LEARN": 2.0}  # the planted fault: Callisto's own default of 0
