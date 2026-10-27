"""The AutoTune review: sessions read from MODE, EV and ATUN on flights whose outcome is
known, an ESC desync during a session, and the simulator's roll AutoTune saved and
stopped part-way."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from flight_builder import ARM, DISARM, HOVER, hover_flight, table
from test_motors_health import QUAD, esc, rcou

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.autotune import AUTOTUNE
from flight_insights.analyzers.model import Result
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
ALT_HOLD, AUTOTUNE_MODE, LAND = 2, 15, 9
INITIALISED, OFF, SUCCESS, FAILED, LIMIT, SAVED = 30, 31, 33, 34, 35, 37
START, END = 8_000_000, 30_000_000  # the session, inside the hover
ROLL, PITCH, YAW = 0, 1, 2
D_UP, D_DOWN, P_UP, ANGLE_UP = 0, 1, 2, 5
GAINS = {"ATC_RAT_RLL_P": 0.054, "ATC_RAT_RLL_D": 0.0014, "ATC_RAT_PIT_P": 0.054,
         "ATC_RAT_PIT_D": 0.0014, "ATC_RAT_YAW_P": 0.18, "AUTOTUNE_MIN_D": 0.0005}  # fmt: skip


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def at(s: float) -> int:
    return int(START + s * 1e6)


def tune_flight(
    rows: Sequence[tuple[float, int, int, float, float]],
    events: Sequence[tuple[float, int]],
    modes: list[tuple[int, int]] | None = None,
    extra: dict[str, pa.Table] | None = None,
    answers: dict[str, Any] | None = None,
    **params: float,
) -> FlightData:
    """ATUN rows (seconds into the session, axis, step, rate P, rate D) and EV events
    (seconds into the session, id); AutoTune from START to END, then LAND by default."""
    modes = modes or [(ARM, ALT_HOLD), (START, AUTOTUNE_MODE), (END, LAND)]
    atun = table(
        [at(r[0]) for r in rows],
        {"Axis": (pa.uint8(), [r[1] for r in rows]), "TuneStep": (pa.uint8(), [r[2] for r in rows]),
         "RP": (pa.float32(), [r[3] for r in rows]), "RD": (pa.float32(), [r[4] for r in rows])},
    )  # fmt: skip
    tables = {
        "MODE": table([m[0] for m in modes], {"ModeNum": (pa.uint8(), [m[1] for m in modes])}),
        "EV": table([at(e[0]) for e in events], {"Id": (pa.uint8(), [e[1] for e in events])}),
        "ATUN": atun,
        **(extra or {}),
    }
    return hover_flight(tables, QUAD | GAINS | params, answers=answers)


def roll_tuned(p: float = 0.118, d: float = 0.0009) -> list[tuple[float, int, int, float, float]]:
    """A roll tune's gain updates through its steps, ending at rate P `p` and D `d`."""
    return [(1, ROLL, D_UP, 0.054, 0.0020), (4, ROLL, D_DOWN, 0.06, d), (8, ROLL, P_UP, p, d),
            (12, ROLL, ANGLE_UP, p, d)]  # fmt: skip


def codes(out: Result) -> dict[str, dict[str, Any]]:
    return {f.code: dict(f.params) for f in out.findings}


# --- how a session ended -------------------------------------------------------------------


def test_a_finished_tune_saved_on_disarm_reports_its_gains(reg: Registry) -> None:
    """Disarmed still in AutoTune: SAVEDGAINS at disarm, after the session's last row."""
    modes = [(ARM, ALT_HOLD), (START, AUTOTUNE_MODE)]
    flight = tune_flight(roll_tuned(), [(0, INITIALISED), (13, SUCCESS)], modes,
                         AUTOTUNE_AXES=1.0)  # fmt: skip
    flight = replace(flight, disarm_us=END)
    flight.cache.tables["EV"] = table(  # type: ignore[union-attr]
        [at(0), at(13), END], {"Id": (pa.uint8(), [INITIALISED, SUCCESS, SAVED])}
    )
    out = AUTOTUNE.run(flight, reg)
    assert codes(out) == {"autotune.saved": {"axes": "roll"}}
    [ev] = out.evidence
    assert ev.values["outcome"] == "finished" and ev.values["saved"] is True
    roll = ev.values["gains"]["roll"]
    assert roll["p_before"] == pytest.approx(0.054) and roll["p_after"] == pytest.approx(0.118)
    assert roll["p_change_pct"] == pytest.approx(118.5, abs=0.2)
    assert roll["d_before"] == pytest.approx(0.0014) and roll["d_after"] == pytest.approx(0.0009)
    assert ev.values["steps"]["roll"] == ["rate D up", "rate D down", "rate P up", "angle P up"]


