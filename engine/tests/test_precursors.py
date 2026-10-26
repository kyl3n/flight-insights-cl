"""Failsafe precursors: the warning each failsafe gave before it fired, and how close the
battery failsafe came on flights where it did not. On flights built in memory, and on
the committed SITL failsafe flights (tests/fixtures/sitl/4.7.0/copter-quad-fs-*)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import table
from test_failsafe_timing import at, bat
from test_failsafes import ARM, BATTERY, DISARM, T, build

from flight_insights.analyzers import ANALYZERS, FlightData, Registry
from flight_insights.analyzers.links import LINKS
from flight_insights.analyzers.model import Result
from flight_insights.analyzers.precursors import PRECURSORS, run_lengths, windowed_loss
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def run(reg: Registry, f: FlightData) -> Result:
    r = PRECURSORS.run(f, reg)
    assert r.skipped is None
    return r


def found(r: Result) -> dict[str, dict[str, Any]]:
    return {f.code: dict(f.params) for f in r.findings}


def metric(r: Result, name: str) -> tuple[float, str | None]:
    [m] = [m for m in r.metrics if m.metric == name]
    return m.value, m.zone


# --- the series' arithmetic ------------------------------------------------------------


def test_loss_over_a_sliding_window_needs_enough_packets() -> None:
    times = np.arange(0, 20_000_000, 1_000_000)
    got = np.full(20, 40)
    missed = np.where(times >= 10_000_000, 10, 0)
    loss = windowed_loss(times, got, missed, 5_000_000, 20)
    assert loss[9] == 0.0
    assert loss[10] == pytest.approx(100 * 10 / 210)  # one second of five lossy
    assert loss[14] == pytest.approx(20.0)  # the whole window lossy
    assert np.isnan(windowed_loss(times, got, missed, 5_000_000, 1000)).all()


def test_how_long_each_spell_has_lasted() -> None:
    times = np.arange(0, 1_000_000, 100_000)
    mask = np.array([0, 1, 1, 1, 0, 1, 0, 0, 1, 1], bool)
    assert run_lengths(times, mask).tolist() == [0, 0, 100_000, 200_000, 0, 0, 0, 0, 0, 100_000]


# --- the battery's near misses ---------------------------------------------------------


LOW = {**BATTERY, "BATT_LOW_VOLT": 11.0, "BATT_LOW_TIMER": 10}


def low_spell(samples: int) -> list[float]:
    """10 Hz from arming: a spell of `samples` under 11 V from 10 s, then recovered."""
    return [12.0] * 100 + [10.8] * samples + [12.0] * (800 - 100 - samples)


def test_a_low_voltage_spell_short_of_the_timer_is_a_near_miss(reg: Registry) -> None:
    r = run(reg, build("LOITER", [], params=LOW, extra={"BAT": bat(low_spell(51))}))
    assert metric(r, "failsafe.battery_low_run") == (0.5, "caution")
    assert found(r) == {"failsafe.battery_near_miss": {
        "left": "5 s of BATT_LOW_TIMER 10", "at_s": 10.0,
        "what": "BAT.Volt under BATT_LOW_VOLT 11 V for 5.0 s",
    }}  # fmt: skip


def test_no_near_miss_once_the_battery_failsafe_fired(reg: Registry) -> None:
    f = build("LOITER", [(at(150), "batt", 1)], [(at(150), "RTL", "batt")], LOW,
              extra={"BAT": bat(low_spell(51))})  # fmt: skip
    r = run(reg, f)
    assert metric(r, "failsafe.battery_low_run") == (0.5, "caution")
    assert "failsafe.battery_near_miss" not in found(r)


@pytest.mark.parametrize(
    ("used", "zone", "near"), [(3500, "caution", True), (3000, "target", False)]
)
def test_charge_used_against_the_failsafes_budget(
    reg: Registry, used: float, zone: str, near: bool
) -> None:
    params = {**LOW, "BATT_CAPACITY": 5000, "BATT_LOW_MAH": 1000}  # a budget of 4000 mAh
    charge = list(np.linspace(0, used, 800))
    r = run(reg, build("LOITER", [], params=params,
                       extra={"BAT": bat([12.0] * 800, charge)}))  # fmt: skip
    assert metric(r, "failsafe.battery_charge") == (round(used / 4000, 3), zone)
    if near:
        assert found(r)["failsafe.battery_near_miss"]["left"] == f"{4000 - used:g} mAh"
    else:
        assert "failsafe.battery_near_miss" not in found(r)


def test_no_battery_series_without_its_thresholds(reg: Registry) -> None:
    r = run(reg, build("LOITER", [], params=BATTERY, extra={"BAT": bat([12.0] * 800)}))
    assert r.metrics == [] and r.findings == []


# --- lead time ---------------------------------------------------------------------------


def mav(lossy_from: int, heard_until: int) -> pa.Table:
    """One link logged each second: 40 packets heard a second, 10 more missed a second from
    `lossy_from` (20%), and the GCS heard until `heard_until`."""
    times = list(range(ARM - 5_000_000, DISARM, 1_000_000))
    rxp, rxdp, got, missed = list[int](), list[int](), 0, 0
    for t in times:
        got += 40 if t < lossy_from else 30
        missed += 10 if t >= lossy_from else 0
        rxp.append(got % 65536)
        rxdp.append(missed % 65536)
    return table(times, {
        "chan": (pa.uint8(), [0] * len(times)),
        "rxp": (pa.uint16(), rxp),
        "rxdp": (pa.uint16(), rxdp),
        "mgs": (pa.uint32(), [min(t, heard_until) // 1000 for t in times]),
    })  # fmt: skip


def test_the_first_warning_before_a_gcs_failsafe(reg: Registry) -> None:
    f = build("LOITER", [(T, "gcs", 1)], [(T, "RTL", "gcs")], {"FS_GCS_ENABLE": 1},
              extra={"MAV": mav(T - 20_000_000, T - 5_050_000)})  # fmt: skip
    r = run(reg, f)
    assert found(r)["failsafe.lead_time"] == {
        "failsafe": "Ground station",
        "at_s": 30.0,
        "first": "packets missed on the way up",
        "lead_s": 20.0,
        "warned": 2,
        "watched": 2,
    }
    [ev] = [e for e in r.evidence if e.id.startswith("precursor:lead")]
    assert ev.values["uplink_loss_lead_s"] == 20.0
    assert ev.values["gcs_silence_lead_s"] == 3.0  # 2 s unheard: 0.4 of FS_GCS_TIMEOUT 5


def rci2(flagged_from: int | None) -> pa.Table:
    times = [ARM + 20_000 * k for k in range((DISARM - ARM) // 20_000)]
    flags = [5 if flagged_from is not None and t >= flagged_from else 1 for t in times]
    return table(times, {"Flags": (pa.uint8(), flags), "OMask": (pa.uint16(), [0] * len(times))})


@pytest.mark.parametrize(
    ("flagged_from", "code", "lead"),
    [(T - 600_000, "failsafe.lead_time", 0.6), (None, "failsafe.unwarned", None)],
)
def test_a_radio_failsafe_warned_by_the_receivers_flag_or_not_at_all(
    reg: Registry, flagged_from: int | None, code: str, lead: float | None
) -> None:
    f = build("LOITER", [(T, "radio", 1)], [(T, "RTL", "radio")], {"FS_THR_ENABLE": 1},
              extra={"RCI2": rci2(flagged_from)})  # fmt: skip
    params = found(run(reg, f))[code]
    assert params.get("lead_s") == lead and params["watched"] == 1


def test_registered_after_the_link_analyzer(reg: Registry) -> None:
    names = [a.name for a in ANALYZERS]
    assert names.index("precursors") == names.index(LINKS.name) + 1
    assert all(m in reg.metrics for m in PRECURSORS.metrics)


# --- the SITL failsafe flights -----------------------------------------------------------


@pytest.fixture(scope="module")
def sitl(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Result]:
    pack = load_pack()
    registry = Registry.load(pack)
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("precursors")
    out: dict[str, Result] = {}
    with open_store(folder / "p.db", keys, "e" * 32) as store:
        cache = ColumnCache(folder / "cache", keys)
        for name in ("copter-quad-fs-gcs", "copter-quad-fs-battery", "copter-quad-fs-radio"):
            batch = scan(store, pack, bytes(32), [FIXTURES / f"{name}.bin"])
            [res] = commit(store, pack, cache, batch, propose(store, pack, batch), [Decision("f1")])
            out[name] = run(registry, FlightData.load(store, cache, pack, res.flights[0]))
    return out


def leads(r: Result) -> list[tuple[str, Any, Any]]:
    return [(f.code, f.params.get("first"), f.params.get("lead_s")) for f in r.findings]


def test_sitl_the_lost_ground_station_warns_only_through_its_silence(
    sitl: dict[str, Result],
) -> None:
    assert leads(sitl["copter-quad-fs-gcs"]) == [
        ("failsafe.lead_time", "the ground station unheard", 3.0)
    ]


def test_sitl_the_rested_voltage_warns_before_both_battery_stages(
    sitl: dict[str, Result],
) -> None:
    r = sitl["copter-quad-fs-battery"]
    first = "BAT.VoltR within 3% of BATT_LOW_VOLT"
    assert leads(r) == [("failsafe.lead_time", first, 2.1), ("failsafe.lead_time", first, 7.4)]
    assert metric(r, "failsafe.battery_low_run")[1] == "out"  # it fired: no near miss


def test_sitl_a_receiver_that_just_stops_gives_no_warning(sitl: dict[str, Result]) -> None:
    assert leads(sitl["copter-quad-fs-radio"]) == [("failsafe.unwarned", None, None)]


def test_only_the_armed_time_counts(reg: Registry) -> None:
    times = [ARM - 5_000_000 + 100_000 * k for k in range(1000)]  # from 5 s before arming
    volts = [10.8 if t < ARM else 12.0 for t in times]  # low on the ground only
    low = table(times, {"Inst": (pa.uint8(), [0] * 1000), "Volt": (pa.float32(), volts)})
    r = run(reg, build("LOITER", [], params=LOW, extra={"BAT": low}))
    assert metric(r, "failsafe.battery_low_run") == (0.0, "target")
