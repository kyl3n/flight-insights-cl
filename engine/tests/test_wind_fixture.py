"""The measured wind on ArduCopter 4.7.0 SITL flights (tests/fixtures/sitl/4.7.0/
copter-quad-wind*): legs at 3, 6 and 9 m/s on eight courses, in a gusting 5 m/s wind
from the west at every height and in still air. The simulator's frame model has rotor
momentum drag and bluff-body drag, and its wind is known exactly, so the analyzer must
find it, and the same drag in both flights; a hover alone measures nothing."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from test_imports import airframe

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.model import Result
from flight_insights.analyzers.wind import WIND
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
NAMES = ("copter-quad-wind", "copter-quad-wind-calm", "copter-quad-hover",
         "copter-quad-nognss-drill")  # fmt: skip


@pytest.fixture(scope="module")
def flown(tmp_path_factory: pytest.TempPathFactory) -> dict[str, FlightData]:
    pack = load_pack()
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("wind")
    out: dict[str, FlightData] = {}
    store = open_store(folder / "wind.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    for name in NAMES:
        batch = scan(store, pack, bytes(32), [FIXTURES / f"{name}.bin"])
        af = airframe(store, pack, name)
        [res] = commit(store, pack, cache, batch, propose(store, pack, batch),
                       [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
        out[name] = FlightData.load(store, cache, pack, res.flights[0])
    return out


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def metrics(r: Result) -> dict[str, tuple[float, dict[str, float]]]:
    return {m.metric: (m.value, dict(m.spread)) for m in r.metrics}


def test_the_set_wind_is_measured(flown: dict[str, FlightData], reg: Registry) -> None:
    r = WIND.run(flown["copter-quad-wind"], reg)
    assert r.skipped is None
    m = metrics(r)
    speed, spread = m["wind.speed_ms"]
    assert speed == pytest.approx(5.0, rel=0.03)
    assert spread["low"] - 0.05 <= 5.0 <= spread["high"] + 0.05  # SITL's 5 m/s, gusting
    [ev] = r.evidence
    assert abs(ev.values["from_deg"] - 270) <= 3 and ev.values["model"] == "quadratic"
    assert ev.values["windows"] >= 100 and abs(ev.values["height_m"] - 20) < 1
    assert m["wind.peak_ms"][0] >= speed
    assert "wind.ekf_speed_ms" not in m  # drag fusion is off in SITL's defaults


def test_still_air_measures_none_with_the_same_drag(
    flown: dict[str, FlightData], reg: Registry
) -> None:
    windy = metrics(WIND.run(flown["copter-quad-wind"], reg))
    calm = metrics(WIND.run(flown["copter-quad-wind-calm"], reg))
    assert calm["wind.speed_ms"][0] < 0.3
    for name in ("wind.drag_linear", "wind.drag_quadratic"):  # one airframe, one drag
        (a, sa), (b, sb) = windy[name], calm[name]
        assert sa["low"] <= sb["high"] and sb["low"] <= sa["high"], name
        assert a == pytest.approx(b, rel=0.1)
    assert windy["wind.trim_deg"][0] < 0.5 and calm["wind.trim_deg"][0] < 0.5


def test_a_hover_alone_measures_nothing(flown: dict[str, FlightData], reg: Registry) -> None:
    for name in ("copter-quad-hover", "copter-quad-nognss-drill"):
        r = WIND.run(flown[name], reg)
        assert r.skipped is not None and "drag from the wind" in r.skipped
        assert r.metrics == []


def test_the_estimators_own_wind_beside_it_with_drag_fusion(
    flown: dict[str, FlightData], reg: Registry
) -> None:
    """With drag fusion configured, the estimator's own wind is read from XKF2 over the
    same windows. This SITL flight booted without it, so EKF3 kept its wind at zero."""
    flight = flown["copter-quad-wind"]
    fused = dataclasses.replace(flight, params={**flight.params, "EK3_DRAG_BCOEF_X": 20.0})
    m = metrics(WIND.run(fused, reg))
    assert m["wind.ekf_speed_ms"][0] == pytest.approx(0.0, abs=0.05)
    assert m["wind.speed_ms"][0] == pytest.approx(5.0, rel=0.03)