def test_a_finished_tune_disarmed_in_another_mode_is_not_saved(reg: Registry) -> None:
    out = AUTOTUNE.run(tune_flight(roll_tuned(), [(0, INITIALISED), (13, SUCCESS)],
                                   AUTOTUNE_AXES=1.0), reg)  # fmt: skip
    assert codes(out) == {"autotune.not_saved": {"axes": "roll"}}


def test_stopped_part_way_keeps_nothing_and_names_what_is_left(reg: Registry) -> None:
    """Roll finished, pitch started, then the pilot left AutoTune: no save on disarm."""
    rows = [*roll_tuned(), (14, PITCH, D_UP, 0.054, 0.002)]
    out = AUTOTUNE.run(tune_flight(rows, [(0, INITIALISED), (20, OFF)], AUTOTUNE_AXES=3.0), reg)
    assert codes(out) == {
        "autotune.not_saved": {"axes": "roll"},
        "autotune.incomplete": {"remaining": "pitch"},
    }
    assert out.evidence[0].values["outcome"] == "stopped"
    assert set(out.evidence[0].values["gains"]) == {"roll"}  # pitch never finished


def test_a_failure_names_the_axis_and_step(reg: Registry) -> None:
    rows = [(1, ROLL, D_UP, 0.054, 0.002)]
    out = AUTOTUNE.run(tune_flight(rows, [(0, INITIALISED), (3, FAILED)], AUTOTUNE_AXES=1.0), reg)
    assert codes(out)["autotune.failed"] == {"axis": "roll", "step": "rate D up"}


# --- what the tune ran into ----------------------------------------------------------------


def test_a_gain_limit_in_a_rate_step_matters_and_in_an_angle_step_does_not(reg: Registry) -> None:
    rate = AUTOTUNE.run(tune_flight(roll_tuned(), [(0, INITIALISED), (5, LIMIT), (13, SUCCESS)],
                                    AUTOTUNE_AXES=1.0), reg)  # fmt: skip
    assert codes(rate)["autotune.rate_limit"] == {"axis": "roll", "step": "rate D down"}
    angle = AUTOTUNE.run(tune_flight(roll_tuned(), [(0, INITIALISED), (12.5, LIMIT), (13, SUCCESS)],
                                     AUTOTUNE_AXES=1.0), reg)  # fmt: skip
    assert "autotune.rate_limit" not in codes(angle)
    assert angle.evidence[0].values["limits"] == ["roll: angle P up"]


def test_rate_d_left_at_its_floor_points_at_noise(reg: Registry) -> None:
    """D at AUTOTUNE_MIN_D when the D search ends; the 25% back-off afterwards may take
    the final D below it, so the floor is read at the end of the search."""
    out = AUTOTUNE.run(tune_flight(roll_tuned(d=0.0005), [(0, INITIALISED), (13, SUCCESS)],
                                   AUTOTUNE_AXES=1.0), reg)  # fmt: skip
    assert codes(out)["autotune.d_at_min"] == {"axes": "roll"}
    clear = AUTOTUNE.run(tune_flight(roll_tuned(d=0.0009), [(0, INITIALISED), (13, SUCCESS)],
                                     AUTOTUNE_AXES=1.0), reg)  # fmt: skip
    assert "autotune.d_at_min" not in codes(clear)


def test_yaw_rd_is_its_rate_filter_not_d(reg: Registry) -> None:
    rows = [(1, YAW, D_DOWN, 0.2, 0.0001), (5, YAW, P_UP, 0.25, 0.0001)]
    out = AUTOTUNE.run(tune_flight(rows, [(0, INITIALISED), (8, SUCCESS)], AUTOTUNE_AXES=4.0), reg)
    assert "autotune.d_at_min" not in codes(out)
    assert "d_after" not in out.evidence[0].values["gains"]["yaw"]


def test_an_esc_losing_sync_during_autotune_stops_further_tuning(reg: Registry) -> None:
    dip = (2, at(6), at(6.6))  # motor 3, inside the session
    extra = {"RCOU": rcou([1550] * 4), "ESC": esc({"RPM": [9000.0] * 4}, dip)}
    out = AUTOTUNE.run(tune_flight(roll_tuned(), [(0, INITIALISED), (13, SUCCESS)], extra=extra,
                                   answers={"motor_kv": 210}, AUTOTUNE_AXES=1.0), reg)  # fmt: skip
    found = {f.code: f for f in out.findings}
    assert found["autotune.desync"].severity == "out"
    assert found["autotune.desync"].params == {"motors": "3"}
    assert "autotune.low_kv_unchecked" not in found  # the ESC RPM was there to check


