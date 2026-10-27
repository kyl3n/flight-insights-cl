"""The in-flight A/B split (history/ab.py): a parameter changed while the aircraft flew
splits the flight there, and both sides are compared on what the change should move."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.model import Analyzer, Evidence, MetricValue, Result, Segment
from flight_insights.analyzers.tune import TUNE
from flight_insights.history import ab
from flight_insights.imports import Decision, commit, propose, scan
from flight_insights.knowledge import Pack, load_pack
from flight_insights.store import Store, StoreKeys, open_store
from flight_insights.store.cache import ColumnCache

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
PACK = load_pack()


@dataclasses.dataclass
class Imported:
    store: Store
    cache: ColumnCache
    pack: Pack
    flight: str


Load = Callable[[str], Imported]


@pytest.fixture
def imported(tmp_path: Path) -> Iterator[Load]:
    def load(name: str) -> Imported:
        folder = tmp_path / name
        folder.mkdir()
        keys = StoreKeys.generate()
        store = open_store(folder / "f.db", keys, "e" * 32)
        stores.append(store)
        cache = ColumnCache(folder / "cache", keys)
        batch = scan(store, PACK, bytes(range(32)), [FIXTURES / f"{name}.bin"])
        [res] = commit(store, PACK, cache, batch, propose(store, PACK, batch), [Decision("f1")])
        return Imported(store, cache, PACK, res.flights[0])

    stores: list[Store] = []
    yield load
    for s in stores:
        s.close()


def _secs(i: Imported, t: int) -> float:
    row = i.store.get("flight", i.flight)
    assert row is not None
    return (t - int(row["arm_us"])) / 1e6


def test_the_roll_gains_cut_mid_flight_make_the_roll_tracking_worse(imported: Load) -> None:
    i = imported("copter-quad-tune-ab")
    splits = ab.split(i.store, i.cache, i.pack, i.flight)
    # The tune check's logging, switched on and off around each set of doublets, moves
    # nothing part of a flight can show: only the gain change splits the flight.
    [s] = splits
    names = {c.name for c in s.changes}
    assert {"ATC_RAT_RLL_P", "ATC_RAT_RLL_I", "ATC_RAT_RLL_D"} <= names
    assert s.skipped is None and s.before is not None and s.after is not None
    assert s.before[0] == FlightData.load(i.store, i.cache, i.pack, i.flight).arm_us
    assert s.before[1] == s.at_us and s.after[0] > s.at_us
    found = {r.metric: r for r in s.results}
    for metric in ("tune.tracking_error_roll", "tune.attitude_error_roll"):
        r = found[metric]
        assert r.method == "intervals"
        assert r.verdict == "worse" and r.low > 0  # the two sides' intervals are apart
        assert r.after > 1.4 * r.before
        assert r.candidates[0].change.name == "ATC_RAT_RLL_P"
        assert r.candidates[0].group == "rate P"
        assert r.confidence == "high"
    # Nothing the roll gains do not move is compared.
    assert not any(m.endswith(("_pitch", "_yaw")) for m in found)


def test_each_side_is_measured_with_its_own_gains(imported: Load) -> None:
    i = imported("copter-quad-tune-ab")
    [s] = ab.split(i.store, i.cache, i.pack, i.flight)
    flight = FlightData.load(i.store, i.cache, i.pack, i.flight)
    reg = Registry.load(i.pack)
    assert s.before is not None and s.after is not None
    seen: list[float] = []

    def probe(f: FlightData, _reg: Registry) -> Result:
        seen.append(f.params["ATC_RAT_RLL_P"])
        return Result()

    analyzer = dataclasses.replace(TUNE, run=probe)
    ab._measure([analyzer], flight, reg, s.before, flight.params, {"x"})  # pyright: ignore[reportPrivateUsage]
    assert seen == [pytest.approx(0.135)]
    moment_params = {**flight.params, "ATC_RAT_RLL_P": 0.054}
    ab._measure([analyzer], flight, reg, s.after, moment_params, {"x"})  # pyright: ignore[reportPrivateUsage]
    assert seen[-1] == pytest.approx(0.054)


def test_a_flight_whose_only_change_is_the_tune_checks_logging_is_not_split(imported: Load) -> None:
    i = imported("copter-quad-tune-default")
    assert ab.split(i.store, i.cache, i.pack, i.flight) == []


def test_a_metric_with_no_interval_is_compared_on_blocks(
    imported: Load, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-term noise has no interval of its own: each side is cut into blocks, and the
    blocks are tested as flights are."""
    i = imported("copter-quad-tune-ab")

    def noise(f: FlightData, _reg: Registry) -> Result:
        assert f.span is not None
        wobble = 0.1 * np.sin(f.span[0] / 1e6)  # a little scatter between the blocks
        value = 4000.0 * f.params["ATC_RAT_RLL_D"] + float(wobble)
        ev = Evidence("rate_d:roll", "metric", "noise", ("PIDR",), ("D",),
                      Segment("hover", *f.span))  # fmt: skip
        return Result(metrics=[MetricValue("tune.d_noise_roll", "hover", value, None, ev.id)],
                      evidence=[ev])  # fmt: skip

    fake = Analyzer("fake_noise", 1, noise, ("tune.d_noise_roll",), (), spans=True)
    monkeypatch.setattr(ab, "ANALYZERS", (fake,))
    [s] = ab.split(i.store, i.cache, i.pack, i.flight)
    [r] = [r for r in s.results if r.metric == "tune.d_noise_roll"]
    assert r.method == "blocks"
    assert r.before == pytest.approx(14.4, abs=0.2) and r.after == pytest.approx(5.76, abs=0.2)
    assert r.verdict == "better" and r.high < 0  # less D, less D-term noise
    assert r.candidates[0].change.name == "ATC_RAT_RLL_D"


