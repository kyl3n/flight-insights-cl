"""The rate loop's responses: frequency and step responses read through the attitude
target, checked against loops whose answers are known, with gusts fed back through the
attitude loop, and on the simulator's tune-check flights."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import flight, table
from rate_loop_sim import HZ, Loop, fly

from flight_insights.analyzers import FlightData, Registry, run_analysis
from flight_insights.analyzers.model import Result
from flight_insights.analyzers.rate_tune import RATE_TUNE
from flight_insights.imports import Decision, commit, propose, scan
from flight_insights.knowledge import load_pack
from flight_insights.spectral.response import (
    Stretch,
    correlations,
    cross_spectra,
    figures,
    margins,
    step,
)
from flight_insights.store import StoreKeys, open_store
from flight_insights.store.cache import ColumnCache

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
ARM = 1_000_000
DEG = {b"unit": b"deg", b"multiplier": b"1"}
HEADING = {b"unit": b"degheading", b"multiplier": b"1"}
METRE = {b"unit": b"m", b"multiplier": b"1"}
# A loop like a well-tuned mid-size quad's, one with too little damping, and one too fast
# for stick doublets to reach its bandwidth.
GOOD = Loop(zeta=0.48, fn_hz=3.2, delay_s=0.03)
RINGING = Loop(zeta=0.3, fn_hz=2.0, delay_s=0.04)
FAST = Loop(zeta=0.5, fn_hz=25.0, delay_s=0.01)


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def _pid(t: list[int], target: Any, actual: Any, flags: Any = None) -> pa.Table:
    n = len(t)
    return table(
        t,
        {
            "Tar": (pa.float32(), np.asarray(target).tolist()),
            "Act": (pa.float32(), np.asarray(actual).tolist()),
            "Flags": (pa.uint8(), [0] * n if flags is None else list(flags)),
        },
    )


def loop_flight(
    loop: Loop,
    gust_dps: float = 0.0,
    pid_hz: float = HZ,
    mode: int = 2,  # ALT_HOLD
    tables: dict[str, pa.Table | None] | None = None,
    vehicle: str = "copter",
    **params: float,
) -> FlightData:
    """The roll loop flown by rate_loop_sim; pitch and yaw held still."""
    attitude_target, target, actual = fly(loop, gust_dps)
    n = len(target)
    t = (ARM + np.arange(n) * (1e6 / HZ)).astype(np.int64)
    zero = np.zeros(n)
    every = max(1, round(HZ / pid_hz))
    ts = t[::every].tolist()
    built: dict[str, pa.Table | None] = {
        "PIDR": _pid(ts, target[::every], actual[::every]),
        "PIDP": _pid(ts, zero[::every], zero[::every]),
        "PIDY": _pid(ts, zero[::every], zero[::every]),
        "ANG": table(
            t.tolist(),
            {
                "DesRoll": (pa.float32(), attitude_target.tolist()),
                "DesPitch": (pa.float32(), zero.tolist()),
                "DesYaw": (pa.float32(), zero.tolist()),
            },
            {"DesRoll": DEG, "DesPitch": DEG, "DesYaw": HEADING},
        ),
        "MODE": table([0], {"ModeNum": (pa.uint8(), [mode])}),
        # on the ground for 2 s after arming, then 12 m up
        "CTUN": table(
            [0, ARM + 2_000_000, ARM + 2_500_000, int(t[-1])],
            {"Alt": (pa.float32(), [0.0, 0.0, 12.0, 12.0])},
            {"Alt": METRE},
        ),
    } | (tables or {})
    present = {k: v for k, v in built.items() if v is not None}
    fingerprint: dict[str, Any] = {
        "vehicle": vehicle,
        "board": {"hal": "ChibiOS"},
        "hover_windows": [],
    }
    return flight(
        present, ARM, int(t[-1]), params={"SCHED_LOOP_RATE": 400.0} | params,
        fingerprint=fingerprint,
    )  # fmt: skip


def metrics(r: Result) -> dict[str, Any]:
    return {m.metric.removeprefix("tune.").removesuffix("_roll"): m for m in r.metrics}


def evidence(r: Result) -> dict[str, Any]:
    return dict(next(e for e in r.evidence if e.id == "rate:roll").values)


def worst_error_in(loop: Loop, top_hz: float) -> float:
    f = np.linspace(0.3, top_hz, 4000)
    return float(np.abs(1 - loop.response(f)).max())


# --- known loops -----------------------------------------------------------------------


def test_a_loop_measured_through_its_stick_inputs(reg: Registry) -> None:
    out = RATE_TUNE.run(loop_flight(GOOD), reg)
    m, ev = metrics(out), evidence(out)
    assert ev["bandwidth_hz"] is not None and ev["error_peak_inside"] and ev["step_measured"]
    assert m["bandwidth"].value == pytest.approx(GOOD.bandwidth_hz(), rel=0.03)
    assert m["sensitivity_peak"].value == pytest.approx(
        worst_error_in(GOOD, ev["measured_to_hz"]), rel=0.03
    )
    truth = figures(GOOD.step(), HZ)
    assert truth.rise_s is not None
    assert m["rise_time"].value == pytest.approx(truth.rise_s * 1000, rel=0.05)
    assert m["overshoot"].value == pytest.approx(truth.overshoot_pct, abs=1.0)
    assert m["settling"].value == pytest.approx(truth.settling_s * 1000, rel=0.1)
    for value in m.values():
        assert value.spread["low"] <= value.value <= value.spread["high"]
    assert not out.findings  # Ms 1.8: a reasonable margin


def test_gusts_through_the_attitude_loop_do_not_bias_the_step() -> None:
    """Read directly, the target carries the gusts back through the attitude loop and the
    step's tail goes wrong; read through the attitude target it stays where it was."""
    loop = Loop(zeta=0.7, fn_hz=5.0, delay_s=0.02)
    attitude_target, target, actual = fly(loop, 5.0)
    instrument = np.gradient(attitude_target) * HZ
    late = slice(int(3 * HZ), None)
    block, taps = int(5 * HZ), int(1.5 * HZ)
    through = [Stretch(instrument[late], target[late], actual[late])]
    direct = [Stretch(target[late], target[late], actual[late])]
    truth = figures(loop.step(), HZ)
    corr = correlations(through, block, taps)
    plain = correlations(direct, block, taps)
    assert corr is not None and plain is not None
    read, guessed = step(corr), step(plain)
    assert read is not None and guessed is not None
    good, bad = figures(read, HZ), figures(guessed, HZ)
    assert good.gain == pytest.approx(1.0, abs=0.05)
    assert good.settling_s < 3 * truth.settling_s
    assert bad.settling_s > 6 * truth.settling_s  # the tail never settles


