"""Readiness for flying without GNSS, from the parameters: a stock ArduCopter 4.7.0 quad,
then the sets, sensors, switches and settings that change each area."""

from __future__ import annotations

from typing import Any

import pytest
from flight_builder import hover_flight

from flight_insights.ai.policy import Policy
from flight_insights.analyzers.model import Result
from flight_insights.analyzers.no_gnss_readiness import NO_GNSS_READINESS
from flight_insights.analyzers.registry import Registry
from flight_insights.knowledge import load_pack
from flight_insights.review import FILE_ANALYZERS

SOURCES = {f"EK3_SRC{n}_{f}": 0.0 for n in (2, 3) for f in ("POSXY", "VELXY", "VELZ", "YAW")}
STOCK = {  # the 4.7.0 defaults that matter here: GNSS only, no drag fusion
    "AHRS_EKF_TYPE": 3.0, "EK3_SRC1_POSXY": 3.0, "EK3_SRC1_VELXY": 3.0, "EK3_SRC1_POSZ": 1.0,
    "EK3_SRC1_VELZ": 3.0, "EK3_SRC1_YAW": 1.0, **SOURCES, "EK3_SRC2_POSZ": 1.0,
    "EK3_SRC3_POSZ": 1.0, "EK3_FLOW_USE": 1.0, "FLOW_TYPE": 0.0, "RNGFND1_TYPE": 0.0,
    "VISO_TYPE": 0.0, "BCN_TYPE": 0.0, "EK3_DRAG_BCOEF_X": 0.0, "EK3_DRAG_BCOEF_Y": 0.0,
    "EK3_DRAG_MCOEF": 0.0, "COMPASS_ENABLE": 1.0, "COMPASS_USE": 1.0, "COMPASS_USE2": 1.0,
    "COMPASS_USE3": 1.0, "FS_EKF_ACTION": 1.0, "FS_EKF_THRESH": 0.8, "FS_DR_ENABLE": 2.0,
    "FS_DR_TIMEOUT": 30.0, "FLTMODE_CH": 5.0, "RC_OPTIONS": 32.0, "SCR_ENABLE": 0.0,
    **{f"FLTMODE{i}": 0.0 for i in range(1, 7)},
}  # fmt: skip
FLOW = {"EK3_SRC2_VELXY": 5.0, "EK3_SRC2_YAW": 1.0, "FLOW_TYPE": 6.0, "RNGFND1_TYPE": 8.0,
        "RNGFND1_ORIENT": 25.0}  # fmt: skip
DRAG = {"EK3_DRAG_BCOEF_X": 70.0, "EK3_DRAG_BCOEF_Y": 66.0, "EK3_DRAG_MCOEF": 0.2}
POSITION_MODES = {f"FLTMODE{i}": m for i, m in enumerate((5.0, 16.0, 6.0, 3.0, 4.0, 9.0), 1)}


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def run(reg: Registry, **params: float) -> Result:
    return NO_GNSS_READINESS.run(hover_flight({}, STOCK | params), reg)


def found(r: Result) -> dict[str, dict[str, Any]]:
    return {f.code: dict(f.params) | {"severity": f.severity} for f in r.findings}


def areas(r: Result) -> dict[str, str]:
    prefix = "no_gnss:readiness:"
    return {e.id[len(prefix) :]: e.values["status"] for e in r.evidence if e.id.startswith(prefix)}


def area(r: Result, name: str) -> dict[str, Any]:
    return dict(next(e.values for e in r.evidence if e.id == f"no_gnss:readiness:{name}"))


def holds(values: dict[str, Any], **expected: Any) -> bool:
    return all(values.get(k) == v for k, v in expected.items())


def test_a_stock_quad_keeps_nothing_without_gnss(reg: Registry) -> None:
    r = run(reg)
    assert areas(r) == {
        "position": "none", "switching": "none", "heading": "ready", "height": "ready",
        "wind": "gap", "failsafes": "ready", "pilot_fallback": "ready",
    }  # fmt: skip
    assert found(r) == {"no_gnss.drag_fusion_off": {"severity": "info"}}
    assert area(r, "pilot_fallback")["modes"] == ["Stabilize"]
    assert area(r, "failsafes")["dead_reckoning_failsafe_acts"] is False


def test_optical_flow_on_a_switch_with_drag_fusion_is_ready(reg: Registry) -> None:
    r = run(reg, **FLOW, **DRAG, RC8_OPTION=90.0, RC7_OPTION=65.0)
    assert set(areas(r).values()) == {"ready"}
    assert found(r) == {}
    assert holds(area(r, "position"), sets=[2], sensors=["optical_flow"], missing=[])
    assert area(r, "switching")["switch_channels"] == [8]
    assert area(r, "pilot_fallback")["drill_switch_channels"] == [7]
    assert area(r, "failsafes")["dead_reckoning_failsafe_acts"] is True


def test_a_flow_set_with_nothing_set_up_and_nothing_to_select_it(reg: Registry) -> None:
    r = run(reg, **FLOW | {"FLOW_TYPE": 0.0, "RNGFND1_ORIENT": 0.0}, EK3_FLOW_USE=2.0)
    f = found(r)
    assert f["no_gnss.source_not_fitted"] == {
        "set": 2, "missing": "FLOW_TYPE, RNGFNDn_TYPE, EK3_FLOW_USE 1", "severity": "caution",
    }  # fmt: skip
    assert f["no_gnss.no_source_switch"] == {"set": "2", "severity": "caution"}
    assert area(r, "position")["status"] == "gap"
    assert area(r, "position")["missing"][0] == "set 2: optical flow (FLOW_TYPE)"


