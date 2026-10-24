"""ArduPilot's gyro filters modelled: biquad responses, notch placement, tracking,
the sample-by-sample simulation, and the model checked against the simulator's own
post-filter gyro data."""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import flight, table

from flight_insights.analyzers import Registry
from flight_insights.analyzers.filter_model import FILTER_MODEL
from flight_insights.cli import main
from flight_insights.dataflash.log import read_dataflash
from flight_insights.filters import (
    HarmonicNotch,
    NotchParams,
    biquads,
    chain,
    check,
    lowpass,
    notch,
    notch_a_q,
    simulate,
    sources,
)
from flight_insights.filters.corpus import TABLES
from flight_insights.filters.tracking import logged_inputs
from flight_insights.fingerprint import fingerprint
from flight_insights.history import from_log
from flight_insights.knowledge import load_pack
from flight_insights.spectral.gyro import Run

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
RATE = 1000.0


def params(**changes: float) -> dict[str, float]:
    base = {
        "INS_HNTCH_ENABLE": 1, "INS_HNTCH_MODE": 1, "INS_HNTCH_FREQ": 100, "INS_HNTCH_BW": 50,
        "INS_HNTCH_ATT": 40, "INS_HNTCH_HMNCS": 1, "INS_HNTCH_REF": 0.25,
        "INS_HNTCH_OPTS": 0, "INS_HNTCH_FM_RAT": 1.0, "INS_GYRO_FILTER": 0,
    }  # fmt: skip
    return {k: float(v) for k, v in {**base, **changes}.items()}


def notch_of(**changes: float) -> HarmonicNotch:
    return HarmonicNotch(NotchParams.read(params(**changes), "INS_HNTCH_"), RATE, "H7")


# --- biquads -------------------------------------------------------------------------------


def test_the_low_pass_is_a_butterworth_held_under_40_percent_of_the_rate() -> None:
    lp = lowpass(RATE, 50.0)
    assert lp is not None
    h = np.abs(lp.response(np.array([0.0, 50.0]), RATE))
    assert h[0] == pytest.approx(1.0) and 20 * math.log10(h[1]) == pytest.approx(-3.01, abs=0.01)
    assert lowpass(RATE, 0.0) is None  # 0 passes the gyro through
    capped = lowpass(RATE, 900.0)
    assert capped == lowpass(RATE, 400.0)


def test_a_notch_takes_its_attenuation_at_its_centre() -> None:
    a, q = notch_a_q(100.0, 50.0, 40.0)
    f = notch(RATE, 100.0, a, q)
    assert f is not None
    h = np.abs(f.response(np.array([0.0, 100.0, 499.0]), RATE))
    assert 20 * math.log10(h[1]) == pytest.approx(-40.0, abs=0.01)
    assert h[0] == pytest.approx(1.0) and h[2] == pytest.approx(1.0, abs=0.01)
    assert notch(RATE, 600.0, a, q) is None  # above Nyquist the firmware leaves it off


