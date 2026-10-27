"""The no-GNSS analyzer on synthetic flights: a 60 s hover that loses GNSS, with the
estimator's own status (XKF4.SS) telling what it did, as ArduCopter 4.7.0 logs it."""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import flight

from flight_insights.ai.policy import Policy
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.gnss import GNSS
from flight_insights.analyzers.model import Result
from flight_insights.analyzers.no_gnss import NO_GNSS
from flight_insights.analyzers.registry import Registry
from flight_insights.knowledge import load_pack

ARM, DISARM = 1_000_000, 59_000_000
LAT, LON = 10.0, 20.0  # a synthetic origin
M = 1 / 111_320  # degrees of latitude per metre, near enough for a test
REL, ABS, USING, DR = 1 << 3, 1 << 4, 1 << 13, 1 << 18
GOOD = REL | ABS | USING | 0b111  # attitude, velocities
Columns = dict[str, np.ndarray[Any, Any]]


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def at(hz: float) -> np.ndarray[Any, Any]:
    return np.arange(0, 60_000_000, 1e6 / hz).astype(np.int64)


def during(t: np.ndarray[Any, Any], a: float, b: float) -> np.ndarray[Any, Any]:
    return (t >= a * 1e6) & (t < b * 1e6)


def hover(receivers: int = 1) -> dict[str, Columns]:
    g, e = at(5), at(10)
    n = len(g)
    return {
        "GPS": {"TimeUS": np.repeat(g, receivers), "I": np.tile(np.arange(receivers), n),
                "Status": np.full(n * receivers, 3), "NSats": np.full(n * receivers, 14),
                "Lat": np.full(n * receivers, LAT), "Lng": np.full(n * receivers, LON),
                "Alt": np.full(n * receivers, 110.0)},
        "POS": {"TimeUS": e, "Lat": np.full(len(e), LAT), "Lng": np.full(len(e), LON),
                "Alt": np.full(len(e), 110.0)},
        "XKF4": {"TimeUS": e, "C": np.zeros(len(e), np.int64), "PI": np.zeros(len(e), np.int64),
                 "SV": np.zeros(len(e)), "SP": np.zeros(len(e)), "SH": np.zeros(len(e)),
                 "SS": np.full(len(e), GOOD)},
    }  # fmt: skip


def table(c: Columns) -> pa.Table:
    return pa.Table.from_arrays([pa.array(v) for v in c.values()], names=list(c))


def build(cols: dict[str, Columns]) -> FlightData:
    return flight({name: table(c) for name, c in cols.items()}, ARM, DISARM)


def set_(cols: dict[str, Columns], name: str, column: str, a: float, b: float, value: Any,
         receiver: int | None = None) -> None:  # fmt: skip
    c = cols[name]
    rows = during(c["TimeUS"], a, b)
    if receiver is not None:
        rows &= c["I"] == receiver
    c[column] = c[column].astype(np.float64) if isinstance(value, float) else c[column]
    c[column][rows] = value


def lost_gnss(cols: dict[str, Columns], a: float, b: float) -> None:
    set_(cols, "GPS", "Status", a, b, 1)
    set_(cols, "GPS", "NSats", a, b, 0)


def run(cols: dict[str, Columns], reg: Registry) -> Result:
    return NO_GNSS.run(build(cols), reg)


def test_a_flight_with_gnss_all_along_has_no_outage(reg: Registry) -> None:
    out = run(hover(), reg)
    assert out.findings == [] and out.metrics == []


