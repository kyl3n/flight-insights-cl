"""The propeller the setup's Priority favours (performance/propchoice.py), on the made-up
heavy-lift quad of test_candidate: the search tries the flown family round its diameter,
ranks by hover power for endurance and by thrust-to-weight for agility, never offers one
that fails a check, and advises only when switched on and the gain clears the validated
error."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from test_candidate import DUTY, FAMILY, FLOWN, MASS, RHO, basis
from test_propfamily import RULES

from flight_insights.performance.propchoice import (
    PROP_CHOICE,
    candidates,
    finding,
    search,
    with_rules,
)

CALIBRATED = {FLOWN.family: replace(FAMILY, tables=2)}
QUICK = RULES | {"search": RULES["search"] | {"diameter_steps_in": [-2.0, 0.0, 2.0],
                                              "pitch_steps": 4}}  # fmt: skip


def test_the_search_round_the_flown_propeller() -> None:
    props = candidates(FLOWN, RULES)
    spec = RULES["search"]
    # 22x6.6 is on the grid (a pitch ratio of 0.30): the flown propeller is tried once
    assert len(props) == len(spec["diameter_steps_in"]) * spec["pitch_steps"]
    assert sum(p == FLOWN for p in props) == 1
    assert {p.diameter_in for p in props} == {20.0, 21.0, 22.0, 23.0, 24.0}
    assert all(p.family == FLOWN.family and p.blades == FLOWN.blades for p in props)
    lo, hi = spec["pitch_ratio"]
    assert all(lo - 0.01 <= p.pitch_in / p.diameter_in <= hi + 0.01 for p in props if p != FLOWN)


def test_endurance_ranks_by_hover_power() -> None:
    got = search(basis(families=CALIBRATED), QUICK, "endurance", MASS, RHO, DUTY)
    assert got["objective"] == "hover_power" and got["best"] is not None
    ok = [o for o in got["options"] if o["ok"]]
    assert ok[0] == got["best"] and ok[0]["gain"] > 0
    gains = [o["gain"] for o in ok]
    assert gains == sorted(gains, reverse=True)
    # the biggest propellers that fit hover on least power: the best is at the largest
    # diameter the clearance allows
    assert got["best"]["diameter_in"] == max(o["diameter_in"] for o in ok)
    # advice is off until the local validation: shown, nothing advised
    assert not got["advised"] and "validation" in got["why"]
    assert finding("f1", got, FLOWN, MASS, ["energy:flight"]) is None


def test_agility_ranks_by_thrust() -> None:
    got = search(basis(families=CALIBRATED), QUICK, "agility", MASS, RHO, DUTY)
    assert got["objective"] == "thrust" and got["best"]["gain"] > 0
    assert search(basis(motor=None), QUICK, "agility", MASS, RHO, DUTY)["reason"] == "needs_motor"
    calm = search(basis(), QUICK, "stability", MASS, RHO, DUTY)
    assert calm["options"] == [] and calm["reason"] == "no_priority"


def test_a_choice_that_fails_a_check_is_never_the_best() -> None:
    # a tighter frame: the bigger propellers no longer clear their neighbours
    got = search(basis(families=CALIBRATED, wheelbase_mm=850.0), QUICK, "endurance", MASS,
                 RHO, DUTY)  # fmt: skip
    assert got["best"] is not None and got["best"]["ok"]
    assert any("clearance" in o["failed"] for o in got["options"])
    assert all(o["ok"] for o in got["options"][: [o["ok"] for o in got["options"]].count(True)])


@pytest.mark.parametrize(
    ("advice", "kw", "advised", "why"),
    [
        ({"enabled": True, "validated_error": 0.01}, {}, True, None),
        ({"enabled": True, "validated_error": 0.5}, {}, False, "twice"),
        ({"enabled": True, "validated_error": None}, {}, False, "validation"),
        ({"enabled": True, "validated_error": 0.01}, {"at": None}, False, "anchored"),
        ({"enabled": True, "validated_error": 0.01}, {"families": {}}, False, "calibrated"),
    ],
)
def test_when_it_is_advised(advice: dict[str, Any], kw: dict[str, Any], advised: bool,
                            why: str | None) -> None:  # fmt: skip
    b = basis(**({"families": CALIBRATED} | kw))
    got = search(b, with_rules(QUICK, **advice), "endurance", MASS, RHO, DUTY)
    assert got["advised"] is advised
    if why:
        assert why in got["why"]
    reasons = {"twice": "within_error", "validation": "validation", "anchored": "not_calibrated",
               "calibrated": "not_calibrated", None: None}  # fmt: skip
    assert got["reason"] == reasons[why]
    f = finding("f1", got, FLOWN, MASS, ["energy:flight", "propeller:anchor"])
    if not advised:
        assert f is None
        return
    assert f is not None and f["code"] == PROP_CHOICE and f["changes"] == []
    assert f["params"]["flown"] == "22x6.6" and f["params"]["gain_pct"] > 2 * 1.0
    assert f["evidence"] == ["energy:flight", "propeller:anchor"]