def test_the_block_engine_equals_the_biquad_recursion() -> None:
    rng = np.random.default_rng(3)
    x = rng.normal(size=(3000, 3))
    starts = np.arange(0, 3000, 5, dtype=np.int64)
    coeffs: list[tuple[float, float, float, float, float]] = []
    for k in range(len(starts)):
        centre = 150 + 30 * math.sin(k / 20)
        f = notch(RATE, centre, *notch_a_q(centre, 40, 40))
        assert f is not None
        coeffs.append((f.b0, f.b1, f.b2, f.a1, f.a2))
    c = np.asarray(coeffs)
    y = biquads(x, starts, c)
    ref = np.empty_like(x)
    x1 = x2 = y1 = y2 = x[0]
    for i in range(len(x)):
        b0, b1, b2, a1, a2 = c[i // 5]
        out = b0 * x[i] + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, x[i], y1, out
        ref[i] = out
    assert np.abs(y - ref).max() < 1e-12


# --- where the harmonic notch puts its notches ------------------------------------------------


def test_harmonics_double_and_triple_notches() -> None:
    single = notch_of(INS_HNTCH_HMNCS=0b101).notches([120.0])
    assert [round(n.center_hz) for n in single] == [120, 360]  # 1st and 3rd harmonic
    double = notch_of(INS_HNTCH_OPTS=1).notches([120.0])
    spread = 50 / (32 * 100)  # BW / (32 x FREQ)
    assert [n.center_hz for n in double] == pytest.approx([120 * (1 - spread), 120 * (1 + spread)])
    triple = notch_of(INS_HNTCH_OPTS=16).notches([120.0])
    assert len(triple) == 3 and triple[0].center_hz == pytest.approx(120.0)


def test_low_frequencies_hold_at_the_minimum_then_fade_out() -> None:
    n = notch_of(INS_HNTCH_FM_RAT=0.8)  # minimum 80 Hz, off below 20 Hz
    [held] = n.placements([60.0])
    assert held is not None and held.center_hz == 80.0 and n.a < held.a < 1.0
    assert n.placements([15.0]) == [None]
    assert notch_of(INS_HNTCH_OPTS=32).placements([15.0])[0] is not None  # option 5: hold


def test_notches_above_48_percent_of_the_rate_are_switched_off() -> None:
    placed = notch_of(INS_HNTCH_HMNCS=0b111).placements([200.0])
    assert [p is not None for p in placed] == [True, True, False]  # 600 Hz is past 480 Hz


def test_an_f4_board_caps_the_notch_count() -> None:
    p = NotchParams.read(params(INS_HNTCH_HMNCS=0b111, INS_HNTCH_OPTS=16), "INS_HNTCH_")
    many = HarmonicNotch(p, 8000.0, "F4").placements([100.0] * 12)
    assert len(many) == 24  # 12 motors x 3 harmonics x 3 notches capped at HAL_HNF_MAX_FILTERS


# --- what drives the notches ---------------------------------------------------------------------


def test_throttle_tracking_scales_with_the_square_root_of_throttle() -> None:
    motb = table([0, 100_000], {"ThrOut": (pa.float32(), [0.25, 0.36])})
    src = sources(notch_of(), {"MOTB": motb}, 0, 200_000)
    assert src is not None
    assert src.freqs[:, 0] == pytest.approx([100.0, 120.0])  # 100 x sqrt(thr / 0.25)


def test_esc_tracking_averages_the_motors_or_follows_each() -> None:
    esc = table(
        [0, 0, 10_000, 10_000],
        {"Instance": (pa.uint8(), [0, 1, 0, 1]), "RPM": (pa.float32(), [6000, 7200, 6000, 7200])},
        {"RPM": {b"unit": b"rpm", b"multiplier": b"1.0"}},
    )
    mean = sources(notch_of(INS_HNTCH_MODE=3, INS_HNTCH_REF=1), {"ESC": esc}, 0, 20_000)
    each = sources(
        notch_of(INS_HNTCH_MODE=3, INS_HNTCH_REF=1, INS_HNTCH_OPTS=2), {"ESC": esc}, 0, 20_000
    )
    assert mean is not None and each is not None
    assert mean.freqs[0, 0] == pytest.approx(110.0)
    assert list(each.freqs[0]) == pytest.approx([100.0, 120.0])


def test_a_fixed_notch_ignores_everything_else() -> None:
    src = sources(notch_of(INS_HNTCH_MODE=0), {}, 0, 1)
    assert src is not None and src.freqs[0, 0] == 100.0


# --- the simulation ---------------------------------------------------------------------------


def test_a_tracked_tone_is_removed_as_the_firmware_would() -> None:
    t = np.arange(20_000) / RATE
    thr = 0.25 * (1 + 0.5 * t / t[-1])  # throttle rises: the tone climbs from 100 Hz
    freq = 100 * np.sqrt(thr / 0.25)
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    x = np.column_stack([np.sin(phase)] * 3)
    times = (np.arange(len(t)) * 1000).astype(np.int64)
    motb = table(times[::10].tolist(), {"ThrOut": (pa.float32(), thr[::10].tolist())})
    filters = chain(params(), RATE, "H7", {"MOTB": motb}, 0, int(times[-1]))
    out = simulate(filters, Run(0, int(times[-1]), RATE, x, times))
    rms_in, rms_out = np.sqrt((x[2000:] ** 2).mean()), np.sqrt((out.samples[2000:] ** 2).mean())
    assert 20 * math.log10(rms_out / rms_in) < -25  # the notch follows the tone down


# --- the model against the simulator's own post-filter data ---------------------------------------


def _flight_inputs(
    name: str,
) -> tuple[dict[str, float], dict[str, pa.Table], list[tuple[int, int]], int, str | None]:
    pack = load_pack()
    with read_dataflash(FIXTURES / name) as df:
        snap = from_log(df)
        p = snap.dialect(pack).to_canonical(snap.values).values
        fp = fingerprint(df, pack, bytes(32))
        tables = {n: df.table(n) for n in TABLES if n in df.message_types}
    spans = [(w.start_us, w.end_us) for w in fp.hover_windows]
    return dict(p), tables, spans, int(fp.imu_count.value or 1), fp.board.family


@pytest.mark.parametrize(
    "name", ["copter-quad-rawimu.bin", "copter-heavylift-light.bin", "copter-heavylift-loaded.bin"]
)
def test_the_model_reproduces_the_logged_post_filter_gyro(name: str) -> None:
    p, tables, spans, imus, family = _flight_inputs(name)
    result = check(p, tables, spans, imus, family)
    assert result is not None
    error = result.spectrum_error_db()
    assert error is not None and error < 0.2  # dB, in 10 Hz bands up to 450 Hz
    [tracking] = result.tracking
    assert tracking["model_centre_hz"] == pytest.approx(tracking["logged_centre_hz"], rel=0.01)
    lag = result.phase_lag_deg
    assert 0 < lag["10"] < lag["20"] < lag["40"]


def test_notch_inputs_logged_at_every_update_replay_the_filters_exactly() -> None:
    # Per-motor ESC notches moved at loop rate in gusty wind, with fast harmonic notch
    # logging: following the logged inputs leaves a fraction of the error that
    # rebuilding them from 100 Hz ESC telemetry does.
    p, tables, spans, imus, family = _flight_inputs("copter-quad-fastnotch.bin")
    exact = check(p, tables, spans, imus, family)
    assert exact is not None and exact.notch_inputs == "exact"
    [tracking] = exact.tracking
    assert tracking["source"] == "logged FTN, every update"
    exact_error = exact.spectrum_error_db()
    assert exact_error is not None and exact_error < 0.1
    rebuilt_tables = {n: t for n, t in tables.items() if n not in ("FTN", "FTNS")}
    rebuilt = check(p, rebuilt_tables, spans, imus, family)
    assert rebuilt is not None and rebuilt.notch_inputs == "rebuilt"
    rebuilt_error = rebuilt.spectrum_error_db()
    assert rebuilt_error is not None and rebuilt_error > 3 * exact_error


def test_inputs_logged_at_10_hz_are_not_every_update() -> None:
    _, tables, spans, *_ = _flight_inputs("copter-quad-rawimu.bin")  # FTNS at 10 Hz
    assert "FTNS" in tables
    assert logged_inputs(tables, 0, 5_000, spans[0][0], spans[-1][1]) is None


def test_a_candidate_follows_the_logged_inputs_only_if_it_follows_the_same_thing() -> None:
    p, tables, spans, _, family = _flight_inputs("copter-quad-fastnotch.bin")
    a, b = spans[0][0], spans[-1][1]
    shape = chain(p, 1000.0, family, tables, a, b, {"INS_HNTCH_": {"bw": 20.0}})
    assert shape.sets[0].sources.every_update and shape.inputs == "exact"
    mode = chain(p, 1000.0, family, tables, a, b, {"INS_HNTCH_": {"mode": 1, "ref": 0.3}})
    assert not mode.sets[0].sources.every_update and mode.inputs == "rebuilt"
    single = chain(p, 1000.0, family, tables, a, b, {"INS_HNTCH_": {"opts": 4}})
    assert not single.sets[0].sources.every_update


def test_the_wrong_settings_do_not_reproduce_the_flight(pack_reg: Registry) -> None:
    p, tables, spans, imus, family = _flight_inputs("copter-heavylift-loaded.bin")
    hover = [{"start_us": a, "end_us": b} for a, b in spans]
    fp = {"vehicle": "copter", "board": {"family": family}, "imu_count": {"value": imus},
          "hover_windows": hover}  # fmt: skip
    arm, disarm = spans[0][0] - 10_000_000, spans[-1][1] + 10_000_000
    right = FILTER_MODEL.run(flight(tables, arm, disarm, p, fingerprint=fp), pack_reg)
    assert right.metrics[0].zone == "target" and not right.findings
    wrong = dict(p, INS_HNTCH_FREQ=120.0)  # not what this aircraft flew with
    r = FILTER_MODEL.run(flight(tables, arm, disarm, wrong, fingerprint=fp), pack_reg)
    [f] = r.findings
    assert f.code == "filter.model_mismatch" and f.params["error"] > 1


@pytest.fixture(scope="module")
def pack_reg() -> Registry:
    return Registry.load(load_pack())


def test_filter_check_reports_hashes_and_errors_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    shutil.copy(FIXTURES / "copter-quad-rawimu.bin", corpus / "secret-name.bin")
    shutil.copy(FIXTURES / "copter-quad-hover.bin", corpus / "another.bin")
    out = tmp_path / "report.json"
    code = main(["filter-check", "--corpus", str(corpus), "--report", str(out), "--jobs", "1"])
    assert code == 0 and "PASSED" in capsys.readouterr().out
    text = out.read_text(encoding="utf-8")
    assert "secret-name" not in text and "another" not in text
    data = json.loads(text)
    assert data["checked"] == 1 and data["passed"] is True
    assert {log["status"] for log in data["logs"]} == {"checked", "no pre-filter data"}
    # each log is held to its class's target, from the knowledge pack's zones
    assert data["targets_db"] == {"exact": 0.1, "rebuilt": 1.5, "control": 0.1}
    [checked] = [log for log in data["logs"] if log["status"] == "checked"]
    assert checked["notch_inputs"] == "rebuilt"  # ESC RPM at 100 Hz, the mean of the motors
    assert (checked["target_db"], checked["passed"]) == (1.5, True)
