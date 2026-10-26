"""The attribution map (rules/attribution.json): which parameter changes should move
which metrics, checked against the metric registry and the 4.7 parameter metadata."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from test_analyzers import copy_pack

from flight_insights.analyzers import Registry
from flight_insights.history.attribution import attribute, links_for, links_to, shift_json
from flight_insights.knowledge import KnowledgeError, Pack, load_pack
from flight_insights.params import Dialect
from flight_insights.platforms.records import create_airframe, create_platform
from flight_insights.platforms.schema import load_schema
from flight_insights.store import Store, StoreKeys, open_store


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


@pytest.fixture(scope="module")
def reg(pack: Pack) -> Registry:
    return Registry.load(pack)


def test_every_pattern_names_real_parameters(reg: Registry, pack: Pack) -> None:
    for version in pack.versions:
        names = set(pack.metadata(version, "copter"))
        for group in reg.attribution:
            for pattern in group.params:
                assert any(pattern.fullmatch(n) for n in names), (version, pattern.pattern)


def test_only_tuning_and_calibration_parameters_are_candidates(reg: Registry, pack: Pack) -> None:
    """Learned values, counters, hardware IDs and ground-station settings change on their
    own: they are context, never a cause, so the map ties none of them to a metric."""
    dialect = Dialect(pack, "copter", pack.versions[-1])
    for name in sorted(pack.metadata(pack.versions[-1], "copter")):
        if any(True for _ in links_for(reg, name)):
            assert dialect.param_class(name) in ("tuning", "calibration"), name
    assert not list(links_for(reg, "MOT_THST_HOVER"))  # learned while hover learning is on


def test_every_link_has_its_reasons_in_both_languages(reg: Registry) -> None:
    assert len(reg.attribution) >= 20
    for group in reg.attribution:
        assert set(group.why) == {"en", "uk"} and group.sources, group.group


def test_an_axis_in_the_name_maps_to_its_own_metrics(reg: Registry) -> None:
    roll = {k.metric: (k.sign, k.weight) for k in links_for(reg, "ATC_RAT_RLL_P")}
    assert roll["tune.attitude_error_roll"] == (-1, 1.0)  # more P, less error
    assert roll["tune.overshoot_roll"] == (1, 0.6)
    assert not any(m.endswith("_pitch") for m in roll)
    yaw = {k.metric for k in links_for(reg, "ATC_RAT_YAW_P")}
    assert "tune.tracking_error_yaw" in yaw and not any("attitude" in m for m in yaw)


def test_angle_p_moves_attitude_and_rate_error_opposite_ways(reg: Registry) -> None:
    """What the gate's SITL rehearsal showed: a stiffer attitude loop lowers attitude
    error and asks the rate loop for busier rates."""
    att = links_to(reg, "ATC_ANG_PIT_P", "tune.attitude_error_pitch")
    rate = links_to(reg, "ATC_ANG_PIT_P", "tune.tracking_error_pitch")
    assert att is not None and rate is not None and (att.sign, rate.sign) == (-1, 1)


def test_a_sensor_scale_moves_what_is_measured_through_it(reg: Registry) -> None:
    """A calibration change shifts the power figures with nothing changed on the aircraft:
    the map says so, so attribution can name it."""
    found = {k.metric for k in links_for(reg, "BATT_AMP_PERVLT") if k.sign == 1}
    assert {"power.peak_current", "power.hover_power"} <= found
    expo = links_to(reg, "MOT_THST_EXPO", "payload.hover_thrust")
    assert expo is not None and expo.weight == 1.0


def test_either_way_where_zero_means_off(reg: Registry) -> None:
    smax = links_to(reg, "ATC_RAT_RLL_SMAX", "tune.slew_limited_roll")
    assert smax is not None and smax.sign == 0


def _first_link(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["links"][0]


BROKEN: dict[str, tuple[Callable[[dict[str, Any]], object], str]] = {
    "a metric nobody measures": (
        lambda d: _first_link(d)["metrics"][0].update(metric="tune.feel_{axis}"),
        "no metric",
    ),
    "a sign of 2": (lambda d: _first_link(d)["metrics"][0].update(sign=2), "sign"),
    "an unknown strength": (
        lambda d: _first_link(d)["metrics"][0].update(strength="huge"),
        "strength",
    ),
    "a pattern that does not compile": (
        lambda d: _first_link(d).update(params=["ATC_RAT_(P"]),
        "does not compile",
    ),
    "an axis the parameters do not give": (
        lambda d: _first_link(d).update(params=["ATC_RAT_RLL_P"]),
        "names an axis",
    ),
    "no Ukrainian": (lambda d: _first_link(d)["why"].pop("uk"), "why needs text"),
    "no sources": (lambda d: _first_link(d).update(sources=[]), "sources"),
}


@pytest.mark.parametrize("case", sorted(BROKEN))
def test_a_broken_map_is_refused(pack: Pack, tmp_path: Path, case: str) -> None:
    edit, message = BROKEN[case]
    root = copy_pack(pack, tmp_path)
    path = root / "rules" / "attribution.json"
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    edit(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(KnowledgeError, match=message):
        Registry.load(load_pack(root))


# --- shifts and their ranked causes -------------------------------------------------------

T0 = 1_760_000_000_000_000
D_NOISE = ("tune.d_noise_roll", "inputs")
Value = float | tuple[float, float, float]  # a value, or (value, low, high)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with open_store(tmp_path / "f.db", StoreKeys.generate(), "e" * 32) as s:
        yield s


def airframe(store: Store) -> str:
    platform = create_platform(store, load_schema(load_pack()), "HL", "copter")
    return create_airframe(store, platform, "HL1")


def fly(
    store: Store, af: str, n: int, values: Mapping[tuple[str, str], Value],
    thrust: float | None = None, firmware: str = "4.7.0",
) -> str:  # fmt: skip
    """Flight n of the airframe, analysed, with these metric values."""
    fid = store.insert(
        "flight",
        {"airframe_id": af, "firmware": firmware, "boot_utc_us": T0 + n * 86_400_000_000,
         "arm_us": 1_000_000},
    )  # fmt: skip
    run = store.insert(
        "analysis_run",
        {"flight_id": fid, "engine_version": "t", "knowledge_version": "t",
         "analyzers_json": "{}", "status": "complete"},
    )  # fmt: skip
    rows = dict(values) | ({("payload.hover_thrust", "hover"): thrust} if thrust else {})
    for (metric, segment), v in rows.items():
        value, spread = (
            (v, {}) if isinstance(v, float | int) else (v[0], {"low": v[1], "high": v[2]})
        )
        store.insert(
            "metric_value",
            {"run_id": run, "flight_id": fid, "metric": metric, "segment": segment,
             "value": value, "spread_json": json.dumps(spread), "zone": "target"},
        )  # fmt: skip
    return fid


def change(store: Store, af: str, after: str, *changes: tuple[str, float, float, str]) -> None:
    """A parameter change set that first applies to flight `after`: (name, before,
    after, class) each."""
    store.insert(
        "change_set",
        {"airframe_id": af, "kind": "parameter", "origin": "loaded file",
         "after_flight_id": after,
         "details_json": json.dumps({"changes": [
             {"name": n, "before": b, "after": a, "class": c} for n, b, a, c in changes
         ]})},
    )  # fmt: skip


def noise(values: Sequence[float]) -> list[dict[tuple[str, str], Value]]:
    return [{D_NOISE: v} for v in values]


def flights(store: Store, af: str, series: Sequence[Mapping[tuple[str, str], Value]]) -> list[str]:
    return [fly(store, af, n, v) for n, v in enumerate(series)]


def test_a_raised_d_gain_is_named_for_the_d_noise_it_brought(store: Store, reg: Registry) -> None:
    af = airframe(store)
    ids = flights(store, af, noise([10.0, 10.4, 9.8, 10.2, 20.1, 19.8, 20.4]))
    change(store, af, ids[4], ("ATC_RAT_RLL_D", 0.003, 0.006, "tuning"),
           ("BATT_CAPACITY", 10000.0, 12000.0, "tuning"),
           ("MOT_THST_HOVER", 0.31, 0.33, "learned"))  # fmt: skip
    [s] = attribute(store, reg, af, "tune.d_noise_roll")
    assert (s.flight, s.verdict, s.confidence) == (ids[4], "worse", "high")
    assert s.before == tuple(ids[:4]) and s.after == tuple(ids[4:])
    assert s.effect == pytest.approx(9.95, abs=0.2) and s.low > 0
    ranked = [c.change.name for c in s.candidates]
    assert ranked == ["ATC_RAT_RLL_D", "BATT_CAPACITY"]  # linked, then not
    assert s.candidates[0].group == "rate D" and s.candidates[0].factors["direction"] == 1.0
    assert [c.name for c in s.context] == ["MOT_THST_HOVER"]  # learned: context, no cause


def test_a_shift_reads_as_the_app_shows_it(store: Store, reg: Registry) -> None:
    af = airframe(store)
    ids = flights(store, af, noise([10.0, 10.4, 9.8, 10.2, 20.1, 19.8, 20.4]))
    change(store, af, ids[4], ("ATC_RAT_RLL_D", 0.003, 0.006, "tuning"))
    [s] = attribute(store, reg, af)  # every metric: only D noise moved
    shown = shift_json(s, reg)
    assert (shown["metric"], shown["verdict"], shown["name"]["en"]) == (
        "tune.d_noise_roll", "worse", reg.metrics["tune.d_noise_roll"].name["en"]
    )  # fmt: skip
    [top] = shown["candidates"]
    assert top["name"] == "ATC_RAT_RLL_D" and top["link"] == "rate D"
    assert set(top["why"]) == {"en", "uk"} and json.dumps(shown)


def test_noise_inside_the_spread_is_no_shift(store: Store, reg: Registry) -> None:
    af = airframe(store)
    ids = flights(store, af, noise([10.0, 11.0, 9.0, 10.5, 9.5, 10.8, 9.7]))
    change(store, af, ids[4], ("ATC_RAT_RLL_D", 0.003, 0.0033, "tuning"))
    assert attribute(store, reg, af, "tune.d_noise_roll") == []


def test_a_large_move_on_too_few_flights_is_inconclusive(store: Store, reg: Registry) -> None:
    af = airframe(store)
    ids = flights(store, af, noise([10.0, 10.5, 9.5, 10.0, 30.0, 10.0]))
    change(store, af, ids[4], ("ATC_RAT_RLL_D", 0.003, 0.006, "tuning"))
    [s] = attribute(store, reg, af, "tune.d_noise_roll")
    assert s.verdict == "inconclusive" and s.low <= 0 <= s.high


def test_one_flight_each_side_counts_when_their_intervals_are_apart(
    store: Store, reg: Registry
) -> None:
    """The tune check's error carries an interval per flight, so the gate's few flights
    can still show a shift."""
    af = airframe(store)
    metric = ("tune.attitude_error_pitch", "inputs")
    a = fly(store, af, 0, {metric: (4.0, 3.8, 4.2)})
    b = fly(store, af, 1, {metric: (6.0, 5.7, 6.3)})
    change(store, af, b, ("ATC_ANG_PIT_P", 20.0, 9.0, "tuning"))
    [s] = attribute(store, reg, af, "tune.attitude_error_pitch")
    assert (s.before, s.after, s.verdict, s.spread) == ((a,), (b,), "worse", None)
    assert s.candidates[0].factors["direction"] == 1.0  # less angle P, more attitude error
    c = fly(store, af, 2, {metric: (6.1, 5.0, 7.2)})  # overlapping: no shift from b to c
    change(store, af, c, ("ATC_RAT_PIT_D", 0.004, 0.0042, "tuning"))
    assert [x.flight for x in attribute(store, reg, af, "tune.attitude_error_pitch")] == [b]


def test_a_loaded_flight_is_never_read_against_a_light_one(store: Store, reg: Registry) -> None:
    af = airframe(store)
    ids = [
        fly(store, af, n, {D_NOISE: v}, thrust=t)
        for n, (v, t) in enumerate([(10.0, 30.0), (10.2, 30.0), (9.9, 30.0), (10.1, 30.0),
                                    (14.0, 37.5), (14.2, 37.5), (13.9, 37.5)])
    ]  # fmt: skip
    change(store, af, ids[4], ("ATC_RAT_RLL_D", 0.003, 0.006, "tuning"))
    assert attribute(store, reg, af, "tune.d_noise_roll") == []  # loaded is its own group


def test_a_build_change_with_it_lowers_the_confidence(store: Store, reg: Registry) -> None:
    af = airframe(store)
    ids = flights(store, af, noise([10.0, 10.4, 9.8, 10.2, 20.1, 19.8, 20.4]))
    change(store, af, ids[4], ("ATC_RAT_RLL_D", 0.003, 0.006, "tuning"))
    store.insert(
        "build_version",
        {"airframe_id": af, "number": 2, "reason_kind": "upgrade", "reason": "new props",
         "spec_json": "{}", "effective_date": "2026-11-01", "first_flight_id": ids[4]},
    )  # fmt: skip
    [s] = attribute(store, reg, af, "tune.d_noise_roll")
    assert s.confidence == "medium" and {c.change.kind for c in s.candidates} == {
        "parameter", "build"
    }  # fmt: skip


def test_a_change_that_cannot_move_the_metric_does_not_split_its_window(
    store: Store, reg: Registry
) -> None:
    """Telemetry rates changed after flight 1: the D noise baseline still runs from
    flight 0, so the later D change has enough flights before it."""
    af = airframe(store)
    ids = flights(store, af, noise([10.0, 10.4, 9.8, 10.2, 20.1, 19.8, 20.4]))
    change(store, af, ids[2], ("SERIAL1_BAUD", 57.0, 115.0, "tuning"))
    change(store, af, ids[4], ("ATC_RAT_RLL_D", 0.003, 0.006, "tuning"))
    [s] = attribute(store, reg, af, "tune.d_noise_roll")
    assert s.flight == ids[4] and s.before == tuple(ids[:4])


def test_a_change_that_would_push_the_other_way_ranks_below(store: Store, reg: Registry) -> None:
    """D noise rose: a raised D gain fits, a lowered gyro filter would have lowered it."""
    af = airframe(store)
    ids = flights(store, af, noise([10.0, 10.4, 9.8, 10.2, 20.1, 19.8, 20.4]))
    change(store, af, ids[4], ("INS_GYRO_FILTER", 40.0, 20.0, "tuning"),
           ("ATC_RAT_RLL_D", 0.003, 0.006, "tuning"))  # fmt: skip
    [s] = attribute(store, reg, af, "tune.d_noise_roll")
    names = [c.change.name for c in s.candidates]
    assert names == ["ATC_RAT_RLL_D", "INS_GYRO_FILTER"]
    assert s.candidates[1].factors["direction"] == 0.2
