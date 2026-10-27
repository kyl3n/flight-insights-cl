"""The revert test (history/revert.py): the single-change test flights that settle a
shift, most likely first, read from what the airframe's later flights showed."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest
from test_attribution import D_NOISE, Value, airframe, change, fly

from flight_insights.analyzers import Registry
from flight_insights.history.attribution import attribute
from flight_insights.history.persist import save_snapshot
from flight_insights.history.revert import (
    RevertError,
    files,
    find_shift,
    revert_json,
    revert_test,
)
from flight_insights.history.snapshot import Snapshot
from flight_insights.knowledge import Pack, load_pack
from flight_insights.params.files import read_param_file
from flight_insights.store import Store, StoreKeys, open_store

BASE = {"ATC_RAT_RLL_D": 0.003, "ATC_RAT_RLL_P": 0.135, "BATT_CAPACITY": 10000.0}


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


@pytest.fixture(scope="module")
def reg(pack: Pack) -> Registry:
    return Registry.load(pack)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with open_store(tmp_path / "f.db", StoreKeys.generate(), "e" * 32) as s:
        yield s


def flown(store: Store, pack: Pack, af: str, n: int, value: Value, params: Mapping[str, float],
          metric: tuple[str, str] = D_NOISE) -> str:  # fmt: skip
    """Flight n, with its D-term noise (or `metric`) and the parameters it flew with."""
    fid = fly(store, af, n, {metric: value})
    snap = save_snapshot(store, Snapshot(dict(params), "copter", "4.7.0", "log start", "primary"),
                         pack)  # fmt: skip
    store.conn.execute("UPDATE flight SET param_snapshot_id = ? WHERE id = ?", (snap, fid))
    return fid


def history(store: Store, pack: Pack, af: str, rows: Sequence[tuple[Value, Mapping[str, float]]]
            ) -> list[str]:  # fmt: skip
    return [flown(store, pack, af, n, v, p) for n, (v, p) in enumerate(rows)]


RAISED = BASE | {"ATC_RAT_RLL_D": 0.006, "BATT_CAPACITY": 12000.0}


def shifted(store: Store, pack: Pack, extra: Sequence[tuple[Value, Mapping[str, float]]] = ()
            ) -> tuple[str, list[str]]:  # fmt: skip
    """Four quiet flights, then D and the capacity raised together: the D-term noise
    doubles. Then the later flights in `extra`."""
    af = airframe(store)
    rows = [(10.0, BASE), (10.4, BASE), (9.8, BASE), (10.2, BASE),
            (20.1, RAISED), (19.8, RAISED), (20.4, RAISED), *extra]  # fmt: skip
    ids = history(store, pack, af, rows)
    change(store, af, ids[4], ("ATC_RAT_RLL_D", 0.003, 0.006, "tuning"),
           ("BATT_CAPACITY", 10000.0, 12000.0, "tuning"))  # fmt: skip
    for k in range(7, len(rows)):  # a later flight's own changes, as import records them
        was, now = rows[k - 1][1], rows[k][1]
        moved = [(n, was[n], now[n], "tuning") for n in now if now[n] != was[n]]
        if moved:
            change(store, af, ids[k], *moved)
    return af, ids


def test_each_parameter_is_one_step_most_likely_first(store: Store, pack: Pack, reg: Registry
                                                      ) -> None:  # fmt: skip
    af, _ = shifted(store, pack)
    [s] = attribute(store, reg, af, "tune.d_noise_roll")
    t = revert_test(store, pack, reg, af, s)
    assert [x.candidate.change.name for x in t.steps] == ["ATC_RAT_RLL_D", "BATT_CAPACITY"]
    assert [x.state for x in t.steps] == ["to fly", "to fly"] and t.next_step == 1
    assert t.before == pytest.approx(10.1) and t.after == pytest.approx(20.1)
    assert t.band is not None and t.explained_by is None


def test_a_flight_with_the_top_candidate_put_back_explains_the_shift(
    store: Store, pack: Pack, reg: Registry
) -> None:
    reverted = RAISED | {"ATC_RAT_RLL_D": 0.003}
    af, ids = shifted(store, pack, [(10.3, reverted)])
    s = find_shift(attribute(store, reg, af, "tune.d_noise_roll"), *D_NOISE,
                   "label:", ids[4])  # fmt: skip
    assert s is not None
    t = revert_test(store, pack, reg, af, s)
    assert [x.state for x in t.steps] == ["explained", "not needed"]
    assert t.steps[0].flight == ids[7] and t.steps[0].value == pytest.approx(10.3)
    assert t.explained_by == "ATC_RAT_RLL_D" and t.next_step is None


def test_a_change_that_was_not_the_cause_sends_the_next_step(
    store: Store, pack: Pack, reg: Registry
) -> None:
    reverted = RAISED | {"ATC_RAT_RLL_D": 0.003}
    af, ids = shifted(store, pack, [(19.9, reverted)])  # still noisy with D back
    s = find_shift(attribute(store, reg, af, "tune.d_noise_roll"), *D_NOISE, "label:", ids[4])
    assert s is not None
    t = revert_test(store, pack, reg, af, s)
    assert [x.state for x in t.steps] == ["not this one", "to fly"] and t.next_step == 2


def test_a_flight_that_put_back_two_changes_tests_neither(
    store: Store, pack: Pack, reg: Registry
) -> None:
    af, ids = shifted(store, pack, [(10.0, BASE)])  # both back: it says nothing about which
    s = find_shift(attribute(store, reg, af, "tune.d_noise_roll"), *D_NOISE, "label:", ids[4])
    assert s is not None
    t = revert_test(store, pack, reg, af, s)
    assert [x.state for x in t.steps] == ["to fly", "to fly"]


def test_halfway_back_is_unclear(store: Store, pack: Pack, reg: Registry) -> None:
    reverted = RAISED | {"ATC_RAT_RLL_D": 0.003}
    af, ids = shifted(store, pack, [(15.0, reverted)])
    s = find_shift(attribute(store, reg, af, "tune.d_noise_roll"), *D_NOISE, "label:", ids[4])
    assert s is not None
    t = revert_test(store, pack, reg, af, s)
    assert t.steps[0].state == "unclear" and t.next_step == 2


def test_the_files_put_back_one_parameter_and_undo_it(
    store: Store, pack: Pack, reg: Registry, tmp_path: Path
) -> None:
    af, _ = shifted(store, pack)
    [s] = attribute(store, reg, af, "tune.d_noise_roll")
    t = revert_test(store, pack, reg, af, s)
    revert_file, undo_file = files(t, 1, store, pack, reg, "mission planner")
    assert revert_file["filename"].endswith("-1-revert.param")
    for f, value in ((revert_file, 0.003), (undo_file, 0.006)):
        path = tmp_path / f["filename"]
        path.write_text(f["text"])
        [entry] = read_param_file(path).entries
        assert entry.name == "ATC_RAT_RLL_D" and entry.value == pytest.approx(value)
    assert "changes nothing else" in revert_file["text"]
    assert "near 10.1" in revert_file["text"] and "the next test" in revert_file["text"]
    [qgc, _] = files(t, 2, store, pack, reg, "qgroundcontrol", "uk")
    assert qgc["filename"].endswith(".params") and "BATT_CAPACITY" in qgc["text"]
    assert "Тест повернення 2 з 2" in qgc["text"]
    with pytest.raises(RevertError):
        files(t, 3, store, pack, reg, "mission planner")


def test_only_a_worse_shift_with_a_parameter_to_put_back_is_tested(
    store: Store, pack: Pack, reg: Registry
) -> None:
    af = airframe(store)
    lowered = BASE | {"ATC_RAT_RLL_D": 0.0015}
    ids = history(store, pack, af, [(10.0, BASE), (10.4, BASE), (9.8, BASE), (10.2, BASE),
                                    (5.1, lowered), (4.9, lowered)])  # fmt: skip
    change(store, af, ids[4], ("ATC_RAT_RLL_D", 0.003, 0.0015, "tuning"))
    [better] = attribute(store, reg, af, "tune.d_noise_roll")
    assert better.verdict == "better"
    with pytest.raises(RevertError, match="worse"):
        revert_test(store, pack, reg, af, better)


def test_the_test_reads_as_the_app_shows_it(store: Store, pack: Pack, reg: Registry) -> None:
    af, _ = shifted(store, pack)
    [s] = attribute(store, reg, af, "tune.d_noise_roll")
    shown = revert_json(revert_test(store, pack, reg, af, s), reg)
    assert shown["metric"] == "tune.d_noise_roll" and shown["next_step"] == 1
    assert [x["name"] for x in shown["steps"]] == ["ATC_RAT_RLL_D", "BATT_CAPACITY"]
    assert shown["steps"][0]["state"] == "to fly" and shown["by_hand"] == []


def test_the_revert_test_over_rpc(tmp_path: Path, pack: Pack) -> None:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, pack)
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys())
    pl = call(server, out, 2, "platform.create", name="Quad", vehicle="copter")["result"]
    af = call(server, out, 3, "airframe.create", platform_id=pl["id"], name="Q-1")["result"]
    shift = {"airframe_id": af["id"], "metric": D_NOISE[0], "segment": D_NOISE[1],
             "group": "label:", "flight": "f"}  # fmt: skip
    none = call(server, out, 4, "trend.revert_test", **shift)
    assert none["error"]["message"] == "the airframe has no such shift"
    unnamed = call(server, out, 5, "trend.revert_test", **(shift | {"group": ""}))
    assert unnamed["error"]["message"] == "group must name the shift"
    unknown = call(server, out, 6, "trend.revert_test", **(shift | {"metric": "tune.feel"}))
    assert "registry" in unknown["error"]["message"]


def test_a_metric_with_intervals_reads_the_test_flights_interval(
    store: Store, pack: Pack, reg: Registry
) -> None:
    """One tune check each side: the test flight's interval against the last flight
    before and the first after."""
    track = ("tune.tracking_error_roll", "inputs")
    soft = BASE | {"ATC_RAT_RLL_P": 0.054}
    af = airframe(store)
    ids = [
        flown(store, pack, af, 0, (25.0, 24.0, 26.0), BASE, track),
        flown(store, pack, af, 1, (41.0, 40.0, 42.0), soft, track),
        flown(store, pack, af, 2, (25.6, 24.6, 26.6), BASE, track),
    ]
    change(store, af, ids[1], ("ATC_RAT_RLL_P", 0.135, 0.054, "tuning"))
    change(store, af, ids[2], ("ATC_RAT_RLL_P", 0.054, 0.135, "tuning"))
    s = find_shift(attribute(store, reg, af, track[0]), *track, "label:", ids[1])
    assert s is not None and s.spread is None and s.verdict == "worse"
    t = revert_test(store, pack, reg, af, s)
    assert t.band is None
    assert [x.state for x in t.steps] == ["explained"] and t.explained_by == "ATC_RAT_RLL_P"
