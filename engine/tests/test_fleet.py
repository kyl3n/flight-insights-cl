"""Fleet evidence (history/fleet.py) and credit for improvements (history/credit.py):
the platform's other airframes weigh in on a change, and a change that helped is
followed until someone undoes it."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest
from test_attribution import D_NOISE, change, fly
from test_revert import BASE, flown

from flight_insights.analyzers import Registry
from flight_insights.history import fleet
from flight_insights.history.attribution import attribute, shift_json
from flight_insights.history.credit import credit_json, credits
from flight_insights.knowledge import Pack, load_pack
from flight_insights.platforms.records import create_airframe, create_platform
from flight_insights.platforms.schema import load_schema
from flight_insights.store import Store, StoreKeys, open_store


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


def platform(store: Store, pack: Pack, count: int) -> tuple[str, list[str]]:
    p = create_platform(store, load_schema(pack), "HL", "copter")
    return p, [create_airframe(store, p, f"HL{k}") for k in range(count)]


def doubled(store: Store, af: str, *changes: tuple[str, float, float, str],
            after: Sequence[float] = (20.1, 19.8, 20.4)) -> list[str]:  # fmt: skip
    """Four quiet flights, then `changes` together, then `after`."""
    ids = [fly(store, af, n, {D_NOISE: v})
           for n, v in enumerate([10.0, 10.4, 9.8, 10.2, *after])]  # fmt: skip
    change(store, af, ids[4], *changes)
    return ids


D_UP = ("ATC_RAT_RLL_D", 0.003, 0.006, "tuning")
CAPACITY = ("BATT_CAPACITY", 10000.0, 12000.0, "tuning")
SPIN = ("MOT_SPIN_MIN", 0.15, 0.12, "tuning")


def top(store: Store, reg: Registry, af: str) -> list[tuple[str, float]]:
    [s] = attribute(store, reg, af, D_NOISE[0])
    return [(c.change.name, round(c.score, 3)) for c in s.candidates]


def test_three_airframes_that_agree_raise_the_change_everywhere(
    store: Store, pack: Pack, reg: Registry
) -> None:
    _, (target, *others) = platform(store, pack, 4)
    for af in others:
        doubled(store, af, D_UP)  # D alone, and the noise doubled
    doubled(store, target, D_UP, CAPACITY)
    [s] = attribute(store, reg, target, D_NOISE[0])
    d = s.candidates[0]
    assert d.change.name == "ATC_RAT_RLL_D" and d.fleet == (3, 0)
    assert d.factors["fleet"] == 1.5 and d.score == pytest.approx(1.5)
    assert shift_json(s, reg)["candidates"][0]["fleet"] == {"agree": 3, "against": 0}
    # Without the fleet the same shift ranks as one airframe sees it.
    [alone] = attribute(store, reg, target, D_NOISE[0], fleet=False)
    assert alone.candidates[0].score == pytest.approx(1.0) and alone.candidates[0].fleet is None


def test_two_airframes_are_not_enough(store: Store, pack: Pack, reg: Registry) -> None:
    _, (target, *others) = platform(store, pack, 3)
    for af in others:
        doubled(store, af, D_UP)
    doubled(store, target, D_UP, CAPACITY)
    [s] = attribute(store, reg, target, D_NOISE[0])
    assert s.candidates[0].fleet == (2, 0) and "fleet" not in s.candidates[0].factors


def test_airframes_where_it_did_the_opposite_lower_it(
    store: Store, pack: Pack, reg: Registry
) -> None:
    _, (target, *others) = platform(store, pack, 4)
    for af in others:
        doubled(store, af, D_UP, after=(5.1, 4.9, 5.0))  # D up, noise down: the other way
    doubled(store, target, D_UP, CAPACITY)
    [s] = attribute(store, reg, target, D_NOISE[0])
    d = next(c for c in s.candidates if c.change.name == "ATC_RAT_RLL_D")
    assert d.fleet == (0, 3) and d.factors["fleet"] == 0.5


def test_a_change_the_map_does_not_tie_is_learned_from_the_fleet(
    store: Store, pack: Pack, reg: Registry
) -> None:
    _, (target, *others) = platform(store, pack, 4)
    for af in others:
        doubled(store, af, SPIN)  # the only change each time: it counts though unlinked
    doubled(store, target, SPIN, CAPACITY)
    [s] = attribute(store, reg, target, D_NOISE[0])
    assert [(c.change.name, c.group) for c in s.candidates] == [
        ("MOT_SPIN_MIN", "fleet"), ("BATT_CAPACITY", None)
    ]  # fmt: skip
    assert s.candidates[0].score == pytest.approx(0.6) and s.confidence == "high"
    # The target's own shift is no evidence: two changes, neither tied by the map.
    found = fleet.links(store, reg, platform_of(store, target))
    spin = [x for x in found if x["param"] == "MOT_SPIN_MIN"]
    assert spin == [{"param": "MOT_SPIN_MIN", "changed": "down", "metric": D_NOISE[0],
                     "moved": "up", "airframes": 3, "against": 0, "in_map": False,
                     "link": None}]  # fmt: skip


def test_the_platform_lists_the_links_it_confirmed(store: Store, pack: Pack, reg: Registry
                                                   ) -> None:  # fmt: skip
    p, airframes = platform(store, pack, 3)
    for af in airframes:
        doubled(store, af, D_UP)
    [d] = fleet.links(store, reg, p)
    assert (d["param"], d["changed"], d["moved"], d["airframes"]) == ("ATC_RAT_RLL_D", "up",
                                                                      "up", 3)  # fmt: skip
    assert d["in_map"] and d["link"] == "rate D"


def test_a_shift_with_changes_ranked_close_is_no_evidence(reg: Registry) -> None:
    from flight_insights.history.attribution import Candidate, Change, Shift

    def cand(name: str, score: float, group: str | None) -> Candidate:
        return Candidate(Change("parameter", name, 1.0, 2.0, "f", "tuning"), score, group, {})

    def shift(confidence: str, *cands: Candidate) -> Shift:
        return Shift("m", "s", "g", "f", (), (), 1.0, 0.5, 1.5, None, "worse", cands, (),
                     confidence)  # fmt: skip

    classes = {"tuning"}
    assert fleet.evidence(shift("low", cand("A", 1.0, "x"), cand("B", 0.9, "y")), classes) == []
    [one] = fleet.evidence(shift("high", cand("A", 1.0, "x"), cand("B", 0.1, None)), classes)
    assert one.name == "A"
    [only] = fleet.evidence(shift("low", cand("C", 0.05, None)), classes)
    assert only.name == "C"


def platform_of(store: Store, af: str) -> str:
    row = store.get("airframe", af)
    assert row is not None
    return str(row["platform_id"])


# --- credit ---------------------------------------------------------------------------

LOWER = BASE | {"ATC_RAT_RLL_D": 0.0015}


def improved(store: Store, pack: Pack, later: Sequence[tuple[float, Mapping[str, float]]] = ()
             ) -> tuple[str, list[str]]:  # fmt: skip
    _, (af,) = platform(store, pack, 1)
    rows = [(10.0, BASE), (10.4, BASE), (9.8, BASE), (10.2, BASE),
            (5.1, LOWER), (4.9, LOWER), (5.0, LOWER), *later]  # fmt: skip
    ids = [flown(store, pack, af, n, v, p) for n, (v, p) in enumerate(rows)]
    change(store, af, ids[4], ("ATC_RAT_RLL_D", 0.003, 0.0015, "tuning"))
    return af, ids


def test_a_change_that_helped_is_credited_and_kept(store: Store, pack: Pack, reg: Registry
                                                   ) -> None:  # fmt: skip
    af, ids = improved(store, pack)
    [c] = credits(store, pack, reg, af)
    assert c.candidate.change.name == "ATC_RAT_RLL_D" and c.shift.flight == ids[4]
    shown = credit_json(c, reg)
    assert shown["kept"] and shown["undone_on"] is None and shown["link"] == "rate D"


def test_a_credited_change_put_back_later_is_named(store: Store, pack: Pack, reg: Registry
                                                   ) -> None:  # fmt: skip
    af, ids = improved(store, pack, [(5.2, LOWER), (5.0, BASE)])
    [c] = credits(store, pack, reg, af)
    assert c.undone_on == ids[8] and c.value_then == pytest.approx(0.003)
    assert not credit_json(c, reg)["kept"]


def test_a_worse_shift_credits_nothing(store: Store, pack: Pack, reg: Registry) -> None:
    _, (af,) = platform(store, pack, 1)
    raised = BASE | {"ATC_RAT_RLL_D": 0.006}
    rows = [(10.0, BASE), (10.4, BASE), (9.8, BASE), (10.2, BASE),
            (20.1, raised), (19.8, raised), (20.4, raised)]  # fmt: skip
    ids = [flown(store, pack, af, n, v, p) for n, (v, p) in enumerate(rows)]
    change(store, af, ids[4], D_UP)
    assert credits(store, pack, reg, af) == []


def test_fleet_and_credits_over_rpc(tmp_path: Path, pack: Pack) -> None:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, pack)
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys())
    pl = call(server, out, 2, "platform.create", name="Quad", vehicle="copter")["result"]
    af = call(server, out, 3, "airframe.create", platform_id=pl["id"], name="Q-1")["result"]
    assert call(server, out, 4, "trend.fleet", platform_id=pl["id"])["result"] == []
    assert call(server, out, 5, "trend.credits", airframe_id=af["id"])["result"] == []
    bad = call(server, out, 6, "trend.fleet", platform_id="")
    assert bad["error"]["message"] == "platform_id must be an id"
