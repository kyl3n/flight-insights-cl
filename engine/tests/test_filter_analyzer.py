"""The filter analyzer: the tracking source the log supports, and the flags on an
existing notch setup, on synthetic flights and the simulator's heavy-lift pair."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import ARM, DISARM, esc, gyr, hover_flight, table

from flight_insights.analyzers import Finding, Registry, run_analysis
from flight_insights.analyzers.filters import FILTERS
from flight_insights.analyzers.motors import picture
from flight_insights.filters.search import Screen, search
from flight_insights.imports import Decision, commit, propose, scan
from flight_insights.knowledge import load_pack
from flight_insights.store import StoreKeys, open_store
from flight_insights.store.cache import ColumnCache

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
MOTORS = esc(9000)  # 150 Hz, clean telemetry
NOTCH = {  # an ESC-RPM notch set up for 150 Hz motors, no low-pass
    "INS_HNTCH_ENABLE": 1, "INS_HNTCH_MODE": 3, "INS_HNTCH_FREQ": 110, "INS_HNTCH_BW": 55,
    "INS_HNTCH_REF": 1, "INS_HNTCH_HMNCS": 1, "INS_HNTCH_OPTS": 32, "INS_GYRO_FILTER": 0,
}  # fmt: skip


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def run(
    tables: dict[str, pa.Table], reg: Registry, fp: dict[str, Any] | None = None, /, **params: float
) -> list[Finding]:
    p = {k: float(v) for k, v in params.items()}
    return FILTERS.run(hover_flight(tables, p, fp), reg).findings


def changes(f: Finding) -> dict[str, tuple[float | None, float | None]]:
    return {c.param: (c.current, c.proposed) for c in f.changes}


def throttle(value: float) -> pa.Table:
    t = np.arange(ARM, DISARM, 100_000)
    return table(t.tolist(), {"ThrOut": (pa.float32(), [value] * len(t))})


def test_a_well_set_esc_notch_raises_nothing(reg: Registry) -> None:
    assert run({"GYR": gyr([(150.0, 4.0)]), "ESC": MOTORS}, reg, **NOTCH) == []


def test_no_notch_with_clear_motor_noise_proposes_the_esc_notch(reg: Registry) -> None:
    [f] = run({"GYR": gyr([(150.0, 4.0)]), "ESC": MOTORS}, reg, INS_HNTCH_ENABLE=0)
    assert (f.code, f.params["mode"], f.params["peak_hz"]) == (
        "filter.no_notch",
        "ESC telemetry",
        pytest.approx(150.0, abs=1),
    )
    c = changes(f)
    assert c["INS_HNTCH_ENABLE"] == (0.0, 1.0) and c["INS_HNTCH_MODE"][1] == 3.0
    assert c["INS_HNTCH_FREQ"][1] == 110.0  # below the 150 Hz hover, above the low-pass
    assert c["INS_HNTCH_REF"][1] == 1.0 and c["INS_HNTCH_OPTS"][1] == 32.0  # bit 5: RPM loss


def test_a_throttle_notch_with_reliable_esc_rpm_should_follow_the_escs(reg: Registry) -> None:
    params = NOTCH | {"INS_HNTCH_MODE": 1, "INS_HNTCH_FREQ": 150, "INS_HNTCH_REF": 0.3}
    found = run({"GYR": gyr([(150.0, 4.0)]), "ESC": MOTORS, "MOTB": throttle(0.3)}, reg, **params)
    [f] = found
    assert f.code == "filter.better_source" and f.params == {
        "current": "throttle",
        "better": "ESC telemetry",
    }
    assert changes(f)["INS_HNTCH_MODE"] == (1.0, 3.0)


def test_a_throttle_notch_off_the_peak_is_refitted_without_rpm(reg: Registry) -> None:
    params = NOTCH | {
        "INS_HNTCH_MODE": 1,
        "INS_HNTCH_FREQ": 100,
        "INS_HNTCH_BW": 40,
        "INS_HNTCH_REF": 0.3,
    }
    fp = {"hover_throttle": {"value": 0.32}, "board": {"hal": "ChibiOS", "family": "F4"}}
    [f] = run({"GYR": gyr([(150.0, 4.0)]), "MOTB": throttle(0.3)}, reg, fp, **params)
    assert (f.code, f.severity) == ("filter.not_tracking", "out")  # 50 Hz off, BW 40
    assert f.params["centre_hz"] == pytest.approx(100.0) and f.params["peak_hz"] == pytest.approx(
        150, abs=1
    )
    assert changes(f) == {"INS_HNTCH_FREQ": (100.0, 150.0), "INS_HNTCH_REF": (0.3, 0.32)}


def test_a_helicopter_may_not_use_the_throttle_notch(reg: Registry) -> None:
    fp = {"frame": {"value": {"vehicle_class": "helicopter"}}}
    params = NOTCH | {"INS_HNTCH_MODE": 1, "INS_HNTCH_FREQ": 80, "INS_HNTCH_BW": 40}
    [f] = run({"GYR": gyr([(150.0, 4.0)]), "ESC": MOTORS}, reg, fp, **params)
    assert (f.code, f.severity) == ("filter.heli_throttle", "out")
    assert changes(f)["INS_HNTCH_MODE"] == (1.0, 0.0) and changes(f)["INS_HNTCH_FREQ"] == (
        80.0,
        150.0,
    )


def test_an_esc_floor_above_the_hover_frequency_is_lowered(reg: Registry) -> None:
    found = run(
        {"GYR": gyr([(150.0, 4.0)]), "ESC": MOTORS}, reg, **(NOTCH | {"INS_HNTCH_FREQ": 180})
    )
    f = next(x for x in found if x.code == "filter.freq_above_hover")
    assert f.params == {"freq": 180.0, "motor_hz": 150.0}
    assert changes(f) == {"INS_HNTCH_FREQ": (180.0, 110.0)}


def test_a_strong_harmonic_left_after_the_filters_is_added(reg: Registry) -> None:
    [f] = run({"GYR": gyr([(5.0, 2.0), (150.0, 4.0), (300.0, 2.0)]), "ESC": MOTORS}, reg, **NOTCH)
    assert f.code == "filter.harmonic_uncovered" and f.params["harmonic"] == 2
    assert changes(f) == {"INS_HNTCH_HMNCS": (1.0, 3.0)}


def test_a_harmonic_the_low_pass_already_removes_is_left_alone(reg: Registry) -> None:
    params = NOTCH | {"INS_GYRO_FILTER": 40}
    assert (
        run({"GYR": gyr([(5.0, 2.0), (150.0, 4.0), (300.0, 0.05)]), "ESC": MOTORS}, reg, **params)
        == []
    )


def test_a_bandwidth_wider_than_the_frequency_is_narrowed(reg: Registry) -> None:
    found = run({"GYR": gyr([(150.0, 4.0)]), "ESC": MOTORS}, reg, **(NOTCH | {"INS_HNTCH_BW": 200}))
    by_code = {f.code: f for f in found}
    assert set(by_code) == {"filter.bandwidth_wide", "filter.better_settings"}
    # The search's whole set carries the change; the flag explains it.
    assert by_code["filter.bandwidth_wide"].changes == ()
    old_bw, new_bw = changes(by_code["filter.better_settings"])["INS_HNTCH_BW"]
    assert old_bw == 200.0 and new_bw is not None and new_bw <= 55.0


def test_the_fft_notch_on_an_f4_board(reg: Registry) -> None:
    fp = {"hover_throttle": {"value": 0.3}, "board": {"hal": "ChibiOS", "family": "F4"}}
    params = NOTCH | {"INS_HNTCH_MODE": 4, "INS_HNTCH_FREQ": 150, "INS_HNTCH_BW": 75}
    found = run({"GYR": gyr([(150.0, 4.0)])}, reg, fp, **params)
    f = next(x for x in found if x.code == "filter.fft_weak_board")
    assert f.severity == "out" and changes(f)["INS_HNTCH_MODE"] == (4.0, 1.0)


def test_no_pre_filter_gyro_is_left_to_the_logging_analyzer(reg: Registry) -> None:
    r = FILTERS.run(hover_flight({"ESC": MOTORS}, {}), reg)
    assert r.skipped and not r.findings


# --- the simulator's heavy-lift pair -------------------------------------------------------------


def analysed(tmp_path: Path, name: str) -> dict[str, Any]:
    pack = load_pack()
    keys = StoreKeys.generate()
    with open_store(tmp_path / "fleet.db", keys, "e" * 32) as store:
        cache = ColumnCache(tmp_path / "cache", keys)
        batch = scan(store, pack, bytes(32), [FIXTURES / name])
        [res] = commit(store, pack, cache, batch, propose(store, pack, batch), [Decision("f1")])
        return run_analysis(store, cache, pack, res.flights[0])


def test_the_planted_throttle_notch_on_the_heavy_lift(tmp_path: Path) -> None:
    loaded = analysed(tmp_path, "copter-heavylift-loaded.bin")
    found = {f["code"]: f for f in loaded["findings"] if f["analyzer"] == "filters"}
    assert set(found) == {"filter.better_source", "filter.not_tracking", "filter.better_settings"}
    lag = found["filter.not_tracking"]["params"]
    assert lag["centre_hz"] == pytest.approx(201.8, abs=1)
    assert lag["peak_hz"] == pytest.approx(225, abs=2)
    better = found["filter.better_settings"]["params"]
    assert better["lag_new"] < better["lag_now"] - 10 and better["noise_change_db"] <= 0
    search = next(e for e in loaded["evidence"] if e["id"] == "filters:search")["values"]
    assert search["best"]["notch"]["MODE"] == 3.0  # the planted throttle notch becomes ESC RPM


def test_the_raw_imu_quad_is_set_up_well(tmp_path: Path) -> None:
    out = analysed(tmp_path, "copter-quad-rawimu.bin")
    assert not [f for f in out["findings"] if f["analyzer"] == "filters"]


def test_the_light_flight_only_needs_the_source_switch(tmp_path: Path) -> None:
    light = analysed(tmp_path, "copter-heavylift-light.bin")
    [f] = [f for f in light["findings"] if f["analyzer"] == "filters"]
    assert f["code"] == "filter.better_source"
    proposed = {c["param"]: c["proposed"] for c in f["changes"]}
    assert proposed["INS_HNTCH_MODE"] == 3.0 and proposed["INS_HNTCH_REF"] == 1.0


# --- the candidate search --------------------------------------------------------------------


def screen_for(tables: dict[str, pa.Table], **params: float) -> Screen:
    flight = hover_flight(tables, {k: float(v) for k, v in params.items()})
    m = picture(flight)
    assert m.spectrum is not None
    rules = Registry.load(load_pack()).rules["filters"]
    return Screen(m.spectrum, flight.params, tables, m.spans, "H7", 1, rules)


def test_notching_the_motors_frees_room_to_raise_the_low_pass() -> None:
    tables = {"GYR": gyr([(5.0, 2.0), (150.0, 4.0)]), "ESC": MOTORS}
    s = screen_for(tables, INS_HNTCH_ENABLE=0, INS_GYRO_FILTER=20)
    base = {"MODE": 3.0, "REF": 1.0, "FREQ": 110.0, "OPTS": 32.0}
    out = search(s, base, 1, 4, None)
    assert out.best is not None
    assert out.best.setting.gyro_filter > 20  # the payoff
    assert out.best.noise_db <= out.current.noise_db and out.best.lag_deg < out.current.lag_deg


def test_the_search_keeps_every_other_set_as_flown() -> None:
    # The search tunes the first set; fixed notches in the second and third sets stay in
    # every candidate's noise, lag and notch count.
    tables = {"GYR": gyr([(5.0, 2.0), (150.0, 4.0)]), "ESC": MOTORS}
    fixed = {"ENABLE": 1, "MODE": 0, "BW": 20, "ATT": 40, "HMNCS": 1, "REF": 0, "OPTS": 0}
    two = {f"INS_HNTC2_{k}": float(v) for k, v in (fixed | {"FREQ": 60}).items()}
    three = {f"INS_HNTC3_{k}": float(v) for k, v in (fixed | {"FREQ": 75}).items()}
    alone = screen_for(tables, INS_HNTCH_ENABLE=0, **two)
    both = screen_for(tables, INS_HNTCH_ENABLE=0, **two, **three)
    assert both.other_count == 2 * alone.other_count > 0
    assert both.other_lag > alone.other_lag > 0


def test_a_busy_processor_gets_no_more_notches() -> None:
    tables = {"GYR": gyr([(5.0, 2.0), (150.0, 4.0)]), "ESC": MOTORS}
    s = screen_for(tables, **(NOTCH | {"INS_GYRO_FILTER": 20}))
    base = {"MODE": 3.0, "REF": 1.0, "FREQ": 110.0, "OPTS": 32.0}
    busy = search(s, base, 1, 4, cpu_load_p95=85.0)
    assert busy.best is None or busy.best.notches <= busy.current.notches