def test_a_source_set_one_that_copes_needs_no_switch(reg: Registry) -> None:
    flow_first = {"EK3_SRC1_POSXY": 0.0, "EK3_SRC1_VELXY": 5.0} | {
        k: v for k, v in FLOW.items() if not k.startswith("EK3_SRC2")
    }
    r = run(reg, **flow_first)
    assert "no_gnss.no_source_switch" not in found(r)
    assert holds(area(r, "switching"), status="ready", needed=False)


@pytest.mark.parametrize(
    ("params", "risky"),
    [({"EK3_SRC1_YAW": 2.0}, True),  # GPS yaw alone (moving baseline)
     ({"EK3_SRC1_YAW": 8.0}, True),  # the GSF yaw estimator runs on GPS velocity
     ({"EK3_SRC1_YAW": 3.0}, False),  # GPS with the compass to fall back on
     ({"EK3_SRC1_YAW": 3.0, "COMPASS_USE": 0.0, "COMPASS_USE2": 0.0, "COMPASS_USE3": 0.0},
      True)],  # ... but no compass in use
)  # fmt: skip
def test_a_heading_that_goes_with_gnss(
    reg: Registry, params: dict[str, float], risky: bool
) -> None:
    r = run(reg, **params)
    assert ("no_gnss.heading_needs_gnss" in found(r)) is risky
    if risky:
        assert found(r)["no_gnss.heading_needs_gnss"]["yaw"] == params["EK3_SRC1_YAW"]


def test_the_flow_set_takes_its_heading_from_gnss(reg: Registry) -> None:
    r = run(reg, **FLOW | {"EK3_SRC2_YAW": 2.0}, RC8_OPTION=90.0)
    assert holds(found(r)["no_gnss.heading_needs_gnss"], set=2, yaw=2.0)


def test_drag_fusion_follows_the_firmwares_thresholds(reg: Registry) -> None:
    def off(**params: float) -> bool:
        return "no_gnss.drag_fusion_off" in found(run(reg, **params))

    assert off(EK3_DRAG_BCOEF_X=1.0)  # enabled above 1, not at it
    assert not off(EK3_DRAG_BCOEF_X=1.5)
    assert not off(EK3_DRAG_MCOEF=0.1)
    assert off(EK3_DRAG_MCOEF=0.001)
    flies = hover_flight({}, STOCK, None, {"flies_without_gnss": True})
    severe = NO_GNSS_READINESS.run(flies, reg)
    assert found(severe)["no_gnss.drag_fusion_off"]["severity"] == "caution"


def test_no_drag_fusion_to_judge_on_a_helicopter_or_a_small_build(reg: Registry) -> None:
    heli = run(reg, FRAME_CLASS=6.0)
    assert area(heli, "wind")["status"] == "unavailable" and found(heli) == {}
    small = hover_flight({}, {k: v for k, v in STOCK.items() if "DRAG" not in k})
    r = NO_GNSS_READINESS.run(small, reg)  # a 1 MB board's build has no drag fusion
    assert area(r, "wind")["status"] == "unavailable" and found(r) == {}


def test_no_mode_without_a_position_on_a_switch(reg: Registry) -> None:
    r = run(reg, **POSITION_MODES)
    assert found(r) == {"no_gnss.drag_fusion_off": {"severity": "info"},
                        "no_gnss.no_pilot_fallback": {"severity": "info"}}  # fmt: skip
    assert area(run(reg, **POSITION_MODES, RC9_OPTION=70.0), "pilot_fallback")["modes"] == [
        "AltHold"
    ]
    station = run(reg, **POSITION_MODES, RC_OPTIONS=33.0)  # the receiver ignored
    assert holds(area(station, "pilot_fallback"), status="ready", ground_station_control=True)
    # FlowHold counts only with optical flow fitted
    flowhold = POSITION_MODES | {"FLTMODE6": 22.0}
    assert "no_gnss.no_pilot_fallback" in found(run(reg, **flowhold))
    assert "no_gnss.no_pilot_fallback" not in found(run(reg, **flowhold, FLOW_TYPE=6.0))


def test_an_ekf_failsafe_that_only_reports(reg: Registry) -> None:
    r = run(reg, FS_EKF_ACTION=0.0)
    assert holds(area(r, "failsafes"), status="gap", ekf_action="report only")


def test_without_ekf3_source_sets_nothing_is_judged(reg: Registry) -> None:
    assert run(reg, AHRS_EKF_TYPE=11.0).skipped
    plane = NO_GNSS_READINESS.run(hover_flight({}, STOCK, {"vehicle": "plane"}), reg)
    assert plane.skipped


def test_a_parameter_file_gets_its_readiness_and_the_ai_may_read_it(reg: Registry) -> None:
    assert NO_GNSS_READINESS in FILE_ANALYZERS
    r = run(reg, **FLOW)
    policy = Policy.load(load_pack(), "4.7.0")
    assert all(policy.evidence_clear(e.kind, e.messages, e.fields) for e in r.evidence)
