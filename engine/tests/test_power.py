"""The power analyzer, and the registry's multi-interval and baseline zones."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import ARM, DISARM, hover_flight, table

from flight_insights.analyzers import Registry, run_analysis
from flight_insights.analyzers.battery_power import energy_wh, supply
from flight_insights.analyzers.power import POWER
from flight_insights.imports import Decision, commit, propose, scan
from flight_insights.knowledge import load_pack
from flight_insights.store import StoreKeys, open_store
from flight_insights.store.cache import ColumnCache

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
V = {b"unit": b"V", b"multiplier": b"1.0"}
A = {b"unit": b"A", b"multiplier": b"1.0"}
OHM = {b"unit": b"Ohm", b"multiplier": b"1.0"}


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def battery(resting: float = 50.0, hover_a: float = 40.0, ohm: float = 0.02) -> pa.Table:
    """BAT at 10 Hz: resting before arming, then hover current, with one 80 A peak."""
    t = np.arange(0, DISARM, 100_000)
    amps = np.where(t < ARM, 0.0, hover_a)
    amps[len(amps) // 2] = 80.0
    volts = resting - amps * ohm
    return table(
        t.tolist(),
        {
            "Inst": (pa.uint8(), [0] * len(t)),
            "Volt": (pa.float32(), volts.tolist()),
            "Curr": (pa.float32(), amps.tolist()),
            "Res": (pa.float32(), [ohm] * len(t)),
        },
        {"Volt": V, "Curr": A, "Res": OHM},
    )


def monitors(*packs: tuple[int, float, float, float]) -> pa.Table:
    """BAT rows for several monitors at 10 Hz, interleaved as the firmware logs them: each
    (instance, resting volts, hover amps, ohms)."""
    t = np.arange(0, DISARM, 100_000)
    rows: list[tuple[int, int, float, float, float]] = []
    for x in t:
        for inst, resting, hover_a, ohm in packs:
            amps = 0.0 if x < ARM else hover_a
            rows.append((int(x) + 1000 * inst, inst, resting - amps * ohm, amps, ohm))
    return table(
        [r[0] for r in rows],
        {
            "Inst": (pa.uint8(), [r[1] for r in rows]),
            "Volt": (pa.float32(), [r[2] for r in rows]),
            "Curr": (pa.float32(), [r[3] for r in rows]),
            "Res": (pa.float32(), [r[4] for r in rows]),
        },
        {"Volt": V, "Curr": A, "Res": OHM},
    )


def test_parallel_packs_each_with_a_monitor_are_summed(reg: Registry) -> None:
    """Two packs sharing the load, each on its own monitor (BATT_ and BATT2_ type 4): the
    first alone would read half the power."""
    tables = {"BAT": monitors((0, 50.0, 20.0, 0.04), (1, 50.0, 20.0, 0.04))}
    params: dict[str, float] = {"BATT_MONITOR": 4, "BATT2_MONITOR": 4}
    r = POWER.run(hover_flight(tables, params), reg)
    got = metrics(r)
    pack_w = (50 - 20 * 0.04) * 20
    assert got["power.hover_power"][0] == pytest.approx(2 * pack_w, rel=1e-5)
    assert got["power.peak_current"][0] == pytest.approx(40.0, rel=1e-5)
    assert got["power.battery_loss"][0] == pytest.approx(2 * 20**2 * 0.04, rel=1e-4)
    assert got["power.battery_resistance"][0] == pytest.approx(40.0, rel=1e-4)  # the first's
    [ev] = [e for e in r.evidence if e.id == "power:battery"]
    assert ev.values["batteries"] == [0, 1] and ev.values["read_from"] == "packs"


@pytest.mark.parametrize(
    ("kinds", "used", "how"),
    [({"BATT_MONITOR": 4, "BATT2_MONITOR": 4, "BATT3_MONITOR": 10}, (0, 1), "packs"),
     ({"BATT_MONITOR": 10, "BATT2_MONITOR": 4, "BATT3_MONITOR": 4}, (1, 2), "packs"),
     ({"BATT_MONITOR": 9, "BATT2_MONITOR": 4, "BATT3_MONITOR": 3}, (1,), "packs"),
     ({"BATT_MONITOR": 10, "BATT2_MONITOR": 9, "BATT3_MONITOR": 3}, (0,), "sum"),
     ({"BATT_MONITOR": 3, "BATT2_MONITOR": 9, "BATT3_MONITOR": 11}, (1,), "esc"),
     ({}, (0, 1, 2), "packs")],
)  # fmt: skip
def test_nothing_is_counted_twice(
    reg: Registry, kinds: dict[str, float], used: tuple[int, ...], how: str
) -> None:
    """A sum monitor (10) or ESC monitor (9) repeats packs already counted; a voltage-only
    (3) or fuel (11) monitor measures no pack's current. Unnamed monitors count."""
    tables = {"BAT": monitors((0, 50.0, 20.0, 0.04), (1, 50.0, 20.0, 0.04),
                              (2, 50.0, 20.0, 0.04))}  # fmt: skip
    s = supply(hover_flight(tables, kinds), reg.rules["batteries"])
    assert s is not None and (s.instances, s.how) == (used, how)