def test_gnss_lost_and_the_position_given_up(reg: Registry) -> None:
    """As the SITL drill shows: the estimator stops using GNSS 4 s after it goes, gives up
    its position 3 s later, and the EKF failsafe lands the aircraft."""
    cols = hover()
    lost_gnss(cols, 20, 40)
    set_(cols, "XKF4", "SS", 24, 27, REL | ABS | 0b111)  # no longer using GPS
    set_(cols, "XKF4", "SS", 27, 40.2, 0b001)  # attitude only
    cols["ERR"] = {
        "TimeUS": np.array([27_100_000]),
        "Subsys": np.array([17]),
        "ECode": np.array([1]),
    }
    cols["MODE"] = {
        "TimeUS": np.array([27_100_000]),
        "ModeNum": np.array([9]),
        "Rsn": np.array([6]),
    }
    out = run(cols, reg)
    [f] = out.findings
    assert (f.code, f.severity) == ("no_gnss.position_lost", "caution")
    assert f.params["what"] == "GNSS lost" and f.params["hold_s"] == 7.0
    assert f.params["response"] == (
        "the estimator stopped using GNSS after 4 s; it gave up its position after 7 s; "
        "EKFINAV failsafe triggered; the mode changed to LAND by EKF failsafe; "
        "GNSS was in use again 0.2 s after it returned"
    )
    metrics = {m.metric: (m.value, m.zone) for m in out.metrics}
    assert metrics == {"no_gnss.hold_s": (7.0, "out"), "no_gnss.detection_s": (4.0, "target")}
    assert not any(e.id.endswith(":drift") for e in out.evidence)  # no position to compare


def test_a_drill_on_optical_flow_keeps_a_relative_position(reg: Registry) -> None:
    """Source set 2 (flow) chosen with the aux 90 switch, then GNSS switched off with aux
    65 for 30 s: a relative position all along, 3 m from GNSS when it returns."""
    cols = hover()
    cols["AUXF"] = {"TimeUS": np.array([15_000_000, 20_000_000, 50_000_000]),
                    "function": np.array([90, 65, 65]), "pos": np.array([1, 2, 0])}  # fmt: skip
    set_(cols, "XKF4", "SS", 15, 60, REL | ABS | 0b111)  # set 2 does not use GPS
    set_(cols, "XKF4", "SS", 24, 52, REL | 0b111)
    lost_gnss(cols, 20, 50)
    set_(cols, "POS", "Lat", 20, 50, LAT + 3 * M)  # the estimate drifts 3 m north
    out = run(cols, reg)
    [f] = out.findings
    assert (f.code, f.params["what"]) == ("no_gnss.position_held", "GNSS-off drill")
    assert f.params["response"].startswith(
        "the estimator was not using GNSS when it went (source set 2); "
        "it kept a relative position throughout (absolute for 4 s)"
    )
    drift = next(e for e in out.evidence if e.id == "no_gnss:outage:1:drift")
    assert drift.values["drift_m"] == pytest.approx(3.0, abs=0.1)
    assert drift.values["drift_m_per_min"] == pytest.approx(6.0, abs=0.2)
    assert drift.values["minutes_to_tolerance"] == pytest.approx(10 / 6, abs=0.06)
    assert drift.values["dead_reckoning"] is False
    metrics = {m.metric: m.value for m in out.metrics}
    assert metrics["no_gnss.hold_s"] == 30.0  # kept a position it could fly on throughout
    assert "no_gnss.detection_s" not in metrics  # it was not using GNSS to begin with
    assert metrics["no_gnss.minutes_to_tolerance"] == drift.values["minutes_to_tolerance"]


def dead_reckoned(drift_m: float) -> dict[str, Columns]:
    """GNSS lost for 30 s in a hover with drag fusion: the estimator stops using it after
    4 s and dead-reckons on the wind from 10 s (XKF4.SS: relative position and bit 18, no
    absolute position), as the SITL study logged it; the estimate is `drift_m` off when
    GNSS returns."""
    cols = hover()
    lost_gnss(cols, 20, 50)
    set_(cols, "XKF4", "SS", 24, 30, REL | ABS | 0b111)
    set_(cols, "XKF4", "SS", 30, 50.4, REL | DR | 0b111)
    set_(cols, "POS", "Lat", 20, 50, LAT + drift_m * M)
    return cols


