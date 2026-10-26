"""The tune check: stick inputs found in the rate targets, the ones that measure nothing
but the rate loop kept, tracking error with a seeded interval, and the logging that
makes it possible."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import flight, table

from flight_insights.analyzers import FlightData, Registry, run_analysis
from flight_insights.analyzers.log_adequacy import LOG_ADEQUACY
from flight_insights.analyzers.model import Result
from flight_insights.analyzers.tune import TUNE
from flight_insights.imports import Decision, commit, propose, scan
from flight_insights.knowledge import load_pack
from flight_insights.segment.excitation import Signals, events, spans
from flight_insights.stats import bootstrap_mean
from flight_insights.store import StoreKeys, open_store
from flight_insights.store.cache import ColumnCache

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
ARM, DISARM = 1_000_000, 140_000_000
HZ = 400.0
DEG_S = {b"unit": b"deg/s", b"multiplier": b"1"}
METRE = {b"unit": b"m", b"multiplier": b"1"}
# Eight doublets on roll, then pitch, then yaw: 1 s one way, 1 s the other, 3 s centred.
STARTS = {
    axis: [5.0 + 45.0 * i + 5.0 * k for k in range(8)] for i, axis in enumerate(("R", "P", "Y"))
}
AMPLITUDE = 40.0  # deg/s
LOGGED = float(180222 | 1 | 1 << 12)  # Copter's default bitmask, with fast attitude and PID


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def _times(hz: float = HZ) -> np.ndarray[Any, Any]:
    return np.arange(0, DISARM + 2_000_000, int(1e6 / hz), dtype=np.int64)


def doublets(t: np.ndarray[Any, Any], starts: list[float], amplitude: float = AMPLITUDE) -> Any:
    s = t / 1e6
    out = np.zeros(len(t))
    for t0 in starts:
        out[(s >= t0) & (s < t0 + 1)] = amplitude
        out[(s >= t0 + 1) & (s < t0 + 2)] = -amplitude
    return out


def lag(target: Any, tau_s: float, hz: float = HZ) -> Any:
    """A first-order response to the target, as a slow rate loop gives."""
    a = (1.0 / hz) / (tau_s + 1.0 / hz)
    out = np.zeros(len(target))
    for i in range(1, len(target)):
        out[i] = out[i - 1] + a * (target[i] - out[i - 1])
    return out


def rate_table(
    t: np.ndarray[Any, Any], respond: Callable[[Any], Any] = lambda d: 0.75 * d
) -> pa.Table:
    columns: dict[str, tuple[pa.DataType, Any]] = {}
    meta: dict[str, Mapping[bytes, bytes]] = {}
    for axis in ("R", "P", "Y"):
        target = doublets(t, STARTS[axis])
        columns[f"{axis}Des"] = (pa.float32(), target)
        columns[axis] = (pa.float32(), respond(target))
        meta[f"{axis}Des"] = meta[axis] = DEG_S
    return table(t.tolist(), columns, meta)


def sticks(starts: Mapping[str, list[float]] = STARTS) -> pa.Table:
    t = _times(10.0)
    columns: dict[str, tuple[pa.DataType, list[int]]] = {}
    for channel, axis in (("C1", "R"), ("C2", "P"), ("C4", "Y")):
        pwm = 1500 + 4 * doublets(t, starts[axis])  # 40 deg/s from 160 µs: 40% of the throw
        columns[channel] = (pa.uint16(), pwm.astype(int).tolist())
    columns["C3"] = (pa.uint16(), [1500] * len(t))
    return table(t.tolist(), columns)


def climbed(low: tuple[float, float] | None = None) -> tuple[list[int], list[float]]:
    """CTUN at 10 Hz: on the ground until 3 s after arming, then 12 m up, or 1 m during `low`."""
    t = _times(10.0)
    alt = np.where(t < ARM + 3_000_000, 0.0, 12.0)
    if low is not None:
        alt[(t >= low[0] * 1e6) & (t < low[1] * 1e6)] = 1.0
    return t.tolist(), alt.tolist()


def tune_flight(
    rate: pa.Table | None = None,
    modes: list[tuple[int, int]] | None = None,
    height: tuple[list[int], list[float]] | None = None,
    flags: Mapping[str, list[tuple[float, float]]] | None = None,
    rcin: pa.Table | None = None,
    answers: Mapping[str, Any] | None = None,
    **params: float,
) -> Any:
    t = _times()
    pids: dict[str, pa.Table] = {}
    for name, axis in (("PIDR", "R"), ("PIDP", "P"), ("PIDY", "Y")):
        f = np.zeros(len(t), dtype=np.uint8)
        for a, b in (flags or {}).get(axis, []):
            f[(t >= a * 1e6) & (t <= b * 1e6)] = 1
        pids[name] = table(t.tolist(), {"Flags": (pa.uint8(), f.tolist())})
    mode_rows = modes or [(0, 2)]  # ALT_HOLD
    ht, h = height or climbed()
    tables = {
        "RATE": rate if rate is not None else rate_table(t),
        "MODE": table(
            [m[0] for m in mode_rows], {"ModeNum": (pa.uint8(), [m[1] for m in mode_rows])}
        ),
        "CTUN": table(ht, {"Alt": (pa.float32(), h)}, {"Alt": METRE}),
        "RCIN": rcin if rcin is not None else sticks(),
        **pids,
    }
    return flight(tables, ARM, DISARM, params={"LOG_BITMASK": LOGGED} | params, answers=answers)


def metrics(r: Result) -> dict[str, Any]:
    return {m.metric.rsplit("_", 1)[-1]: m for m in r.metrics}


def left_out(r: Result, axis: str) -> dict[str, int]:
    return dict(next(e for e in r.evidence if e.id == f"tune:{axis}").values["left_out"])


# --- events ---------------------------------------------------------------------------------


def test_each_doublet_is_one_event_with_its_start_and_settling(reg: Registry) -> None:
    rules = reg.rules["tune"]["excitation"]
    t = _times()
    target = doublets(t, STARTS["R"])
    found = spans(t, target, rules, "roll")
    assert len(found) == 8  # the two lobes of each doublet are one event
    a, b = found[0]
    assert a == pytest.approx(4.8e6, abs=3e3) and b == pytest.approx(8.0e6, abs=3e3)
    assert all(found[k][1] <= found[k + 1][0] for k in range(7))  # never overlapping


def test_tracking_error_is_the_share_of_the_target_missed(reg: Registry) -> None:
    rules = reg.rules["tune"]["excitation"]
    t = _times()
    target = doublets(t, STARTS["R"])
    signals = Signals(modes=[(0, DISARM + 1, "ALT_HOLD")], limited=None, height=None, stick=None)
    found = events("roll", t, target, 0.75 * target, signals, rules)
    assert [e.rejected for e in found] == [None] * 8
    assert all(e.error == pytest.approx(0.25) and e.peak_dps == AMPLITUDE for e in found)


def test_a_slower_rate_loop_tracks_worse(reg: Registry) -> None:
    t = _times()
    quick = metrics(TUNE.run(tune_flight(rate_table(t, lambda d: lag(d, 0.03))), reg))
    slow = metrics(TUNE.run(tune_flight(rate_table(t, lambda d: lag(d, 0.12))), reg))
    for axis in ("roll", "pitch", "yaw"):
        assert slow[axis].spread["low"] > quick[axis].spread["high"]  # intervals apart


# --- the metric -------------------------------------------------------------------------------


def test_each_axis_gets_its_tracking_error_with_an_interval(reg: Registry) -> None:
    r = TUNE.run(tune_flight(), reg)
    assert r.skipped is None and [f.code for f in r.findings] == ["tune.autotune_offer"]
    m = metrics(r)
    assert set(m) == {"roll", "pitch", "yaw"}
    for axis, value in m.items():
        assert value.value == pytest.approx(25.0, abs=1e-6)  # the actual rate is 75% of the target
        assert value.spread == pytest.approx({"low": 25.0, "high": 25.0, "events": 8.0}, abs=1e-6)
        assert value.zone is None and value.segment == "inputs"  # judged against its own flights
        assert value.evidence_id == f"tune:{axis}"


def test_the_interval_is_seeded_and_holds_the_mean() -> None:
    errors = [20.0, 22.5, 31.0, 24.0, 27.5, 19.0, 25.5, 30.0]
    low, high = bootstrap_mean(errors, seed=1)
    assert (low, high) == bootstrap_mean(errors, seed=1)  # the same on every laptop
    assert low < float(np.mean(errors)) < high
    assert high - low < max(errors) - min(errors)


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("loiter", "mode"),
        ("mode change", "mode change"),
        ("low", "low"),
        ("saturated", "saturated"),
        ("no stick", "no stick"),
        ("log gap", "log gap"),
    ],
)
def test_an_input_that_measures_more_than_the_rate_loop_is_left_out(
    reg: Registry, case: str, reason: str
) -> None:
    first = STARTS["R"][0]  # the first roll doublet, 5 s to 7 s
    t = _times()
    kwargs: dict[str, Any] = {}
    if case == "loiter":
        kwargs["modes"] = [(0, 2), (int(4e6), 5), (int(9.5e6), 2)]
    elif case == "mode change":
        kwargs["modes"] = [(0, 2), (int(6e6), 0)]  # AltHold to Stabilize mid-doublet
    elif case == "low":
        kwargs["height"] = climbed(low=(first - 0.5, first + 2.5))
    elif case == "saturated":
        kwargs["flags"] = {"R": [(first + 0.5, first + 0.6)]}
    elif case == "no stick":
        kwargs["rcin"] = sticks({**STARTS, "R": STARTS["R"][1:]})
    else:
        keep = (t < (first + 0.5) * 1e6) | (t > (first + 0.8) * 1e6)
        kwargs["rate"] = rate_table(t).filter(pa.array(keep))
    r = TUNE.run(tune_flight(**kwargs), reg)
    assert left_out(r, "roll") == {reason: 1}
    roll = metrics(r)["roll"]
    assert roll.spread["events"] == 7.0  # the others still count


def test_too_few_counted_inputs_give_no_value_and_say_why(reg: Registry) -> None:
    # The motors reached their limit on every roll input.
    every = [(s, s + 2.0) for s in STARTS["R"]]
    r = TUNE.run(tune_flight(flags={"R": every}), reg)
    assert set(metrics(r)) == {"pitch", "yaw"}
    [few] = [f for f in r.findings if f.code == "tune.few_inputs"]
    assert few.code == "tune.few_inputs" and few.severity == "info"
    assert {k: few.params[k] for k in ("roll", "pitch", "yaw", "needed", "saturated")} == {
        "roll": 0, "pitch": 8, "yaw": 8, "needed": 6, "saturated": 8,
    }  # fmt: skip


def test_ordinary_flying_outside_the_stick_modes_asks_for_nothing(reg: Registry) -> None:
    r = TUNE.run(tune_flight(modes=[(0, 5)]), reg)  # all in Loiter
    assert not r.metrics and not r.findings


def test_rates_logged_at_ten_hz_cannot_be_measured(reg: Registry) -> None:
    r = TUNE.run(tune_flight(rate_table(_times(10.0))), reg)
    assert r.skipped == "RATE is logged at 10 Hz; the tune check needs 200"


# --- the logging it needs ------------------------------------------------------------------------


def _slow_pids(hz: float) -> dict[str, pa.Table]:
    t = _times(hz)
    return {
        name: table(t.tolist(), {"Flags": (pa.uint8(), [0] * len(t))})
        for name in ("PIDR", "PIDP", "PIDY")
    }


def test_the_rate_loop_logged_at_ten_hz_asks_for_fast_attitude_logging(reg: Registry) -> None:
    t = _times(10.0)
    tables = {"RATE": rate_table(t), **_slow_pids(10.0)}
    r = LOG_ADEQUACY.run(flight(tables, ARM, DISARM, params={"LOG_BITMASK": 180222.0}), reg)
    [f] = [f for f in r.findings if f.code == "logging.tune_evidence"]
    assert f.severity == "info" and f.params["hz"] == 10.0 and f.params["needed"] == 200.0
    assert f.params["proposed"] == 180222 | 1  # PID (bit 12) is on by default already
    [change] = f.changes
    assert (change.param, change.proposed, change.stage) == ("LOG_BITMASK", 180222 | 1, "rate tune")


def test_a_rate_loop_logged_every_loop_needs_nothing_more(reg: Registry) -> None:
    tables = {"RATE": rate_table(_times()), **_slow_pids(HZ)}
    r = LOG_ADEQUACY.run(flight(tables, ARM, DISARM, params={"LOG_BITMASK": LOGGED}), reg)
    assert not [f for f in r.findings if f.code == "logging.tune_evidence"]


def test_a_parameter_file_is_told_what_its_bitmask_would_log(reg: Registry) -> None:
    p = FlightData(
        id="f", key="k", airframe_id=None, firmware="4.7.0", arm_us=0, disarm_us=0,
        params={"LOG_BITMASK": 176126.0}, fingerprint={"vehicle": "copter"}, cache=None,
    )  # fmt: skip
    r = LOG_ADEQUACY.run(p, reg)
    [f] = [f for f in r.findings if f.code == "logging.tune_evidence"]
    assert f.params["hz"] == 10.0  # bit 1: RATE at 10 Hz
    assert f.params["proposed"] == 176126 | 1 | 1 << 12
    assert TUNE.run(p, reg).skipped == "a parameter file has no flight to measure"


def test_planes_are_not_asked(reg: Registry) -> None:
    tables = {"RATE": rate_table(_times(10.0))}
    plane = flight(
        tables, ARM, DISARM, params={"LOG_BITMASK": 0.0}, fingerprint={"vehicle": "plane"}
    )
    assert not [
        f for f in LOG_ADEQUACY.run(plane, reg).findings if f.code.startswith("logging.tune")
    ]
    assert TUNE.run(plane, reg).skipped == "the tune check is for Copter"


# --- the simulator flight ------------------------------------------------------------------------


def test_the_simulated_tune_check_measures_roll_and_pitch(reg: Registry, tmp_path: Any) -> None:
    log = (
        Path(__file__).resolve().parents[2]
        / "tests/fixtures/sitl/4.7.0/copter-quad-tune-default.bin"
    )
    keys = StoreKeys.generate()
    pack = reg.pack
    with open_store(tmp_path / "f.db", keys, "e" * 32) as store:
        cache = ColumnCache(tmp_path / "cache", keys)
        batch = scan(store, pack, bytes(range(32)), [log])
        [result] = commit(store, pack, cache, batch, propose(store, pack, batch), [Decision("f1")])
        out = run_analysis(store, cache, pack, result.flights[0])
    tune = {m["metric"]: m for m in out["metrics"] if m["metric"].startswith("tune.tracking")}
    # Six doublets on roll and six on pitch in AltHold, every one counted; no yaw inputs.
    assert set(tune) == {"tune.tracking_error_roll", "tune.tracking_error_pitch"}
    for m in tune.values():
        assert m["spread"]["events"] == 6
        # The simulated quad's rate follows its target about 28 ms late at 0.94 gain.
        assert 20.0 < m["value"] < 30.0
        assert m["spread"]["low"] <= m["value"] <= m["spread"]["high"]
        assert m["spread"]["high"] - m["spread"]["low"] < 1.0  # the same input, the same answer
    codes = {f["code"] for f in out["findings"]}
    assert not codes & {"tune.few_inputs", "logging.tune_evidence"}


# --- what the tune check offers next -------------------------------------------------------


def test_a_measured_tune_check_offers_autotune_to_measure_against(reg: Registry) -> None:
    [offer] = TUNE.run(tune_flight(), reg).findings
    assert (offer.code, offer.severity, offer.params["axes"]) == (
        "tune.autotune_offer",
        "info",
        "roll, pitch",
    )
    assert {e.id for e in offer.evidence} == {"tune:roll", "tune:pitch"}
    assert offer.changes == ()  # small propellers, a fixed payload: nothing to set first


def test_large_props_and_a_varying_payload_set_autotune_up_first(reg: Registry) -> None:
    """ArduPilot: 13-inch props or more, rate filters at 10 Hz; a payload that varies gets
    more gain back-off. One axis a pack is the procedure's text, not a change to keep."""
    fl = tune_flight(answers={"prop_diameter_in": 24, "payload": "variable"},
                     ATC_RAT_RLL_FLTT=20.0, ATC_RAT_RLL_FLTD=20.0, ATC_RAT_PIT_FLTT=20.0,
                     ATC_RAT_PIT_FLTD=10.0, AUTOTUNE_AXES=7.0, AUTOTUNE_GMBK=0.25)  # fmt: skip
    [offer] = TUNE.run(fl, reg).findings
    assert {c.param: c.proposed for c in offer.changes} == {
        "ATC_RAT_RLL_FLTT": 10.0, "ATC_RAT_RLL_FLTD": 10.0, "ATC_RAT_PIT_FLTT": 10.0,
        "AUTOTUNE_GMBK": 0.35,
    }  # fmt: skip
    assert all(c.stage == "autotune" for c in offer.changes) and offer.params["large_props"]


def test_a_flight_with_autotune_in_it_is_not_offered_it_again(reg: Registry) -> None:
    r = TUNE.run(tune_flight(modes=[(0, 2), (55, 15)]), reg)  # AutoTune after the inputs
    assert "tune.autotune_offer" not in {f.code for f in r.findings}
