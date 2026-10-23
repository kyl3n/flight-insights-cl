"""Logging adequacy and system health analyzers, on flights built in memory, and the
log checks kept at import."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from flight_builder import flight, table

from flight_insights.analyzers import ANALYZERS, FlightData, Registry, run_analysis
from flight_insights.analyzers.log_adequacy import LOG_ADEQUACY
from flight_insights.analyzers.system import SYSTEM
from flight_insights.imports import Decision, commit, propose, scan
from flight_insights.knowledge import Pack, load_pack
from flight_insights.store import Store, StoreKeys, open_store
from flight_insights.store.cache import ColumnCache

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
ARM, DISARM = 10_000_000, 70_000_000


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


@pytest.fixture(scope="module")
def reg(pack: Pack) -> Registry:
    return Registry.load(pack)


def gyr(times: list[int]) -> pa.Table:
    return table(times, {"I": (pa.uint8(), [0] * len(times))})


def codes(r: Any) -> list[str]:
    return [f.code for f in r.findings]


# --- logging adequacy: gyro data --------------------------------------------------------


def test_no_raw_gyro_asks_for_pre_and_post_filter_logging(reg: Registry) -> None:
    r = LOG_ADEQUACY.run(flight({}, ARM, DISARM, {"INS_RAW_LOG_OPT": 0}), reg)
    [f] = r.findings
    assert (f.code, f.severity, f.params) == (
        "logging.no_prefilter_gyro",
        "caution",
        {"proposed": 9},
    )
    [change] = f.changes
    assert (change.param, change.current, change.proposed) == ("INS_RAW_LOG_OPT", 0, 9)
    assert f.evidence[0].values["gyr_samples"] == 0


def test_post_filter_only_logging_has_no_pre_filter_data(reg: Registry) -> None:
    samples = gyr(list(range(ARM, ARM + 1_000_000, 1000)))
    r = LOG_ADEQUACY.run(flight({"GYR": samples}, ARM, DISARM, {"INS_RAW_LOG_OPT": 4}), reg)
    assert codes(r) == ["logging.no_prefilter_gyro"]
    assert r.findings[0].changes[0].proposed == 4 | 9


def test_pre_filter_only_logging_cannot_check_the_prediction(reg: Registry) -> None:
    samples = gyr(list(range(ARM, ARM + 1_000_000, 1000)))
    r = LOG_ADEQUACY.run(flight({"GYR": samples}, ARM, DISARM, {"INS_RAW_LOG_OPT": 2}), reg)
    [f] = r.findings
    assert (f.code, f.severity, f.params["proposed"]) == ("logging.no_postfilter_gyro", "info", 11)


def test_the_batch_sampler_counts_as_pre_filter_data(reg: Registry) -> None:
    params = {"INS_RAW_LOG_OPT": 0, "INS_LOG_BAT_MASK": 1, "INS_LOG_BAT_OPT": 0}
    isbh = table([ARM], {"N": (pa.uint16(), [0])})
    r = LOG_ADEQUACY.run(flight({"ISBH": isbh}, ARM, DISARM, params), reg)
    assert codes(r) == []


def test_lost_gyro_samples_are_measured_against_those_logged(reg: Registry) -> None:
    samples = gyr(list(range(ARM, ARM + 990_000 * 10, 10)))  # 990,000 logged while armed
    hole = {"kind": "imu sample loss", "message": "GYR", "start_us": ARM + 5, "end_us": ARM + 9,
            "count": 10_000, "instance": 0, "boot": 0}  # fmt: skip
    gap = {"kind": "timestamp gap", "message": "all", "start_us": ARM + 2_000_000,
           "end_us": ARM + 3_500_000, "count": 1, "instance": None, "boot": 0}  # fmt: skip
    f = flight({"GYR": samples}, ARM, DISARM, {"INS_RAW_LOG_OPT": 9}, {"issues": [hole, gap]})
    r = LOG_ADEQUACY.run(f, reg)
    [loss] = [m for m in r.metrics if m.metric == "logging.imu_sample_loss"]
    assert loss.value == pytest.approx(1.0) and loss.zone == "out"
    lost = next(x for x in r.findings if x.code == "logging.samples_lost")
    assert lost.params == {"percent": 1.0, "count": 10_000}
    gap_finding = next(x for x in r.findings if x.code == "logging.gap")
    assert gap_finding.params == {"seconds": 1.5}


def test_a_flight_imported_before_log_checks_were_kept_has_no_loss_figure(reg: Registry) -> None:
    samples = gyr(list(range(ARM, ARM + 1_000_000, 1000)))
    r = LOG_ADEQUACY.run(flight({"GYR": samples}, ARM, DISARM, {"INS_RAW_LOG_OPT": 9}), reg)
    assert r.metrics == [] and r.findings == []


# --- logging adequacy: dropouts and missing messages -------------------------------------


def test_only_messages_dropped_while_armed_count(reg: Registry) -> None:
    t = [ARM - 2_000_000, ARM - 1_000_000, ARM + 1_000_000, ARM + 2_000_000, DISARM + 1_000_000]
    dsf = table(t, {"Dp": (pa.uint32(), [0, 40, 40, 65, 900])})  # 40 on the bench, 25 in flight
    r = LOG_ADEQUACY.run(flight({"DSF": dsf}, ARM, DISARM, {"INS_RAW_LOG_OPT": 9}), reg)
    [m] = r.metrics
    assert (m.metric, m.value, m.zone) == ("logging.dropped_messages", 25, "caution")
    assert next(f for f in r.findings if f.code == "logging.dropouts").params == {"count": 25}


def test_messages_left_out_by_the_log_bitmask_get_their_bits(reg: Registry) -> None:
    present = {n: table([ARM], {}) for n in ("VIBE", "IMU", "RATE", "BAT", "MOTB", "RCOU")}
    present["GYR"] = gyr([ARM + 1])
    params = {"INS_RAW_LOG_OPT": 9, "LOG_BITMASK": 1 | 2 | 128 | 512 | 1024 | 1 << 17}
    r = LOG_ADEQUACY.run(flight(present, ARM, DISARM, params), reg)
    [f] = r.findings
    assert f.code == "logging.missing_messages"
    assert f.params["messages"] == "PM" and "System Performance (bit 3)" in f.params["bits"]
    assert f.changes[0].proposed == params["LOG_BITMASK"] | 8


def test_a_message_missing_although_its_bit_is_set_is_not_a_logging_gap(reg: Registry) -> None:
    present = {n: table([ARM], {}) for n in ("VIBE", "IMU", "PM", "RATE", "MOTB", "RCOU")}
    present["GYR"] = gyr([ARM + 1])
    params = {"INS_RAW_LOG_OPT": 9, "LOG_BITMASK": 0xFFFFF}  # BAT missing: no battery monitor
    assert codes(LOG_ADEQUACY.run(flight(present, ARM, DISARM, params), reg)) == []


def test_only_mapped_vehicles_are_checked_for_their_log_bits(reg: Registry) -> None:
    fp: dict[str, Any] = {"vehicle": "plane", "board": {"hal": "ChibiOS"}, "hover_windows": []}
    params = {"INS_RAW_LOG_OPT": 9, "LOG_BITMASK": 0}
    f = flight({"GYR": gyr([ARM + 1])}, ARM, DISARM, params, fingerprint=fp)
    assert codes(LOG_ADEQUACY.run(f, reg)) == []


# --- system health: loop timing ---------------------------------------------------------

PM_META = {"Load": {b"unit": b"%", b"multiplier": b"0.1"}}


def pm(rows: list[tuple[int, int, int, int]], rate: int = 400, **extra: list[int]) -> pa.Table:
    """Performance records at 1 s steps from just before arming: (NLon, NL, MaxT, Load)."""
    t = [ARM - 500_000 + k * 1_000_000 for k in range(len(rows))]
    cols: dict[str, tuple[pa.DataType, list[int]]] = {
        "LR": (pa.uint16(), [rate] * len(rows)),
        "NLon": (pa.uint16(), [r[0] for r in rows]),
        "NL": (pa.uint16(), [r[1] for r in rows]),
        "MaxT": (pa.uint32(), [r[2] for r in rows]),
        "Mem": (pa.uint32(), [65536] * len(rows)),
        "Load": (pa.uint16(), [r[3] for r in rows]),
    }
    for name, values in extra.items():
        cols[name] = (pa.uint32(), values)
    return table(t, cols, PM_META)


def test_slow_loops_are_counted_after_the_arming_record(reg: Registry) -> None:
    rows = [(0, 400, 2400, 300), (400, 400, 90_000, 900)] + [(80, 400, 2900, 700)] * 5
    r = SYSTEM.run(flight({"PM": pm(rows)}, ARM, ARM + 6_000_000), reg)
    slow = next(m for m in r.metrics if m.metric == "system.slow_loops")
    assert slow.value == pytest.approx(20.0) and slow.zone == "out"  # the arming record is left out
    longest = next(m for m in r.metrics if m.metric == "system.max_loop_time")
    assert longest.value == pytest.approx(116.0) and longest.zone == "caution"
    f = next(x for x in r.findings if x.code == "system.slow_loops")
    assert f.params == {"percent": 20.0, "load": 70.0}
    assert r.evidence[0].values["records"] == 5


def test_one_long_loop_is_reported_with_less_confidence(reg: Registry) -> None:
    rows = [(0, 400, 2400, 300)] * 2 + [(0, 400, 2400, 300)] * 3 + [(1, 400, 3750, 300)]
    r = SYSTEM.run(flight({"PM": pm(rows)}, ARM, ARM + 6_000_000), reg)
    [f] = r.findings
    assert (f.code, f.severity, f.confidence) == ("system.long_loop", "out", "medium")
    assert f.params == {"percent": 150, "over": 1, "records": 4}


def test_simulator_timing_is_measured_but_not_judged(reg: Registry) -> None:
    rows = [(0, 400, 2400, 0)] + [(400, 400, 9000, 0)] * 5
    fp: dict[str, Any] = {"vehicle": "copter", "board": {"hal": "SITL"}, "hover_windows": []}
    r = SYSTEM.run(flight({"PM": pm(rows)}, ARM, ARM + 6_000_000, fingerprint=fp), reg)
    assert [m.zone for m in r.metrics] == [None, None] and r.findings == []
    assert r.evidence[0].values["simulated"] is True


# --- system health: errors and events ---------------------------------------------------


def test_internal_errors_are_named_from_the_release(reg: Registry) -> None:
    rows = [(0, 400, 2400, 0)] * 4
    mask = 1 << 11 | 1 << 26
    t = pm(rows, InE=[0, mask, mask, mask], ErC=[0, 2, 2, 2], ErrL=[0, 120, 120, 120])
    [f] = SYSTEM.run(flight({"PM": t}, ARM, ARM + 3_000_000), reg).findings
    assert (f.code, f.severity) == ("system.internal_error", "out")
    assert f.params == {"errors": "watchdog reset, mem guard"}
    assert f.evidence[0].values["count"] == 2


def err(rows: list[tuple[int, int, int]]) -> pa.Table:
    """ERR rows: (TimeUS, Subsys, ECode)."""
    return table(
        [r[0] for r in rows],
        {"Subsys": (pa.uint8(), [r[1] for r in rows]), "ECode": (pa.uint8(), [r[2] for r in rows])},
    )


def test_errors_in_flight_become_failsafes_crashes_and_errors(reg: Registry) -> None:
    rows = [
        (ARM - 1_000_000, 6, 1),  # battery failsafe on the bench: not this flight's
        (ARM + 4_000_000, 6, 1),  # battery failsafe
        (ARM + 6_000_000, 6, 0),  # ... resolved
        (ARM + 7_000_000, 6, 1),
        (ARM + 8_000_000, 11, 2),  # GPS glitch
        (ARM + 9_000_000, 19, 1),  # Copter's CPU failsafe
        (ARM + 9_500_000, 25, 1),  # thrust loss
        (ARM + 9_600_000, 12, 2),  # loss of control
    ]
    r = SYSTEM.run(flight({"ERR": err(rows)}, ARM, DISARM), reg)
    found = {(f.code, f.severity, tuple(sorted(f.params.items()))) for f in r.findings}
    assert found == {
        ("system.failsafe", "out", (("count", 2), ("first_s", 4.0), ("subsystem", "batt"))),
        ("system.error", "caution",
         (("code", "gps glitch"), ("count", 1), ("first_s", 8.0), ("subsystem", "gps"))),
        ("system.failsafe", "out", (("count", 1), ("first_s", 9.0), ("subsystem", "cpu"))),
        ("system.crash_check", "safety", (("first_s", 9.5), ("what", "thrust loss check"))),
        ("system.crash_check", "safety",
         (("first_s", 9.6), ("what", "crash check loss of control"))),
    }  # fmt: skip


def test_an_emergency_stop_in_flight_is_a_safety_finding(reg: Registry) -> None:
    ev = table([ARM, ARM + 5_000_000, DISARM], {"Id": (pa.uint8(), [10, 54, 11])})
    [f] = SYSTEM.run(flight({"EV": ev}, ARM, DISARM), reg).findings
    assert (f.code, f.severity, f.params) == ("system.emergency_stop", "safety", {"first_s": 5.0})


def test_a_log_without_pm_err_or_ev_is_skipped(reg: Registry) -> None:
    r = SYSTEM.run(flight({}, ARM, DISARM), reg)
    assert r.skipped and not r.metrics and not r.findings


# --- the log checks kept at import -------------------------------------------------------


def test_import_keeps_each_flights_log_checks_for_the_analyzers(tmp_path: Path, pack: Pack) -> None:
    keys = StoreKeys.generate()
    with open_store(tmp_path / "fleet.db", keys, "e" * 32) as store:
        cache = ColumnCache(tmp_path / "cache", keys)
        fid = _import(store, pack, cache, FIXTURES / "copter-quad-rawimu.bin")
        flight_data = FlightData.load(store, cache, pack, fid)
        assert flight_data.validation is not None
        for issue in flight_data.validation["issues"]:
            assert issue["start_us"] <= flight_data.disarm_us
            assert issue["end_us"] >= flight_data.arm_us
        out = run_analysis(store, cache, pack, fid)
        assert set(out["analyzers"]) == {a.name for a in ANALYZERS}
        assert out["status"] == "complete"
        timing = {m["metric"]: m for m in out["metrics"]}
        assert timing["system.slow_loops"]["zone"] is None  # flown in the simulator


def _import(store: Store, pack: Pack, cache: ColumnCache, log: Path) -> str:
    batch = scan(store, pack, bytes(32), [log])
    [result] = commit(store, pack, cache, batch, propose(store, pack, batch), [Decision("f1")])
    [fid] = result.flights
    return fid