def test_a_side_too_short_is_not_compared(imported: Load, monkeypatch: pytest.MonkeyPatch) -> None:
    i = imported("copter-quad-tune-ab")
    reg = Registry.load(i.pack)
    rules = dict(reg.rules)
    rules["attribution"] = dict(rules["attribution"]) | {
        "in_flight": dict(rules["attribution"]["in_flight"]) | {"min_side_s": 120}
    }
    longer = dataclasses.replace(reg, rules=rules)

    def load(_pack: Pack) -> Registry:
        return longer

    monkeypatch.setattr(ab.Registry, "load", staticmethod(load))
    [s] = ab.split(i.store, i.cache, i.pack, i.flight)
    assert s.results == () and s.skipped is not None and "120 s" in s.skipped


def test_the_split_is_shown_in_seconds_after_arming(imported: Load) -> None:
    i = imported("copter-quad-tune-ab")
    [s] = ab.split(i.store, i.cache, i.pack, i.flight)
    reg = Registry.load(i.pack)
    row = i.store.get("flight", i.flight)
    assert row is not None
    out = ab.split_json(s, reg, int(row["arm_us"]))
    assert out["at_s"] == pytest.approx(_secs(i, s.at_us), abs=0.01)
    assert out["before_s"][0] == 0.0 and out["after_s"][0] > out["at_s"]
    [r] = [r for r in out["results"] if r["metric"] == "tune.tracking_error_roll"]
    assert r["verdict"] == "worse" and r["candidates"][0]["link"] == "rate P"
    assert r["candidates"][0]["why"]["en"]


# --- the transmitter's tuning knob ----------------------------------------------------

KNOB = Registry.load(PACK).rules["attribution"]["in_flight"]["tuning_knob"]


def test_the_knob_turned_mid_flight_makes_the_roll_tracking_worse(imported: Load) -> None:
    """RC7 is the knob for roll rate P (TUNE 49), turned from 0.135 to 0.054 between two
    sets of doublets: no PARM records it, PTUN does."""
    i = imported("copter-quad-tune-knob")
    [s] = ab.split(i.store, i.cache, i.pack, i.flight)
    [p] = [c for c in s.changes if c.name == "ATC_RAT_RLL_P"]
    assert p.origin == "tuning knob"
    assert p.before == pytest.approx(0.135) and p.after == pytest.approx(0.054)
    assert s.skipped is None and s.before is not None and s.after is not None
    found = {r.metric: r for r in s.results}
    for metric in ("tune.tracking_error_roll", "tune.attitude_error_roll"):
        r = found[metric]
        assert r.verdict == "worse" and r.low > 0 and r.confidence == "high"
        assert r.after > 1.4 * r.before
        top = r.candidates[0]
        assert (top.change.name, top.change.origin, top.group) == (
            "ATC_RAT_RLL_P", "tuning knob", "rate P",
        )  # fmt: skip
    row = i.store.get("flight", i.flight)
    assert row is not None
    shown = ab.split_json(s, Registry.load(i.pack), int(row["arm_us"]))
    assert {"name": "ATC_RAT_RLL_P", "origin": "tuning knob"}.items() <= next(
        c for c in shown["changes"] if c["name"] == "ATC_RAT_RLL_P"
    ).items()


def test_each_side_is_measured_with_the_knobs_value(imported: Load) -> None:
    i = imported("copter-quad-tune-knob")
    flight = FlightData.load(i.store, i.cache, i.pack, i.flight)
    [m] = ab._moments(i.store, i.pack, flight, 5_000_000, lambda c: (  # pyright: ignore[reportPrivateUsage]
        frozenset({"x"}) if c.name == "ATC_RAT_RLL_P" else frozenset()
    ), KNOB)  # fmt: skip
    assert m.before["ATC_RAT_RLL_P"] == pytest.approx(0.135)
    assert m.after["ATC_RAT_RLL_P"] == pytest.approx(0.054)
    assert m.last_us - m.first_us < 1_000_000  # the knob turned within a second