def test_dead_reckoning_drift_grows_with_the_cube_of_the_time(reg: Registry) -> None:
    """Nothing corrects the position or the tilt on dead reckoning, so the error grows
    with the cube of the time: 2.5 m after 30 s is 10 m after 30 * 4^(1/3) = 47.6 s, not
    the 120 s a drift per minute would say."""
    out = run(dead_reckoned(2.5), reg)
    [f] = out.findings
    assert f.code == "no_gnss.position_held"
    assert f.params["response"].startswith(
        "the estimator stopped using GNSS after 4 s; "
        "it kept a position on dead reckoning from 10 s (absolute for 10 s)"
    )
    [outage] = [e for e in out.evidence if e.id == "no_gnss:outage:1"]
    assert outage.values["dead_reckoning_s"] == 10.0
    drift = next(e for e in out.evidence if e.id == "no_gnss:outage:1:drift")
    assert drift.values["drift_m"] == pytest.approx(2.5, abs=0.1)
    assert drift.values["dead_reckoning"] is True
    assert "drift_m_per_min" not in drift.values  # it would understate a longer flight
    expected = 0.5 * (10 / drift.values["drift_m"]) ** (1 / 3)
    assert drift.values["minutes_to_tolerance"] == pytest.approx(expected, abs=0.05)
    assert "growing with the cube of the time flown" in drift.summary
    metrics = {m.metric: (m.value, m.zone) for m in out.metrics}
    assert metrics["no_gnss.minutes_to_tolerance"] == (drift.values["minutes_to_tolerance"],
                                                       "caution")  # fmt: skip


def test_a_dead_reckoned_outage_past_the_tolerance_comes_sooner_by_the_cube(
    reg: Registry,
) -> None:
    """Already 80 m off after 30 s: the tolerance came at 30 / 8^(1/3) = 15 s, which the
    simulator's curves bear out; a straight line would put it at 3.75 s."""
    drift = next(e for e in run(dead_reckoned(80), reg).evidence
                 if e.id == "no_gnss:outage:1:drift")  # fmt: skip
    assert drift.values["minutes_to_tolerance"] == pytest.approx(0.25, abs=0.05)


def test_what_of_a_dead_reckoned_outage_may_reach_the_ai(reg: Registry) -> None:
    """The outage's evidence reads no position, so when dead reckoning began (a time)
    goes with it; the drift's evidence is withheld but for its allowlisted numbers, so the
    drift in whole metres and the time to the tolerance pass, and its flag does not."""
    out = run(dead_reckoned(2.5), reg)
    policy = Policy.load(load_pack(), "4.7.0")
    [outage] = [e for e in out.evidence if e.id == "no_gnss:outage:1"]
    assert policy.evidence_clear(outage.kind, outage.messages, outage.fields)
    assert outage.values["dead_reckoning_s"] == 10.0
    [drift] = [e for e in out.evidence if e.id == "no_gnss:outage:1:drift"]
    assert not policy.evidence_clear(drift.kind, drift.messages, drift.fields)
    assert policy.derived(drift.values) == {
        "drift_m": round(drift.values["drift_m"]),
        "minutes_to_tolerance": drift.values["minutes_to_tolerance"],
    }


def test_no_time_to_the_tolerance_from_a_drift_too_small_to_read(reg: Registry) -> None:
    drift = next(e for e in run(dead_reckoned(0.0), reg).evidence
                 if e.id == "no_gnss:outage:1:drift")  # fmt: skip
    assert "minutes_to_tolerance" not in drift.values