def test_energy_is_the_trapezoid_over_every_sample() -> None:
    t = np.array([0, 1_000_000, 2_000_000, 5_000_000, 6_000_000], np.int64)
    p = np.array([100.0, 200.0, 200.0, 500.0, 500.0])
    wh, bridged = energy_wh(t, p, 0, 6_000_000, 1.0)
    assert wh == pytest.approx((150 + 200 + 3 * 350 + 500) / 3600)
    assert bridged == pytest.approx(3.0)  # the 3 s with no row
    assert energy_wh(t, p, 1_000_000, 2_000_000, 1.0) == (pytest.approx(200 / 3600), 0.0)


def board(vcc: list[float]) -> pa.Table:
    t = np.linspace(ARM, DISARM, len(vcc)).astype(np.int64)
    return table(t.tolist(), {"Vcc": (pa.float32(), vcc)}, {"Vcc": V})


def escs(volt: float, amps: float, motors: int = 4) -> pa.Table:
    t = np.arange(ARM, DISARM, 100_000)
    rows = [(int(x), m) for x in t for m in range(motors)]
    return table(
        [r[0] for r in rows],
        {
            "Instance": (pa.uint8(), [r[1] for r in rows]),
            "Volt": (pa.float32(), [volt] * len(rows)),
            "Curr": (pa.float32(), [amps] * len(rows)),
        },
        {"Volt": V, "Curr": A},
    )


def metrics(r: Any) -> dict[str, tuple[float, str | None]]:
    return {m.metric: (m.value, m.zone) for m in r.metrics}


def test_hover_power_resistance_and_sag(reg: Registry) -> None:
    r = POWER.run(hover_flight({"BAT": battery()}, {}), reg)
    got = metrics(r)
    assert got["power.hover_power"][0] == pytest.approx((50 - 40 * 0.02) * 40)
    assert got["power.hover_power"][1] is None  # judged against the airframe's history
    assert got["power.battery_resistance"][0] == pytest.approx(20.0)  # mOhm
    assert got["power.battery_loss"][0] == pytest.approx(40**2 * 0.02)
    assert got["power.peak_current"][0] == pytest.approx(80.0)
    [ev] = [e for e in r.evidence if e.id == "power:battery"]
    assert ev.values["sag_v"] == pytest.approx(80 * 0.02)


def test_the_power_split_needs_real_esc_telemetry(reg: Registry) -> None:
    tables = {"BAT": battery(), "ESC": escs(48.8, 9.0)}
    got = metrics(POWER.run(hover_flight(tables, {}), reg))
    total = (50 - 40 * 0.02) * 40
    assert got["power.propulsion"][0] == pytest.approx(4 * 48.8 * 9.0, rel=1e-4)
    assert got["power.avionics"][0] == pytest.approx(total - 4 * 48.8 * 9.0, rel=1e-3)
    assert got["power.harness_loss"][0] == pytest.approx((49.2 - 48.8) * 40, rel=1e-3)
    sim = {"board": {"hal": "SITL", "family": "SITL"}}
    simulated = metrics(POWER.run(hover_flight(tables, {}, sim), reg))
    assert "power.propulsion" not in simulated  # SITL's ESC volts and amps are placeholders


@pytest.mark.parametrize(
    ("throttle", "zone", "code"),
    [(0.45, "target", None), (0.7, "caution", "power.hover_throttle_high"),
     (0.15, "caution", "power.hover_throttle_low")],
)  # fmt: skip
def test_hover_throttle_has_a_band_on_both_sides(
    reg: Registry, throttle: float, zone: str, code: str | None
) -> None:
    fp = {"hover_throttle": {"value": throttle}}
    r = POWER.run(hover_flight({"BAT": battery()}, {}, fp), reg)
    assert metrics(r)["power.hover_throttle"] == (pytest.approx(throttle), zone)
    assert [f.code for f in r.findings] == ([code] if code else [])


def test_board_voltage_outside_the_pre_arm_range(reg: Registry) -> None:
    r = POWER.run(hover_flight({"POWR": board([5.1] * 50 + [4.1] + [5.1] * 50)}, {}), reg)
    got = metrics(r)
    assert got["power.board_voltage_min"] == (pytest.approx(4.1), "out")
    [f] = r.findings
    assert (f.code, f.severity, f.params["lowest"]) == ("power.board_voltage", "out", 4.1)


