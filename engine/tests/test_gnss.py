"""The GNSS analyzer on synthetic flights: a clean 60 s hover, then one disturbance at a
time, each planted in the channels it would show in, and the class it must get."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import flight

from flight_insights.ai.policy import Policy
from flight_insights.analyzers import gnss
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.gnss import GNSS, Trip
from flight_insights.analyzers.model import Result
from flight_insights.analyzers.registry import Registry
from flight_insights.knowledge import load_pack

ARM, DISARM = 1_000_000, 59_000_000
LAT, LON = 10.0, 20.0  # a synthetic origin
M = 1 / 111_320  # degrees of latitude per metre, near enough for a test
Columns = dict[str, np.ndarray[Any, Any]]


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def at(hz: float) -> np.ndarray[Any, Any]:
    return np.arange(0, 60_000_000, 1e6 / hz).astype(np.int64)


def during(t: np.ndarray[Any, Any], a: float, b: float) -> np.ndarray[Any, Any]:
    return (t >= a * 1e6) & (t < b * 1e6)


def sky(receivers: int = 1, ublox: bool = False) -> dict[str, Columns]:
    """A clean hover over the origin: every receiver with a 3D fix on 14 satellites."""
    rng = np.random.default_rng(7)
    g, e = at(5), at(10)
    n = len(g)
    gps: Columns = {
        "TimeUS": np.repeat(g, receivers), "I": np.tile(np.arange(receivers), n),
        "Status": np.full(n * receivers, 3), "NSats": np.full(n * receivers, 14),
        "GWk": np.full(n * receivers, 2400), "GMS": np.repeat(g // 1000 + 100_000, receivers),
        "Lat": np.full(n * receivers, LAT), "Lng": np.full(n * receivers, LON),
        "Alt": np.full(n * receivers, 110.0), "Spd": np.zeros(n * receivers),
        "VZ": np.zeros(n * receivers),
    }  # fmt: skip
    out = {
        "GPS": gps,
        "GPA": {"TimeUS": gps["TimeUS"], "I": gps["I"], "HAcc": np.full(n * receivers, 0.8)},
        "POS": {"TimeUS": e, "Lat": np.full(len(e), LAT), "Lng": np.full(len(e), LON),
                "Alt": np.full(len(e), 110.0)},
        "XKF4": {"TimeUS": e, "C": np.zeros(len(e), np.int64), "PI": np.zeros(len(e), np.int64),
                 "SV": np.full(len(e), 5), "SP": np.full(len(e), 8), "SH": np.full(len(e), 3)},
        "XKF3": {"TimeUS": e, "C": np.zeros(len(e), np.int64),
                 "IVN": rng.normal(0, 0.05, len(e)), "IVE": rng.normal(0, 0.05, len(e))},
        "BARO": {"TimeUS": e, "I": np.zeros(len(e), np.int64), "Alt": np.full(len(e), 10.0)},
        "BAT": {"TimeUS": e, "Inst": np.zeros(len(e), np.int64),
                "Curr": 20 + rng.normal(0, 0.5, len(e))},
    }  # fmt: skip
    if ublox:
        u = at(1)
        out["UBX1"] = {"TimeUS": u, "Instance": np.zeros(len(u), np.int64),
                       "jamInd": np.full(len(u), 20), "agcCnt": np.full(len(u), 5000)}  # fmt: skip
    return out


def _table(columns: dict[str, pa.Array[Any]]) -> pa.Table:
    return pa.Table.from_arrays(list(columns.values()), names=list(columns))


def build(cols: dict[str, Columns], messages: tuple[tuple[float, str], ...] = ()) -> FlightData:
    tables = {name: _table({k: pa.array(v) for k, v in c.items()}) for name, c in cols.items()}
    if messages:
        tables["MSG"] = _table({
            "TimeUS": pa.array([int(s * 1e6) for s, _ in messages], pa.uint64()),
            "Message": pa.array([m.encode() for _, m in messages], pa.binary()),
        })  # fmt: skip
    return flight(tables, ARM, DISARM)


def run(cols: dict[str, Columns], reg: Registry, **kw: Any) -> Result:
    return GNSS.run(build(cols, **kw), reg)


def codes(out: Result) -> list[tuple[str, str]]:
    return [(f.code, f.confidence) for f in out.findings]


def edit(
    cols: dict[str, Columns], name: str, column: str, a: float, b: float,
    value: Callable[[np.ndarray[Any, Any]], Any] | float, receiver: int | None = None,
) -> None:  # fmt: skip
    c = cols[name]
    rows = during(c["TimeUS"], a, b)
    if receiver is not None:
        rows &= c.get("I", c.get("Instance")) == receiver
    c[column] = c[column].astype(np.float64) if isinstance(value, float) else c[column]
    c[column][rows] = value(c[column][rows]) if callable(value) else value


# --- a clean flight --------------------------------------------------------------------------


def test_a_clean_hover_has_no_disturbance(reg: Registry) -> None:
    out = run(sky(), reg)
    assert out.findings == []
    metrics = {m.metric: (m.value, m.zone) for m in out.metrics}
    assert metrics == {"gnss.disturbed_s": (0, "target"), "gnss.sats_min": (14, "target"),
                       "gnss.hacc_p95": (0.8, "target")}  # fmt: skip


def test_no_receiver_is_skipped(reg: Registry) -> None:
    cols = sky()
    del cols["GPS"]
    assert run(cols, reg).skipped == "the log holds no GNSS receiver"


# --- jamming and its relatives -----------------------------------------------------------


def test_ublox_jamming_with_the_signal_lost(reg: Registry) -> None:
    cols = sky(ublox=True)
    edit(cols, "UBX1", "jamInd", 20, 35, 160)  # the CW indicator, 0 to 255
    edit(cols, "UBX1", "agcCnt", 20, 35, 1500)  # gain turned down against the jammer
    edit(cols, "GPS", "NSats", 21, 34, 4)
    edit(cols, "GPA", "HAcc", 22, 34, 25.0)
    out = run(cols, reg)
    assert codes(out) == [("gnss.jamming", "high")]
    ev = out.evidence[0].values
    assert ev["channels"] == ["sats", "accuracy", "cw", "agc"]
    assert ev["highest_cw"] == 160 and ev["lowest_agc_ratio"] == pytest.approx(0.3)
    assert ev["start_s"] == pytest.approx(19.0) and ev["duration_s"] == pytest.approx(15.0, abs=0.3)


def test_an_m10_reports_its_jamming_state_not_a_cw_level(reg: Registry) -> None:
    cols = sky(ublox=True)
    cols["UBX1"]["jamInd"] = np.ones(len(cols["UBX1"]["TimeUS"]), np.int64)  # 1: OK
    edit(cols, "UBX1", "jamInd", 20, 30, 3)  # 3: critical, which on MON-HW would be nothing
    hw = [(0.5, "u-blox 1 HW: 000A0000 SW: ROM SPG 5.10")]
    out = run(cols, reg, messages=tuple(hw))
    assert codes(out) == [("gnss.jamming", "medium")]
    assert out.evidence[0].values["worst_jam_state"] == 3
    assert codes(run(cols, reg)) == []  # without the version message it reads as a low CW level


def test_interference_that_follows_motor_current_is_onboard(reg: Registry) -> None:
    cols = sky(ublox=True)
    edit(cols, "BAT", "Curr", 20, 35, 45.0)  # climbing hard
    edit(cols, "UBX1", "jamInd", 20, 35, 120)
    edit(cols, "GPA", "HAcc", 21, 34, 6.0)
    out = run(cols, reg)
    assert codes(out) == [("gnss.onboard", "medium")]
    assert out.findings[0].params["r"] > 0.9


def test_satellites_lost_with_the_accuracy_held_is_an_obstruction(reg: Registry) -> None:
    cols = sky()
    edit(cols, "GPS", "NSats", 20, 40, 5)
    assert codes(run(cols, reg)) == [("gnss.obstruction", "low")]
    cols = sky(ublox=True)  # with the band seen quiet, it is the likelier answer
    edit(cols, "GPS", "NSats", 20, 40, 5)
    edit(cols, "GPS", "Status", 25, 38, 1)
    assert codes(run(cols, reg)) == [("gnss.obstruction", "medium")]


def test_a_fix_lost_for_long_with_no_receiver_status_is_jamming(reg: Registry) -> None:
    cols = sky()
    edit(cols, "GPS", "Status", 20, 40, 1)
    edit(cols, "GPS", "NSats", 20, 40, 3)
    assert codes(run(cols, reg)) == [("gnss.jamming", "medium")]


def test_a_fix_that_flickers_is_lost_all_along(reg: Registry) -> None:
    """As SITL's jammer does: the fix drops for 0.6 s in every 2 s, the position wanders
    off when it comes back, and the estimator rejects it. All of it is the jamming."""
    cols = sky()
    for start in np.arange(20, 40, 2.0):
        edit(cols, "GPS", "Status", start, start + 0.6, 1)
        edit(cols, "GPS", "NSats", start, start + 0.6, 3)
        edit(cols, "GPS", "Lat", start + 0.6, start + 2, LAT + 300 * M)
    edit(cols, "XKF4", "SP", 21, 40, 4000)
    out = run(cols, reg)
    assert codes(out) == [("gnss.jamming", "medium")]
    assert {"fix", "rejected", "track"} <= set(out.evidence[0].values["channels"])


def test_one_receiver_alone_degrading_is_a_receiver_fault(reg: Registry) -> None:
    cols = sky(receivers=2)
    edit(cols, "GPS", "Status", 20, 35, 1, receiver=1)
    edit(cols, "GPS", "NSats", 20, 35, 2, receiver=1)
    out = run(cols, reg)
    assert codes(out) == [("gnss.receiver", "low")]
    assert out.findings[0].params["receivers"] == "GPS 2"


# --- spoofing ------------------------------------------------------------------------------


def test_a_position_jump_with_a_healthy_signal_is_spoofing(reg: Registry) -> None:
    cols = sky()
    edit(cols, "GPS", "Lat", 20, 30, LAT + 100 * M)  # 100 m north, fix and accuracy fine
    edit(cols, "XKF4", "SP", 20, 30, 2000)  # logged as 100 times the test ratio: 20
    out = run(cols, reg)
    assert codes(out) == [("gnss.spoofing", "medium")]
    assert out.findings[0].severity == "safety"
    main, position = out.evidence[0], out.evidence[1]
    assert main.values["highest_test_ratio"] == pytest.approx(20)
    assert main.values["rejected_s"] == pytest.approx(10, abs=0.2)
    assert position.values["largest_track_gap_m"] == pytest.approx(100, abs=1)
    assert "100" not in str(out.findings[0].params)  # metres stay in the position evidence


def test_independent_checks_agreeing_make_spoofing_likely(reg: Registry) -> None:
    cols = sky()
    edit(cols, "GPS", "Lat", 20, 30, LAT + 100 * M)
    edit(cols, "GPS", "Alt", 20, 30, 140.0)  # 30 m up, while the barometer holds
    edit(cols, "XKF4", "SP", 20, 30, 2000)
    out = run(cols, reg)
    assert codes(out) == [("gnss.spoofing", "high")]
    assert out.evidence[2].id == "gnss:event:1:altitude"
    assert out.evidence[2].values["largest_altitude_gap_m"] == 30


def test_the_barometer_reading_high_in_fast_flight_is_not_spoofing(reg: Registry) -> None:
    """A static port's error grows with the dynamic pressure: a heavy-lift quad's barometer
    read 12 to 14 m off GNSS at 22 to 35 m/s, with the signal healthy throughout."""
    cols = sky()
    edit(cols, "GPS", "Spd", 18, 36, 33.0)
    edit(cols, "GPS", "Alt", 20, 34, 124.0)  # 14 m above the usual offset
    assert codes(run(cols, reg)) == []


def test_the_same_gap_in_a_hover_is_spoofing(reg: Registry) -> None:
    cols = sky()
    edit(cols, "GPS", "Alt", 20, 34, 124.0)
    out = run(cols, reg)
    assert codes(out) == [("gnss.spoofing", "medium")]
    assert out.findings[0].params["channels"] == "altitude left the barometer's"


def test_a_jump_beyond_the_barometer_error_at_speed_still_counts(reg: Registry) -> None:
    cols = sky()
    edit(cols, "GPS", "Spd", 18, 36, 33.0)  # allows 10 + 0.5 x 33^2 / 2g, about 38 m
    edit(cols, "GPS", "Alt", 20, 34, 160.0)  # 50 m
    out = run(cols, reg)
    assert codes(out) == [("gnss.spoofing", "medium")]
    assert out.evidence[1].values["largest_altitude_gap_m"] == 50


def test_a_slow_drift_the_estimator_follows(reg: Registry) -> None:
    """The dangerous form: the estimator takes the drifting position, so it never stands
    apart from GNSS and no test ratio trips; only the one-sided innovation shows it."""
    cols = sky()
    edit(cols, "XKF3", "IVN", 20, 50, lambda v: v + 0.4)
    out = run(cols, reg)
    assert codes(out) == [("gnss.spoofing", "medium")]
    params = out.findings[0].params
    assert params["channels"] == "estimator dragged by a drift"
    assert "never rejected it" in params["response"]
    assert out.evidence[0].values["followed_drift"] is True


def test_gps_time_jumping_against_the_logs_clock(reg: Registry) -> None:
    cols = sky()
    edit(cols, "GPS", "GMS", 30, 60, lambda v: v + 5000)  # 5 s ahead from 30 s on
    out = run(cols, reg)
    assert codes(out) == [("gnss.spoofing", "low")]
    time = next(e for e in out.evidence if e.id == "gnss:event:1:time")
    assert time.values["largest_time_jump_s"] == pytest.approx(5.0, abs=0.01)


def test_gps_time_standing_still_while_the_clock_runs(reg: Registry) -> None:
    cols = sky()
    edit(cols, "GPS", "GMS", 30, 35, 130_000)
    trips = gnss.signal(
        build(cols), reg.rules["gnss"], gnss.receivers(build(cols), reg.rules["gnss"])
    )
    stopped = [t for t in trips if t.channel == "time_stopped"]
    # the jump when it starts again is GPS time catching up, not a jump
    assert len(stopped) == 1 and stopped[0].peak == pytest.approx(5.0, abs=0.3)
    assert not [t for t in trips if t.channel == "time_jump"]


def test_one_receiver_leaving_the_other_is_spoofing(reg: Registry) -> None:
    cols = sky(receivers=2)
    edit(cols, "GPS", "Lat", 20, 30, LAT + 60 * M, receiver=1)
    out = run(cols, reg)
    assert codes(out) == [("gnss.spoofing", "high")]
    assert {"receivers", "track"} <= set(out.evidence[0].values["channels"])


def test_a_heading_reset_from_gnss_during_spoofing(reg: Registry) -> None:
    cols = sky()
    edit(cols, "GPS", "Lat", 20, 30, LAT + 100 * M)
    edit(cols, "XKF4", "SP", 20, 30, 2000)
    out = run(cols, reg, messages=((22.0, "EKF3 IMU0 emergency yaw reset"),))
    assert codes(out) == [("gnss.spoofing", "medium"), ("gnss.yaw_reset", "medium")]
    assert out.findings[1].severity == "safety" and out.findings[1].params["at_s"] == 21.0


def test_the_logged_test_ratio_is_a_hundred_times_the_ratio(reg: Registry) -> None:
    for logged, rejected in ((90, False), (110, True)):
        cols = sky()
        edit(cols, "XKF4", "SP", 20, 30, logged)
        trips = gnss.estimator(build(cols), reg.rules["gnss"], reg.names("4.7.0"))
        assert any(t.channel == "rejected" for t in trips) is rejected
    # the same stored 150 is a ratio of 1.5 whether the log's multiplier says 1, as
    # 4.7.0's does, or 0.01, as a log whose metadata scales the field would
    cols = sky()
    edit(cols, "XKF4", "SP", 20, 30, 150)
    x4 = build(cols).table("XKF4")
    assert x4 is not None
    i = x4.schema.get_field_index("SP")
    rules = reg.rules["gnss"]["consistency"]
    for multiplier in (b"1", b"0.01"):
        field = cast(Any, x4.schema).field(i).with_metadata({b"multiplier": multiplier})
        ratio = gnss.logged_ratio(x4.set_column(i, field, x4.column(i)), "SP", rules)
        assert ratio.max() == pytest.approx(1.5)


# --- how the aircraft coped --------------------------------------------------------------------


def test_the_response_names_the_failsafe_and_the_mode(reg: Registry) -> None:
    cols = sky()
    edit(cols, "GPS", "Status", 20, 40, 1)
    edit(cols, "GPS", "NSats", 20, 40, 3)
    cols["ERR"] = {
        "TimeUS": np.array([25_000_000]),
        "Subsys": np.array([17]),
        "ECode": np.array([1]),
    }
    cols["MODE"] = {
        "TimeUS": np.array([25_100_000]),
        "ModeNum": np.array([9]),
        "Rsn": np.array([6]),
    }
    out = run(cols, reg)
    assert out.findings[0].params["response"] == (
        "the estimator did not reject GNSS; EKFINAV failsafe triggered; "
        "the mode changed to LAND by EKF failsafe"
    )


# --- grouping ------------------------------------------------------------------------------


def test_trips_within_the_merge_window_are_one_event() -> None:
    a, b, c = Trip("fix", 0, 2_000_000, 1, 0), Trip("rejected", 6_000_000, 7_000_000, 5.0), Trip(
        "sats", 13_000_000, 14_000_000, 3, 0)  # fmt: skip
    assert gnss.group([c, a, b], 5e6) == [[a, b], [c]]


# --- privacy -----------------------------------------------------------------------------------


def test_only_evidence_without_positions_reaches_the_ai_path(reg: Registry) -> None:
    """Metres between position estimates, altitudes, GPS time and status text are read
    on this laptop; the evidence holding them cites those fields, so the location policy
    withholds its values. The event itself (times, class, channels, ratios) is clear."""
    cols = sky(receivers=2)
    edit(cols, "GPS", "Lat", 20, 30, LAT + 100 * M)
    edit(cols, "GPS", "Alt", 20, 30, 140.0)
    edit(cols, "GPS", "GMS", 30, 60, lambda v: v + 5000)
    edit(cols, "XKF4", "SP", 20, 30, 2000)
    out = run(cols, reg, messages=((22.0, "EKF3 IMU0 emergency yaw reset"),))
    policy = Policy.load(load_pack(), "4.7.0")
    clear = {e.id: policy.evidence_clear(e.kind, e.messages, e.fields) for e in out.evidence}
    assert clear == {
        "gnss:event:1": True, "gnss:event:1:position": False, "gnss:event:1:altitude": False,
        "gnss:event:1:time": False, "gnss:event:1:yaw": False, "gnss:flight": True,
    }  # fmt: skip
    for f in out.findings:  # what a finding says is times, counts and words
        assert all(not k.endswith("_m") for k in f.params)