def test_no_drift_from_a_return_that_cannot_be_trusted(reg: Registry) -> None:
    """A jammed receiver's first fix back can be kilometres off: drift is read only when
    GNSS holds its fix after returning and implies no impossible speed."""

    def flow_drill() -> dict[str, Columns]:
        cols = hover()
        cols["AUXF"] = {"TimeUS": np.array([20_000_000, 50_000_000]),
                        "function": np.array([65, 65]), "pos": np.array([2, 0])}  # fmt: skip
        set_(cols, "XKF4", "SS", 20, 52, REL | 0b111)
        lost_gnss(cols, 20, 50)
        return cols

    far = flow_drill()
    set_(far, "GPS", "Lat", 50, 50.3, LAT + 1000 * M)  # 1 km in 30 s: 33 m/s
    flicker = flow_drill()
    lost_gnss(flicker, 52, 52.5)  # the fix goes again within 3 s
    for cols in (far, flicker):
        out = run(cols, reg)
        assert [f.code for f in out.findings] == ["no_gnss.position_held"]
        assert not any(e.id.endswith(":drift") for e in out.evidence)


def test_a_second_receiver_with_a_fix_means_no_outage(reg: Registry) -> None:
    cols = hover(receivers=2)
    set_(cols, "GPS", "Status", 20, 40, 1, receiver=0)
    assert run(cols, reg).findings == []


def test_a_brief_loss_is_not_an_outage(reg: Registry) -> None:
    cols = hover()
    lost_gnss(cols, 20, 22)
    assert run(cols, reg).findings == []


def test_gnss_back_before_the_estimator_noticed(reg: Registry) -> None:
    cols = hover()
    lost_gnss(cols, 20, 23.5)
    out = run(cols, reg)
    [f] = out.findings
    assert f.code == "no_gnss.position_held"
    assert f.params["response"] == "it kept its position throughout"
    assert out.evidence[0].values["detection_s"] is None


def test_the_gnss_analyzer_leaves_a_drill_alone(reg: Registry) -> None:
    cols = hover()
    cols["AUXF"] = {"TimeUS": np.array([20_000_000, 40_000_000]), "function": np.array([65, 65]),
                    "pos": np.array([2, 0])}  # fmt: skip
    lost_gnss(cols, 20, 40)
    assert GNSS.run(build(cols), reg).findings == []
    del cols["AUXF"]  # the same loss with no switch is a disturbance
    assert [f.code for f in GNSS.run(build(cols), reg).findings] == ["gnss.jamming"]


def test_drift_stays_on_this_laptop(reg: Registry) -> None:
    cols = hover()
    cols["AUXF"] = {"TimeUS": np.array([20_000_000, 50_000_000]), "function": np.array([65, 65]),
                    "pos": np.array([2, 0])}  # fmt: skip
    set_(cols, "XKF4", "SS", 20, 52, REL | 0b111)
    lost_gnss(cols, 20, 50)
    set_(cols, "POS", "Lat", 20, 50, LAT + 3 * M)
    out = run(cols, reg)
    policy = Policy.load(load_pack(), "4.7.0")
    clear = {e.id: policy.evidence_clear(e.kind, e.messages, e.fields) for e in out.evidence}
    assert clear == {"no_gnss:outage:1": True, "no_gnss:outage:1:drift": False,
                     "no_gnss:flight": True, "no_gnss:flight:drift": False,
                     "no_gnss:range": False}  # fmt: skip
    for f in out.findings:
        assert all("drift" not in k for k in f.params)


def test_the_range_is_the_farthest_it_flew_from_where_it_armed(reg: Registry) -> None:
    """Measured whatever its outages, for the recovery card's trip home on dead
    reckoning; from where it armed, not from the first fix."""
    cols = hover()
    set_(cols, "POS", "Lat", 0, ARM / 1e6, LAT - 500 * M)  # parked away before arming
    set_(cols, "POS", "Lat", 30, 40, LAT + 120 * M)
    set_(cols, "POS", "Lng", 30, 40, LON)
    out = run(cols, reg)
    [rng] = [e for e in out.evidence if e.id == "no_gnss:range"]
    assert abs(rng.values["farthest_m"] - 120) <= 2  # M is near enough, not exact
    assert out.evidence[-1] is rng  # after the outages: the others keep their places
    assert rng.fields == ("Lat", "Lng")