def test_a_dip_under_load_is_worth_a_look(reg: Registry) -> None:
    r = POWER.run(hover_flight({"POWR": board([5.2] * 50 + [4.8] + [5.2] * 50)}, {}), reg)
    got = metrics(r)
    assert got["power.board_voltage_min"][1] == "target"
    assert got["power.board_voltage_dip"] == (pytest.approx(0.4), "caution")
    assert [f.severity for f in r.findings] == ["caution"]


def test_nothing_to_judge_is_skipped(reg: Registry) -> None:
    r = POWER.run(hover_flight({}, {}, {"hover_windows": []}), reg)
    assert r.skipped


def test_the_loaded_heavy_lift_needs_more_power(tmp_path: Path) -> None:
    pack = load_pack()
    power: dict[str, dict[str, float]] = {}
    for name in ("copter-heavylift-light.bin", "copter-heavylift-loaded.bin"):
        keys = StoreKeys.generate()
        with open_store(tmp_path / f"{name}.db", keys, "e" * 32) as store:
            cache = ColumnCache(tmp_path / f"{name}-cache", keys)
            batch = scan(store, pack, bytes(32), [FIXTURES / name])
            [res] = commit(store, pack, cache, batch, propose(store, pack, batch), [Decision("f1")])
            out = run_analysis(store, cache, pack, res.flights[0])
        power[name] = {
            m["metric"]: m["value"] for m in out["metrics"] if m["segment"] in ("hover", "flight")
        }
    light, loaded = power["copter-heavylift-light.bin"], power["copter-heavylift-loaded.bin"]
    assert loaded["power.hover_power"] > 1.3 * light["power.hover_power"]  # 8 kg more
    assert 0.2 <= light["power.hover_throttle"] < loaded["power.hover_throttle"] < 0.6


def test_zones_can_be_several_intervals_or_none(reg: Registry) -> None:
    throttle = reg.metrics["power.hover_throttle"]
    assert [throttle.zone(v) for v in (0.1, 0.3, 0.9)] == ["caution", "target", "caution"]
    assert reg.metrics["power.hover_power"].zone(3000.0) is None


# --- the thrust model and the battery monitor ----------------------------------------------


def test_a_stale_hover_thrust_with_learning_off(reg: Registry) -> None:
    fp = {"hover_throttle": {"value": 0.42}}
    params = {"MOT_HOVER_LEARN": 0.0, "MOT_THST_HOVER": 0.30}
    r = POWER.run(hover_flight({"BAT": battery()}, params, fp), reg)
    [f] = [x for x in r.findings if x.code == "power.thst_hover_stale"]
    assert f.params == {"value": 0.3, "throttle": 0.42}
    assert [(c.param, c.proposed) for c in f.changes] == [("MOT_THST_HOVER", 0.42)]
    learning = POWER.run(
        hover_flight({"BAT": battery()}, params | {"MOT_HOVER_LEARN": 2.0}, fp), reg
    )
    assert not [x for x in learning.findings if x.code == "power.thst_hover_stale"]


def _quiet_battery(volt_scale: float) -> pa.Table:
    """BAT with a quiet stretch after arming (2 A), then a 40 A hover."""
    t = np.arange(ARM, DISARM, 100_000)
    amps = np.where(t < ARM + 3_000_000, 2.0, 40.0)
    volts = (50.0 - amps * 0.02) * volt_scale
    return table(
        t.tolist(),
        {"Inst": (pa.uint8(), [0] * len(t)), "Volt": (pa.float32(), volts.tolist()),
         "Curr": (pa.float32(), amps.tolist())},
        {"Volt": V, "Curr": A},
    )  # fmt: skip


def test_the_battery_monitor_against_the_escs(reg: Registry) -> None:
    # four ESCs drawing 12 A each is 48 A: more than the 40 A the monitor reports
    over = POWER.run(hover_flight({"BAT": _quiet_battery(1.0), "ESC": escs(49.9, 12.0)}, {}), reg)
    [f] = [x for x in over.findings if x.code == "power.monitor_vs_escs"]
    assert f.params == {"what": "current"} and f.changes == ()
    # the monitor reads 6% high while little current flows
    high = POWER.run(hover_flight({"BAT": _quiet_battery(1.06), "ESC": escs(49.9, 9.0)}, {}), reg)
    [g] = [x for x in high.findings if x.code == "power.monitor_vs_escs"]
    assert g.params == {"what": "voltage"}
    agree = POWER.run(hover_flight({"BAT": _quiet_battery(1.0), "ESC": escs(49.9, 9.0)}, {}), reg)
    assert not [x for x in agree.findings if x.code == "power.monitor_vs_escs"]
