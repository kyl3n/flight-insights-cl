"""How fast each motor follows its command: a first-order lag with dead time recovered
from synthetic motors whose lag is known, a slow motor among its siblings, the logs
that cannot be timed, and the simulator's heavy-lift X8, whose motors have no lag."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import ARM, flight, table
from test_imports import airframe

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.model import Result
from flight_insights.analyzers.motor_lag import MOTOR_LAG, unboxed
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.spectral.lag import LagFit
from flight_insights.spectral.response import Stretch, cross_spectra
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

PACK = load_pack()
QUAD_X = PACK.frames("4.7.0")["1"]["types"]["1"]["motors"]
LOOP_HZ = 400.0
SECONDS = 30.0


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(PACK)


def _lagged(u: np.ndarray, hz: float, tc: float, dead: float) -> np.ndarray:
    """u through a first-order lag of time constant `tc` after `dead` seconds, exactly
    discretised at `hz`."""
    shift = round(dead * hz)
    delayed = np.concatenate([np.full(shift, u[0]), u[: len(u) - shift]])
    a = np.exp(-1.0 / (hz * tc)) if tc > 0 else 0.0
    y = np.empty_like(u)
    y[0] = delayed[0]
    for k in range(1, len(u)):
        y[k] = a * y[k - 1] + (1 - a) * delayed[k]
    return y


def expected_ms(tc: float, dead: float, hz: float = LOOP_HZ) -> float:
    """The response time of `_lagged` at `hz`: a first-order lag discretised exactly has
    the low-frequency delay a / (1 - a) samples, a little under its time constant."""
    a = np.exp(-1.0 / (hz * tc))
    return 1000.0 * (a / (1 - a) / hz + dead)


def _inputs(seed: int = 7) -> np.ndarray:
    """The mixer's throttle, roll, pitch and yaw inputs at the loop rate: a hover with
    stick-like random motion below about 10 Hz on every axis."""
    rng = np.random.default_rng(seed)
    n = int(SECONDS * LOOP_HZ)
    kernel = np.hanning(int(LOOP_HZ / 10))
    kernel /= kernel.sum()
    moves = np.stack([np.convolve(rng.normal(0, 1, n), kernel, "same") for _ in range(4)], 1)
    moves *= np.array([0.06, 0.12, 0.12, 0.12]) / moves.std(axis=0)
    moves[:, 0] += 0.45
    return moves


def lag_flight(lags: list[tuple[float, float]], rate_hz: float = LOOP_HZ,
               esc_hz: float = 100.0, with_esc: bool = True) -> FlightData:  # fmt: skip
    """A quad X whose motor i answers with (time constant, dead time) lags[i], its RPM
    reported every loop and logged by a task at `esc_hz`, RATE logged at `rate_hz`."""
    n = int(SECONDS * LOOP_HZ)
    t = ARM + np.arange(n, dtype=np.int64) * int(1e6 / LOOP_HZ)
    u = _inputs()
    factors = np.array([[m["throttle"], m["roll"], m["pitch"], m["yaw"]] for m in QUAD_X])
    command = u @ factors.T
    keep = np.arange(0, n, max(1, round(LOOP_HZ / rate_hz)))
    rate = table(
        t[keep].tolist(),
        {name: (pa.float32(), u[keep, j].tolist())
         for j, name in enumerate(("AOut", "ROut", "POut", "YOut"))},
    )  # fmt: skip
    tables = {"RATE": rate}
    if with_esc:
        log_t: list[int] = list(range(int(t[0]) + 5000, int(t[-1]), int(1e6 / esc_hz)))
        # the newest reading at each logging time
        latest = [int(k) for k in np.searchsorted(t, np.array(log_t), side="right") - 1]
        rows: list[tuple[int, int, float]] = []
        for i, (tc, dead) in enumerate(lags):
            rpm = 2000.0 + 20000.0 * _lagged(command[:, i], LOOP_HZ, tc, dead)
            rows += [(x, i, float(rpm[k])) for x, k in zip(log_t, latest, strict=True)]
        rows.sort()
        tables["ESC"] = table(
            [r[0] for r in rows],
            {"Instance": (pa.uint8(), [r[1] for r in rows]),
             "RPM": (pa.float32(), [r[2] for r in rows])},
            {"RPM": {b"unit": b"rpm", b"multiplier": b"1.0"}},
        )  # fmt: skip
    params = {"FRAME_CLASS": 1.0, "FRAME_TYPE": 1.0,
              **{f"SERVO{i + 1}_FUNCTION": 33.0 + i for i in range(4)}}  # fmt: skip
    return flight(tables, ARM, int(t[-1]), params)


def values(r: Result) -> dict[str, list[float]]:
    [ev] = r.evidence
    return ev.values  # type: ignore[return-value]


def test_the_fit_recovers_a_known_lag() -> None:
    """A first-order lag of 40 ms after 8 ms of dead time, measured as the analyzer
    measures it: the response time within 1 ms, and the two parts within a few."""
    hz, n = 100.0, 256
    rng = np.random.default_rng(3)
    u = np.convolve(rng.normal(0, 1, 40_000), np.hanning(40) / 20, "same")
    y = _lagged(u, 1000.0, 0.040, 0.008)
    # the grid as the analyzer builds it: the command averaged over each 10 ms step,
    # centred; the response read at the step's centre
    x10 = u.reshape(-1, 10).mean(axis=1)
    y10 = y[5::10]
    cs = cross_spectra([Stretch(x10, x10, y10)], hz, n)
    assert cs is not None
    lag = LagFit(np.fft.rfftfreq(n, 1 / hz), (1.0, 8.0), 0.5, 0.001, 0.08, 0.0005).fit(
        unboxed(cs.response(), hz), 0.2, 5
    )
    assert lag is not None
    # 48 ms, less 0.5 ms for the 1 kHz lag's discretisation, plus 0.5 ms for reading
    # the response half a sample after the step's centre
    assert lag.response_s == pytest.approx(0.048, abs=0.0015)
    assert lag.time_constant_s == pytest.approx(0.040, abs=0.003)
    assert lag.dead_time_s == pytest.approx(0.008, abs=0.003)


def test_motors_with_the_same_lag(reg: Registry) -> None:
    r = MOTOR_LAG.run(lag_flight([(0.040, 0.008)] * 4), reg)
    assert r.skipped is None and r.findings == []
    v = values(r)
    assert v["motors"] == [1, 2, 3, 4] and v["frame"] == "QUAD/X"
    # each RPM is logged on the loop it was read, so the logging adds nothing here
    want = expected_ms(0.040, 0.008)  # 46.75 ms
    for ms in v["response_ms"]:
        assert ms == pytest.approx(want, abs=1.5)
    [m] = r.metrics
    assert m.metric == "motors.response_ms" and m.value == pytest.approx(want, abs=1.5)
    assert m.spread["low"] <= m.value <= m.spread["high"] and m.zone is None


def test_a_motor_slower_than_its_siblings(reg: Registry) -> None:
    r = MOTOR_LAG.run(lag_flight([(0.040, 0.008)] * 2 + [(0.110, 0.008), (0.040, 0.008)]), reg)
    [f] = r.findings
    assert f.code == "motors.slow_motor" and f.severity == "caution"
    assert f.params["motor"] == 3
    assert f.params["response_ms"] == pytest.approx(expected_ms(0.110, 0.008), abs=3.0)
    assert f.params["others_ms"] == pytest.approx(expected_ms(0.040, 0.008), abs=1.5)


def test_what_cannot_be_timed(reg: Registry) -> None:
    slow = MOTOR_LAG.run(lag_flight([(0.04, 0.008)] * 4, rate_hz=10.0), reg)
    assert slow.skipped is not None and "RATE is logged at 10 Hz" in slow.skipped
    assert "LOG_BITMASK bit 0" in slow.skipped
    none = MOTOR_LAG.run(lag_flight([(0.04, 0.008)] * 4, with_esc=False), reg)
    assert none.skipped == "the log has no ESC RPM (bidirectional DShot or ESC telemetry)"
    sparse = MOTOR_LAG.run(lag_flight([(0.04, 0.008)] * 4, esc_hz=10.0), reg)
    assert sparse.skipped is not None and "too slowly" in sparse.skipped


def test_the_simulators_heavy_lift_x8_has_no_lag(tmp_path: Path, reg: Registry) -> None:
    """ArduPilot's simulator turns each motor's command into RPM in the same step (its
    only limit, a slew rate, is far above what the doublets ask), so every motor's
    response is the logging alone: a few milliseconds, the same on all eight."""
    fixture = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
    keys = StoreKeys.generate()
    with open_store(tmp_path / "m.db", keys, "e" * 32) as store:
        cache = ColumnCache(tmp_path / "cache", keys)
        batch = scan(store, PACK, bytes(32), [fixture / "copter-heavylift-tune.bin"])
        af = airframe(store, PACK, "x8")
        [res] = commit(store, PACK, cache, batch, propose(store, PACK, batch),
                       [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
        r = MOTOR_LAG.run(FlightData.load(store, cache, PACK, res.flights[0]), reg)
    assert r.skipped is None and r.findings == []
    v = values(r)
    assert v["motors"] == list(range(1, 9)) and v["frame"] == "OCTAQUAD/X"
    assert all(0.0 <= ms <= 8.0 for ms in v["response_ms"])
    assert all(low is not None for low in v["low_ms"])