def test_a_desync_outside_autotune_is_left_to_the_motor_checks(reg: Registry) -> None:
    dip = (2, HOVER[1] - 2_000_000, HOVER[1] - 1_400_000)  # after the session ended
    extra = {"RCOU": rcou([1550] * 4), "ESC": esc({"RPM": [9000.0] * 4}, dip)}
    out = AUTOTUNE.run(tune_flight(roll_tuned(), [(0, INITIALISED), (13, SUCCESS)], extra=extra,
                                   AUTOTUNE_AXES=1.0), reg)  # fmt: skip
    assert "autotune.desync" not in codes(out)


def test_low_kv_motors_without_esc_rpm_are_flagged_unchecked(reg: Registry) -> None:
    out = AUTOTUNE.run(tune_flight(roll_tuned(), [(0, INITIALISED), (13, SUCCESS)],
                                   answers={"motor_kv": 210}, AUTOTUNE_AXES=1.0), reg)  # fmt: skip
    assert codes(out)["autotune.low_kv_unchecked"] == {"kv": 210}
    high = AUTOTUNE.run(tune_flight(roll_tuned(), [(0, INITIALISED), (13, SUCCESS)],
                                    answers={"motor_kv": 920}, AUTOTUNE_AXES=1.0), reg)  # fmt: skip
    assert "autotune.low_kv_unchecked" not in codes(high)


# --- what the review needs -----------------------------------------------------------------


def test_flights_without_autotune_planes_and_parameter_files_are_skipped(reg: Registry) -> None:
    plain = tune_flight([], [], modes=[(ARM, ALT_HOLD)])
    assert AUTOTUNE.run(plain, reg).skipped == "no AutoTune in this flight"
    plane = replace(plain, fingerprint={**plain.fingerprint, "vehicle": "plane"})
    assert AUTOTUNE.run(plane, reg).skipped == "the AutoTune review is for Copter"
    assert AUTOTUNE.run(replace(plain, cache=None), reg).skipped == (
        "a parameter file has no flight to review"
    )


def test_every_threshold_comes_from_the_rule_pack(reg: Registry) -> None:
    rules = reg.rules["autotune"]
    assert rules["calibrated"] is False and rules["sources"] and rules["low_kv"] == 500
    assert END - START < DISARM - ARM


# --- the simulator's roll AutoTune ---------------------------------------------------------


def _review(tmp_path: Path, name: str, reg: Registry) -> Result:
    keys = StoreKeys.generate()
    pack = reg.pack
    with open_store(tmp_path / f"{name}.db", keys, "e" * 32) as store:
        cache = ColumnCache(tmp_path / f"{name}-cache", keys)
        batch = scan(store, pack, bytes(32), [FIXTURES / f"{name}.bin"])
        [res] = commit(store, pack, cache, batch, propose(store, pack, batch), [Decision("f1")])
        return AUTOTUNE.run(FlightData.load(store, cache, pack, res.flights[0]), reg)


def test_the_simulators_finished_roll_tune_is_saved(reg: Registry, tmp_path: Path) -> None:
    out = _review(tmp_path, "copter-quad-autotune-saved", reg)
    assert codes(out) == {"autotune.saved": {"axes": "roll"}}
    [ev] = out.evidence
    roll = ev.values["gains"]["roll"]
    # From the soft gains (0.4 of the defaults), AutoTune about doubles rate P and lowers D.
    assert roll["p_before"] == pytest.approx(0.054) and 0.10 < roll["p_after"] < 0.14
    assert roll["d_after"] < roll["d_before"]
    assert ev.values["limits"] == ["roll: angle P up"]  # an ordinary limit, not a finding


def test_the_simulators_stopped_tune_keeps_nothing(reg: Registry, tmp_path: Path) -> None:
    out = _review(tmp_path, "copter-quad-autotune-aborted", reg)
    assert codes(out) == {"autotune.incomplete": {"remaining": "roll"}}
    [ev] = out.evidence
    assert ev.values["outcome"] == "stopped" and ev.values["saved"] is False
    assert ev.values["gains"] == {}