def _ptun(rows: list[tuple[float, float]], function: int = 49) -> FlightData:
    """A flight armed at 10 s whose only table is PTUN: (seconds, value) at 0.3 s."""
    import pyarrow as pa
    from flight_builder import flight

    n = len(rows)
    columns = {
        "TimeUS": pa.array([int(t * 1e6) for t, _ in rows], pa.uint64()),
        "Param": pa.array([function] * n, pa.uint8()),
        "TunVal": pa.array([v for _, v in rows], pa.float32()),
        "TunMin": pa.array([0.054] * n, pa.float32()),
        "TunMax": pa.array([0.135] * n, pa.float32()),
    }
    table = pa.Table.from_arrays(list(columns.values()), names=list(columns))
    return flight({"PTUN": table}, 10_000_000, 200_000_000)


def _held(*spans: tuple[float, float, float]) -> list[tuple[float, float]]:
    """The knob at each (from, to, value), sampled every 0.3 s as the firmware logs it."""
    return [(t, v) for a, b, v in spans for t in np.arange(a, b, 0.3)]


def test_a_receivers_jitter_is_no_turn() -> None:
    wobble = [(t, 0.135 + (0.001 if k % 2 else -0.001))
              for k, (t, _) in enumerate(_held((0, 90, 0.0)))]  # fmt: skip
    turns, at_arm = ab.knob(_ptun(wobble), KNOB)
    assert turns == [] and at_arm[49] == pytest.approx(0.135, abs=0.002)


def test_a_turn_runs_from_the_value_it_left_to_the_value_it_reached() -> None:
    rows = _held((0, 40, 0.135), (40, 41.5, 0.1), (41.5, 90, 0.054))  # turned over 1.5 s
    [turn] = ab.knob(_ptun(rows), KNOB)[0]
    assert (turn.function, turn.before, turn.after) == (49, pytest.approx(0.135),
                                                       pytest.approx(0.054))  # fmt: skip
    assert turn.first_us == pytest.approx(39.9e6, abs=0.31e6)
    assert turn.last_us == pytest.approx(41.5e6, abs=0.31e6)
    # nudged for a moment and put back: the knob held one value throughout
    nudged = _held((0, 40, 0.135), (40, 41, 0.1), (41, 90, 0.135))
    assert ab.knob(_ptun(nudged), KNOB)[0] == []


def test_a_turn_before_arming_sets_the_value_it_flew_with() -> None:
    turns, at_arm = ab.knob(_ptun(_held((0, 5, 0.135), (5, 90, 0.054))), KNOB)
    assert turns == [] and at_arm[49] == pytest.approx(0.054)


def test_every_knob_function_names_parameters_of_4_7() -> None:
    meta = PACK.metadata("4.7.0", "copter")
    for fn, spec in KNOB["functions"].items():
        assert spec["params"] and all(p in meta for p in spec["params"]), fn
    assert KNOB["functions"]["10"] == {"params": ["WP_SPD"], "scale": 0.01}  # cm/s to m/s
    assert meta["WP_SPD"]["units"] == "m/s"


def test_a_span_cuts_the_hover_and_the_analysed_times(imported: Load) -> None:
    i = imported("copter-quad-tune-default")
    f = FlightData.load(i.store, i.cache, i.pack, i.flight)
    assert f.start_us == f.arm_us and f.end_us == f.disarm_us
    middle = (f.arm_us + f.disarm_us) // 2
    half = dataclasses.replace(f, span=(middle, f.disarm_us + 10**9))
    assert half.start_us == middle and half.end_us == f.disarm_us
    assert all(h.start_us >= middle for h in half.hover())
    assert half.armed.start_us == middle
    t = np.array([f.arm_us, middle - 1, middle, f.disarm_us])
    assert half.during(t).tolist() == [False, False, True, True]
    # The tune check on the half counts only the inputs flown in it.
    whole = TUNE.run(f, Registry.load(i.pack))
    part = TUNE.run(half, Registry.load(i.pack))
    count = {e.id: e.values["counted"] for e in whole.evidence if e.id.startswith("tune:")}
    left = {e.id: e.values["counted"] for e in part.evidence if e.id.startswith("tune:")}
    assert sum(left.values()) < sum(count.values())


def test_the_split_over_rpc(tmp_path: Path) -> None:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, PACK)
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys())
    pl = call(server, out, 2, "platform.create", name="Quad", vehicle="copter")["result"]
    af = call(server, out, 3, "airframe.create", platform_id=pl["id"], name="Q-1")["result"]
    path = str(FIXTURES / "copter-quad-tune-ab.bin")
    scanned = call(server, out, 4, "import.scan", paths=[path])["result"]
    [done] = call(server, out, 5, "import.commit", batch=scanned["batch"],
                  decisions=[{"log": "f1", "airframe_id": af["id"]}])["result"]  # fmt: skip
    [s] = call(server, out, 6, "flight.ab", flight_id=done["flights"][0])["result"]
    assert {c["name"] for c in s["changes"]} >= {"ATC_RAT_RLL_P", "ATC_RAT_RLL_D"}
    assert {r["metric"]: r["verdict"] for r in s["results"]}["tune.tracking_error_roll"] == "worse"
    bad = call(server, out, 7, "flight.ab", flight_id="nope")
    assert bad["error"]["message"] == "no such flight"
