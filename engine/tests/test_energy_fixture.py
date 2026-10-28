"""The speed sweep on ArduCopter 4.7.0 SITL flights (tests/fixtures/sitl/4.7.0): the
card flown in GUIDED counts its four speeds, on the quad at 2 to 8 m/s in a gusting 1.5 m/s
wind along the legs (copter-quad-sweep) and on the heavy-lift X8 at 3 to 12 m/s in still air,
light and loaded (copter-heavylift-sweep, -sweep-loaded); the wind analyzer's 6 s legs are
too short to read power on; and the card is offered on the airframe's flights until one at
the same payload has flown it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from test_imports import airframe

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.energy import ENERGY
from flight_insights.dataflash import si
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.recommend import flight_plan
from flight_insights.recommend.plan import Recommendation
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
SWEEP = "followup.speed_sweep"


@pytest.fixture(scope="module")
def flown(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The hover and the sweep as one airframe's flights; the wind legs as another's."""
    pack = load_pack()
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("sweep")
    store = open_store(folder / "sweep.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    ids: dict[str, str] = {}
    frames: dict[str, str] = {}
    for name, frame in (("copter-quad-hover", "Q"), ("copter-quad-sweep", "Q"),
                        ("copter-quad-wind", "W"), ("copter-heavylift-sweep", "H"),
                        ("copter-heavylift-sweep-loaded", "H")):  # fmt: skip
        batch = scan(store, pack, bytes(32), [FIXTURES / f"{name}.bin"])
        af = frames.setdefault(frame, airframe(store, pack, frame))
        [res] = commit(store, pack, cache, batch, propose(store, pack, batch),
                       [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
        ids[name] = res.flights[0]
    return {"store": store, "cache": cache, "pack": pack, "ids": ids}


def data(flown: dict[str, Any], name: str) -> FlightData:
    return FlightData.load(flown["store"], flown["cache"], flown["pack"], flown["ids"][name])


def test_the_simulator_flying_the_card_counts_its_four_speeds(flown: dict[str, Any]) -> None:
    r = ENERGY.run(data(flown, "copter-quad-sweep"), Registry.load(flown["pack"]))
    m = {x.metric: x.value for x in r.metrics}
    assert m["energy.sweep_levels"] == 4
    assert m["energy.sweep_fastest_ms"] == pytest.approx(8.0, abs=0.3)
    [ev] = [x for x in r.evidence if x.id == "energy:sweep"]
    assert ev.values["slowest_ms"] == pytest.approx(2.0, abs=0.3)


@pytest.mark.parametrize("name", ["copter-heavylift-sweep", "copter-heavylift-sweep-loaded"])
def test_the_heavy_lift_flying_the_card_light_and_loaded_counts_its_four_speeds(
    flown: dict[str, Any], name: str
) -> None:
    """The 32.5 kg X8 at 3, 6, 9 and 12 m/s in still air, and again 8 kg heavier."""
    r = ENERGY.run(data(flown, name), Registry.load(flown["pack"]))
    m = {x.metric: x.value for x in r.metrics}
    assert m["energy.sweep_levels"] == 4
    assert m["energy.sweep_fastest_ms"] == pytest.approx(12.0, abs=0.3)
    [ev] = [x for x in r.evidence if x.id == "energy:sweep"]
    assert ev.values["slowest_ms"] == pytest.approx(3.0, abs=0.3)


def at_arming_and_disarming(f: FlightData, name: str, field: str) -> float:
    """What a logged running total gathered between arming and disarming."""
    bat = f.table(name)
    assert bat is not None
    t = np.asarray(bat.column("TimeUS").to_numpy(), np.int64)
    v = si.values(bat, field)
    return float(np.interp(f.disarm_us, t, v) - np.interp(f.arm_us, t, v))


def test_the_flights_energy_matches_the_firmwares_own_totals(flown: dict[str, Any]) -> None:
    """The packs' energy and charge, integrated from every row, agree with what ArduPilot
    counted itself (BAT.EnrgTot, BAT.CurrTot); the distance flown with the GPS ground speed
    integrated over the same time."""
    f = data(flown, "copter-quad-sweep")
    r = ENERGY.run(f, Registry.load(flown["pack"]))
    m = {x.metric: x.value for x in r.metrics}
    [ev] = [x for x in r.evidence if x.id == "energy:flight"]
    assert m["energy.used_wh"] == pytest.approx(at_arming_and_disarming(f, "BAT", "EnrgTot"),
                                                rel=0.005)  # fmt: skip
    assert ev.values["used_mah"] / 1000 == pytest.approx(
        at_arming_and_disarming(f, "BAT", "CurrTot"), rel=0.01
    )
    gps = f.table("GPS", ["TimeUS", "Spd"])
    assert gps is not None
    t = np.asarray(gps.column("TimeUS").to_numpy(), np.int64)
    inside = (t >= f.arm_us) & (t <= f.disarm_us)
    ground = float(np.trapezoid(si.values(gps, "Spd")[inside], t[inside] / 1e6))
    assert m["energy.distance_m"] == pytest.approx(ground, rel=0.01)
    # the legs at cruise cost less per km than the whole flight with its hover and climb
    assert m["energy.cruise_wh_per_km"] < m["energy.wh_per_km"]
    phases = ("low", "hover", "level", "climb", "descent", "manoeuvre", "untiled")
    assert sum(ev.values[f"{k}_wh"] for k in phases) == pytest.approx(ev.values["used_wh"],
                                                                      abs=0.5)  # fmt: skip


def test_short_legs_on_many_courses_are_no_sweep(flown: dict[str, Any]) -> None:
    r = ENERGY.run(data(flown, "copter-quad-wind"), Registry.load(flown["pack"]))
    assert {x.metric: x.value for x in r.metrics}["energy.sweep_levels"] == 0


def offered(flown: dict[str, Any], name: str) -> list[Recommendation]:
    plan = flight_plan(flown["store"], flown["cache"], flown["pack"], flown["ids"][name])
    return [r for r in plan.recommendations if r.lead.code == SWEEP]


def test_the_card_is_offered_until_a_sweep_is_flown(flown: dict[str, Any]) -> None:
    """The hover flew before the sweep, so its plan offers the card; the sweep's own plan
    does not, and neither does a flight on an airframe with no current logged."""
    [rec] = offered(flown, "copter-quad-hover")
    assert rec.kind == "performance" and rec.status == "next"
    assert offered(flown, "copter-quad-sweep") == []
    assert len(offered(flown, "copter-quad-wind")) == 1  # its legs were too short
