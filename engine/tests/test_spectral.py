"""The spectral core and the ESC RPM analyzer: synthetic signals with known answers,
and the simulator's heavy-lift fixture."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import flight, table

from flight_insights.analyzers import FlightData, Registry, run_analysis
from flight_insights.analyzers.esc_rpm import ESC_RPM
from flight_insights.imports import Decision, commit, propose, scan
from flight_insights.knowledge import Pack, load_pack
from flight_insights.spectral import harmonic_of, layout, peaks, quality, runs, welch
from flight_insights.store import StoreKeys, open_store
from flight_insights.store.cache import ColumnCache

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
RATE = 1000.0
ARM, DISARM = 1_000_000, 40_000_000
HOVER = (5_000_000, 35_000_000)
RAD = {b"unit": b"rad/s", b"multiplier": b"1.0"}


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def gyr(
    tones: list[tuple[float, float]],
    seconds: float = 30.0,
    drop: slice | None = None,
    instance: int = 0,
    start_us: int = HOVER[0],
) -> pa.Table:
    """GYR at 1 kHz: sines of (Hz, amplitude in deg/s) on X and Y, with noise on Z."""
    n = int(seconds * RATE)
    clock = start_us + np.arange(n, dtype=np.int64) * 1000
    t = (clock - start_us) / 1e6
    x = sum(a * np.sin(2 * np.pi * f * t) for f, a in tones) * np.pi / 180
    z = np.random.default_rng(1).normal(0, 0.001, n)
    keep = np.ones(n, bool)
    if drop is not None:
        keep[drop] = False
    cols = {
        "I": (pa.uint8(), [instance] * int(keep.sum())),
        "SampleUS": (pa.uint64(), clock[keep].tolist()),
        "GyrX": (pa.float32(), np.asarray(x)[keep].tolist()),
        "GyrY": (pa.float32(), np.asarray(x)[keep].tolist()),
        "GyrZ": (pa.float32(), z[keep].tolist()),
    }
    meta = {a: RAD for a in ("GyrX", "GyrY", "GyrZ")}
    return table(clock[keep].tolist(), cols, meta)


def esc(
    rpm: float, motors: int = 4, err: float = 0.0, gap: tuple[int, int] | None = None
) -> pa.Table:
    """ESC telemetry every 10 ms for each motor between arming and disarming."""
    t = np.arange(ARM, DISARM, 10_000)
    if gap is not None:
        t = t[(t < gap[0]) | (t > gap[1])]
    rows = [(int(x), m) for x in t for m in range(motors)]
    meta = {"RPM": {b"unit": b"rpm", b"multiplier": b"1.0"}, "Err": {b"unit": b"%"}}
    return table(
        [r[0] for r in rows],
        {
            "Instance": (pa.uint8(), [r[1] for r in rows]),
            "RPM": (pa.float32(), [rpm] * len(rows)),
            "Err": (pa.float32(), [err] * len(rows)),
        },
        meta,
    )


def hover_flight(tables: dict[str, pa.Table], params: dict[str, float] | None = None) -> FlightData:
    fp = {
        "vehicle": "copter",
        "board": {"hal": "ChibiOS"},
        "imu_count": {"value": 1},
        "hover_windows": [{"start_us": HOVER[0], "end_us": HOVER[1]}],
    }
    return flight(tables, ARM, DISARM, {"INS_RAW_LOG_OPT": 1, **(params or {})}, fingerprint=fp)


# --- the library --------------------------------------------------------------------------


def test_a_sines_power_lands_in_its_band() -> None:
    g = gyr([(150.0, 4.0)])
    s = welch(runs(g, 0, *HOVER))
    assert s is not None and s.rate_hz == pytest.approx(RATE) and s.resolution_hz < 1.0
    power = s.band_power(145, 155)
    assert power[0] == pytest.approx(4.0**2 / 2, rel=0.02)  # a sine's mean square, (deg/s)^2
    assert s.band_power(0, 140)[0] < 1e-3


def test_a_lost_sample_splits_the_run_and_no_window_spans_it() -> None:
    g = gyr([(150.0, 4.0)], drop=slice(10_000, 10_003))
    rs = runs(g, 0, *HOVER)
    assert [len(r.samples) for r in rs] == [10_000, 19_997]
    whole = welch(runs(gyr([(150.0, 4.0)]), 0, *HOVER))
    split = welch(rs)
    assert whole is not None and split is not None and split.windows < whole.windows
    assert split.band_power(145, 155)[0] == pytest.approx(8.0, rel=0.02)


def test_peaks_and_harmonics() -> None:
    s = welch(runs(gyr([(120.0, 3.0), (240.0, 1.0), (333.0, 0.5)]), 0, *HOVER))
    assert s is not None
    found = peaks(s, 50, 500)
    assert [round(p.freq_hz) for p in found] == [120, 240, 333]
    assert [harmonic_of(p.freq_hz, 120.0) for p in found] == [1, 2, None]


def test_the_raw_log_options_say_which_instances_are_pre_filter() -> None:
    assert layout(9, 2, [0, 1, 2, 3]).pre == (0, 1)
    assert layout(9, 2, [0, 1, 2, 3]).post == {0: 2, 1: 3}
    assert layout(1, 2, [0]).pre == (0,) and layout(4, 2, [0]).pre == ()


def test_esc_quality_counts_errors_gaps_and_zero_readings() -> None:
    [q0, q1] = quality(
        esc(9000, motors=2, err=0.5, gap=(10_000_000, 10_200_000)), ARM, DISARM, [HOVER]
    )
    assert q0.error_pct == pytest.approx(0.5) and q0.dropouts == 1 and q0.median_hz == 150.0
    assert q0.reports_rpm and q1.zero_rpm == 0
    [none] = quality(esc(0, motors=1), ARM, DISARM, [HOVER])
    assert not none.reports_rpm and none.zero_rpm == 0 and np.isnan(none.median_hz)


# --- the ESC RPM analyzer --------------------------------------------------------------------


def test_rpm_that_matches_the_gyro_peak_raises_nothing(reg: Registry) -> None:
    f = hover_flight({"GYR": gyr([(150.0, 4.0)]), "ESC": esc(9000)})
    r = ESC_RPM.run(f, reg)
    assert r.findings == []
    [ev] = [e for e in r.evidence if e.id == "esc:gyro"]
    assert ev.values["peaks"][0]["harmonic"] == 1


def test_a_dominant_second_harmonic_still_matches(reg: Registry) -> None:
    f = hover_flight({"GYR": gyr([(300.0, 4.0), (150.0, 1.0)]), "ESC": esc(9000)})
    assert ESC_RPM.run(f, reg).findings == []


def test_a_wrong_pole_count_shows_as_a_ratio_and_gets_its_fix(reg: Registry) -> None:
    # 12-pole motors with 14 poles set: ESC RPM reads 12/14 of the true speed.
    params = {"SERVO_BLH_POLES": 14.0, "MOT_PWM_TYPE": 6.0}
    f = hover_flight({"GYR": gyr([(150.0, 4.0)]), "ESC": esc(9000 * 12 / 14)}, params)
    [finding] = ESC_RPM.run(f, reg).findings
    assert (finding.code, finding.severity, finding.confidence) == (
        "esc.rpm_mismatch",
        "out",
        "high",
    )
    assert finding.params["ratio"] == pytest.approx(14 / 12, abs=0.01)
    [change] = finding.changes
    assert (change.param, change.current, change.proposed) == ("SERVO_BLH_POLES", 14, 12.0)


def test_telemetry_errors_and_dropouts_are_findings(reg: Registry) -> None:
    f = hover_flight({"ESC": esc(9000, err=2.5, gap=(10_000_000, 10_200_000))})
    r = ESC_RPM.run(f, reg)
    metrics = {m.metric: (m.value, m.zone) for m in r.metrics}
    assert metrics["esc.telemetry_error_rate"] == (pytest.approx(2.5), "out")
    assert metrics["esc.telemetry_dropouts"] == (4.0, "caution")  # one gap on each ESC
    codes = {f.code: f.params for f in r.findings}
    assert codes["esc.telemetry_errors"] == {"percent": 2.5, "esc": 1}
    assert codes["esc.telemetry_dropouts"] == {"count": 4, "escs": "1, 2, 3, 4"}


def test_no_esc_telemetry_is_skipped(reg: Registry) -> None:
    r = ESC_RPM.run(hover_flight({}), reg)
    assert r.skipped and not r.metrics


# --- on the simulator's heavy-lift X8 ------------------------------------------------------------


def test_on_the_loaded_heavy_lift_rpm_and_gyro_agree(tmp_path: Path) -> None:
    pack: Pack = load_pack()
    keys = StoreKeys.generate()
    with open_store(tmp_path / "fleet.db", keys, "e" * 32) as store:
        cache = ColumnCache(tmp_path / "cache", keys)
        batch = scan(store, pack, bytes(32), [FIXTURES / "copter-heavylift-loaded.bin"])
        [res] = commit(store, pack, cache, batch, propose(store, pack, batch), [Decision("f1")])
        out = run_analysis(store, cache, pack, res.flights[0])
    assert not [f for f in out["findings"] if f["analyzer"] == "esc_rpm"]
    gyro = next(e for e in out["evidence"] if e["id"] == "esc:gyro")
    top = gyro["values"]["peaks"][0]
    assert top["harmonic"] == 1 and top["hz"] == pytest.approx(225, abs=2)  # 8 kg on board
    telemetry = next(e for e in out["evidence"] if e["id"] == "esc:telemetry")
    assert len(telemetry["values"]["escs"]) == 8
