"""The attribution map (rules/attribution.json): which parameter changes should move
which metrics, checked against the metric registry and the 4.7 parameter metadata."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_analyzers import copy_pack

from flight_insights.analyzers import Registry
from flight_insights.history.attribution import links_for, links_to
from flight_insights.knowledge import KnowledgeError, Pack, load_pack
from flight_insights.params import Dialect


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