def test_a_ringing_loop_has_a_low_margin(reg: Registry) -> None:
    out = RATE_TUNE.run(loop_flight(RINGING, gust_dps=2.0), reg)
    m, ev = metrics(out), evidence(out)
    ms = m["sensitivity_peak"]
    assert ms.value == pytest.approx(worst_error_in(RINGING, ev["measured_to_hz"]), rel=0.05)
    assert ms.zone == "caution"
    [finding] = out.findings
    assert finding.code == "tune.low_margin" and finding.severity == "caution"
    assert finding.params["axes"] == ["roll"]
    assert finding.params["ms"] == pytest.approx(ms.value, abs=0.01)
    assert 1.0 < finding.params["hz"] < 3.0
    assert [e.id for e in finding.evidence] == ["rate:roll"]


def test_feed_forward_leaves_the_sensitivity_peak_out(reg: Registry) -> None:
    """With FF, |1 - H| is no longer the loop's sensitivity: no peak, no finding."""
    out = RATE_TUNE.run(loop_flight(RINGING, gust_dps=2.0, ATC_RAT_RLL_FF=0.05), reg)
    assert "sensitivity_peak" not in metrics(out) and "bandwidth" in metrics(out)
    assert not out.findings
    assert evidence(out)["feed_forward"] == [0.05, 0.0]


def test_a_loop_faster_than_the_inputs_reach_is_not_guessed(reg: Registry) -> None:
    """Its bandwidth and margin lie above what stick doublets measure: the analyzer says
    how far it measured and gives no bandwidth, margin or step."""
    out = RATE_TUNE.run(loop_flight(FAST), reg)
    ev = evidence(out)
    assert not out.metrics and not out.findings
    assert ev["bandwidth_hz"] is None and not ev["step_measured"]
    assert not ev["error_peak_inside"] and ev["measured_to_hz"] < FAST.bandwidth_hz()


def test_the_intervals_are_seeded(reg: Registry) -> None:
    first = RATE_TUNE.run(loop_flight(GOOD), reg)
    again = RATE_TUNE.run(loop_flight(GOOD), reg)
    assert [(m.metric, m.value, dict(m.spread)) for m in first.metrics] == [
        (m.metric, m.value, dict(m.spread)) for m in again.metrics
    ]


def test_a_bin_is_measured_only_where_its_error_is_small() -> None:
    """Gusts shrink the band the inputs measure; what is measured stays right."""
    calm, gusty = fly(GOOD, 0.0), fly(GOOD, 10.0)
    reach: list[float] = []
    for attitude_target, target, actual in (calm, gusty):
        instrument = np.gradient(attitude_target) * HZ
        spectra = cross_spectra([Stretch(instrument, target, actual)], HZ, 2048)
        assert spectra is not None
        r = spectra.response()
        m = margins(r, 0.1, 0.3, 40.0)
        assert m is not None
        band = (r.freqs >= 0.3) & (r.freqs <= m.measured_to_hz)
        truth = GOOD.response(r.freqs[band])
        assert np.median(np.abs(r.h[band] - truth) / np.abs(truth)) < 0.1  # the bins' limit
        reach.append(m.measured_to_hz)
    assert reach[1] < reach[0]


