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