# --- what the log must hold ---------------------------------------------------------------


def test_planes_are_not_asked(reg: Registry) -> None:
    out = RATE_TUNE.run(loop_flight(GOOD, vehicle="plane"), reg)
    assert out.skipped == "the rate-loop responses are for Copter"


@pytest.mark.parametrize(
    ("change", "skipped"),
    [
        ({"PIDR": None, "PIDP": None, "PIDY": None},
         "the log has no rate PID messages (PIDR, PIDP, PIDY)"),
        ({"ANG": None}, "the attitude targets (ANG) are logged at 0 Hz; the responses need 200"),
    ],
)  # fmt: skip
def test_what_the_log_lacks_is_said(
    reg: Registry, change: dict[str, pa.Table | None], skipped: str
) -> None:
    assert RATE_TUNE.run(loop_flight(GOOD, tables=change), reg).skipped == skipped


def test_rate_pids_logged_at_ten_hz_cannot_be_measured(reg: Registry) -> None:
    out = RATE_TUNE.run(loop_flight(GOOD, pid_hz=10.0), reg)
    assert out.skipped == "the rate PIDs are logged at 10 Hz; the responses need 200"


def test_flying_outside_the_stick_modes_measures_nothing(reg: Registry) -> None:
    out = RATE_TUNE.run(loop_flight(GOOD, mode=5), reg)  # LOITER
    assert out.skipped == "too little stick flying to measure the rate loop's responses"


def test_saturation_is_left_out(reg: Registry) -> None:
    """The PID's limit flag on throughout: nothing measures the loop alone."""
    _, target, actual = fly(GOOD)
    t = (ARM + np.arange(len(target)) * (1e6 / HZ)).astype(np.int64).tolist()
    saturated = _pid(t, target, actual, flags=[1] * len(t))
    out = RATE_TUNE.run(loop_flight(GOOD, tables={"PIDR": saturated}), reg)
    assert out.skipped == "too little stick flying to measure the rate loop's responses"


# --- the simulator's tune-check flights ---------------------------------------------------------


def _analysed(tmp_path: Path, log: Path) -> dict[str, Any]:
    tmp_path.mkdir()
    keys = StoreKeys.generate()
    pack = load_pack()
    with open_store(tmp_path / "f.db", keys, "e" * 32) as store:
        cache = ColumnCache(tmp_path / "cache", keys)
        batch = scan(store, pack, bytes(range(32)), [log])
        [result] = commit(store, pack, cache, batch, propose(store, pack, batch), [Decision("f1")])
        return run_analysis(store, cache, pack, result.flights[0])


def test_the_simulated_quads_soft_and_default_rate_loops_separate(tmp_path: Path) -> None:
    default = _analysed(tmp_path / "d", FIXTURES / "copter-quad-tune-default.bin")
    soft = _analysed(tmp_path / "s", FIXTURES / "copter-quad-tune-low.bin")

    def rate(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {e["id"]: e["values"] for e in out["evidence"] if e["id"].startswith("rate:")}

    def measured(out: dict[str, Any]) -> dict[str, Any]:
        names = ("bandwidth", "sensitivity_peak", "rise_time", "overshoot", "settling")
        return {
            m["metric"]: m for m in out["metrics"] if m["metric"].startswith(
                tuple(f"tune.{n}_" for n in names)
            )
        }  # fmt: skip

    # The default quad's loop reaches past 20 Hz: its doublets measure it only so far
    assert set(rate(default)) == {"rate:roll", "rate:pitch"}
    for values in rate(default).values():
        assert values["bandwidth_hz"] is None and not values["step_measured"]
        assert 10.0 < values["measured_to_hz"] < 25.0
    assert not measured(default)
    # At 0.4 of the default gains it is slow enough to measure whole
    got = measured(soft)
    for axis in ("roll", "pitch"):
        bandwidth = got[f"tune.bandwidth_{axis}"]
        assert 2.0 < bandwidth["value"] < 3.0
        assert bandwidth["spread"]["low"] <= bandwidth["value"] <= bandwidth["spread"]["high"]
        assert 120.0 < got[f"tune.rise_time_{axis}"]["value"] < 220.0
        assert got[f"tune.overshoot_{axis}"]["value"] < 5.0

    # and tracks its target worse
    def tracking(out: dict[str, Any]) -> float:
        return next(m["value"] for m in out["metrics"] if m["metric"] == "tune.tracking_error_roll")

    assert tracking(soft) > tracking(default) + 10.0
    assert "tune.low_margin" not in {f["code"] for f in soft["findings"] + default["findings"]}


def test_the_engine_keeps_numpy_to_one_blas_thread() -> None:
    """Many BLAS threads on top of the engine's own work slowed a busy laptop's analysis
    tenfold; the engine asks for one before NumPy loads, and the environment may override."""
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "OPENBLAS_NUM_THREADS"}
    code = "import flight_insights, os; print(os.environ['OPENBLAS_NUM_THREADS'])"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert out.stdout.strip() == "1", out.stderr
