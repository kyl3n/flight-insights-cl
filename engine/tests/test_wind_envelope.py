"""The wind envelope (analyzers/wind.py, weather/envelope.py): the strongest steady wind
an airframe can hold position in, from its measured drag and the position controller's
lean limit in Copter 4.7.0, per payload band.

The numbers were checked against ArduPilot 4.7.0 SITL's default quad holding position in
GUIDED while the set wind (SIM_WIND_T 1, no turbulence) rose in steps of 30 s. With the
drag of copter-quad-wind-calm, the lean it needed was predicted within 0.5 degrees from 6
to 13 m/s; at the default 30 degree limit it held 10 m/s at 29.0 degrees and drifted off
at 11, and at ATC_ANGLE_MAX 45 it held 15 m/s and drifted off at 15.5. With ATC_ANGLE_MAX
70 it drifted off at 20 m/s and 52.7 degrees: its throttle before the tilt boost rose
from 0.36 to 0.48 between 30 and 53 degrees, which the firmware's thrust limit reads, and
which a flight that leaned 25 degrees at most never measured."""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import numpy as np
import pytest
from test_imports import airframe

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.model import Result
from flight_insights.analyzers.run import run_analysis
from flight_insights.analyzers.wind import (
    WIND,
    Fit,
    configured_lean,
    lean_limit,
    lean_needed,
    strongest,
    thrust_lean,
)
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store, open_store
from flight_insights.store.keys import StoreKeys
from flight_insights.weather.envelope import envelope

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
PACK = load_pack()
REG = Registry.load(PACK)
RULES = REG.rules["wind"]["envelope"]
# copter-quad-wind-calm's drag (wind.drag_linear, wind.drag_quadratic, wind.trim_deg)
QUAD = Fit(0.2396, 0.03468, np.zeros(2), np.array([0.032, 0.0]), True, 0.0)
# SITL, holding position (wind m/s, tilt flown in degrees)
FLOWN = [(6, 14.9), (7, 18.4), (8, 21.9), (9, 25.5), (10, 29.0), (11, 32.4), (12, 35.7),
         (13, 38.8)]  # fmt: skip


def fit(k1: float, k2: float, bias: float = 0.0) -> Fit:
    return Fit(k1, k2, np.zeros(2), np.array([bias, 0.0]), k2 > 0, 0.0)


def test_the_configured_lean_limit_is_the_firmwares() -> None:
    assert configured_lean({}, RULES) == 30.0  # ATC_ANGLE_MAX's default
    assert configured_lean({"ATC_ANGLE_MAX": 45}, RULES) == 45.0
    assert configured_lean({"ATC_ANGLE_MAX": 5}, RULES) == 10.0  # constrained to 10..80
    assert configured_lean({"ATC_ANGLE_MAX": 85}, RULES) == 80.0
    assert configured_lean({"ATC_ANGLE_MAX": 45, "PSC_ANGLE_MAX": 25}, RULES) == 25.0
    assert configured_lean({"ATC_ANGLE_MAX": 45, "PSC_ANGLE_MAX": 0}, RULES) == 45.0


def test_the_thrust_limit_keeps_a_fifth_of_full_throttle() -> None:
    assert thrust_lean(0.4, RULES) == pytest.approx(60.0)  # acos(0.4 / 0.8)
    assert thrust_lean(0.72, RULES) == pytest.approx(math.degrees(math.acos(0.9)))
    assert thrust_lean(0.79, RULES) == 10.0  # never under 10 degrees
    assert thrust_lean(0.95, RULES) == 10.0


def test_the_lower_limit_holds() -> None:
    light = lean_limit({"ATC_ANGLE_MAX": 30}, (0.4, "hover"), RULES)
    assert (light.deg, light.by, light.thrust_deg) == (30.0, "lean", pytest.approx(60.0))
    heavy = lean_limit({"ATC_ANGLE_MAX": 30}, (0.72, "hover"), RULES)
    assert heavy.by == "thrust" and heavy.deg == pytest.approx(25.84, abs=0.01)
    unknown = lean_limit({}, None, RULES)
    assert (unknown.deg, unknown.by) == (30.0, "lean")
    assert unknown.thrust_deg is None and unknown.throttle is None
    assert lean_limit({"MOT_BAT_CURR_MAX": 60}, (0.4, "hover"), RULES).current_limited


@pytest.mark.parametrize("f", [fit(0.24, 0.035), fit(0.3, 0.0), fit(0.24, 0.035, 0.2)])
def test_the_strongest_wind_needs_exactly_the_limit(f: Fit) -> None:
    for limit in (20.0, 30.0, 45.0):
        assert lean_needed(f, strongest(f, limit)) == pytest.approx(limit, abs=1e-6)


def test_leaning_in_still_air_takes_from_the_envelope() -> None:
    assert strongest(fit(0.24, 0.035, 0.3), 30) < strongest(fit(0.24, 0.035), 30)
    assert strongest(fit(0.24, 0.035, 6.0), 30) == 0.0  # it can lean no more than it does


def test_the_lean_needed_against_the_simulator() -> None:
    for wind, tilt in FLOWN:
        assert lean_needed(QUAD, wind) == pytest.approx(tilt, abs=0.6), wind
    # it held 10 m/s at 29 degrees and drifted off at 11 with 30, 15 and 15.5 with 45
    assert 10.0 < strongest(QUAD, 30.0) < 11.0
    assert strongest(QUAD, 45.0) == pytest.approx(15.25, rel=0.04)


@pytest.fixture(scope="module")
def flights(tmp_path_factory: pytest.TempPathFactory) -> dict[str, FlightData]:
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("envelope")
    store = open_store(folder / "e.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    out: dict[str, FlightData] = {}
    for name in ("copter-quad-wind-calm", "copter-quad-wind-hover"):
        batch = scan(store, PACK, bytes(32), [FIXTURES / f"{name}.bin"])
        af = airframe(store, PACK, name)
        [res] = commit(store, PACK, cache, batch, propose(store, PACK, batch),
                       [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
        out[name] = FlightData.load(store, cache, PACK, res.flights[0])
    return out


def metrics(r: Result) -> dict[str, tuple[float, dict[str, float]]]:
    return {m.metric: (m.value, dict(m.spread)) for m in r.metrics}


def test_a_flight_that_measured_its_drag_gives_its_envelope(
    flights: dict[str, FlightData],
) -> None:
    r = WIND.run(flights["copter-quad-wind-calm"], REG)
    m = metrics(r)
    hold, spread = m["wind.envelope_ms"]
    assert 10.0 < hold < 11.0 and spread["low"] <= hold <= spread["high"]
    lean, lean_spread = m["wind.lean_at_reference_deg"]
    assert lean == pytest.approx(21.9, abs=0.6)  # flown at 8 m/s
    assert lean_spread["low"] <= lean <= lean_spread["high"]
    [ev] = [e for e in r.evidence if e.id == "wind:envelope"]
    v = ev.values
    assert (v["limit_deg"], v["limit_by"], v["configured_deg"]) == (30.0, "lean", 30.0)
    assert v["throttle"] == pytest.approx(0.36, abs=0.01) and v["throttle_from"] == "hover"
    assert v["thrust_deg"] == pytest.approx(63, abs=1)
    # its drag was measured up to about 8 m/s of airspeed: the envelope lies beyond
    assert v["extrapolated"] and v["airspeed_flown_ms"] < hold
    assert not v["thrust_unmeasured"] and not v["current_limited"]
    assert ev.messages == ("XKF1", "CTUN") and "ThI" in ev.fields


def test_the_lean_it_needs_is_the_lean_a_hover_in_that_wind_showed(
    flights: dict[str, FlightData],
) -> None:
    """copter-quad-wind-hover hovered in the set 5 m/s wind and leaned about 11.8
    degrees; the calm flight's drag says 12."""
    assert lean_needed(QUAD, 5.0) == pytest.approx(11.8, abs=0.5)


def test_thrust_at_more_lean_than_flown_widens_the_interval(
    flights: dict[str, FlightData],
) -> None:
    """At ATC_ANGLE_MAX 70 thrust limits the hold at 63 degrees by the hover throttle,
    but the flight leaned 25 at most: SITL drifted off at 20 m/s, so the interval comes
    down to the airspeed flown."""
    calm = flights["copter-quad-wind-calm"]
    steep = dataclasses.replace(calm, params={**calm.params, "ATC_ANGLE_MAX": 70.0})
    r = WIND.run(steep, REG)
    hold, spread = metrics(r)["wind.envelope_ms"]
    [ev] = [e for e in r.evidence if e.id == "wind:envelope"]
    assert ev.values["limit_by"] == "thrust" and ev.values["thrust_unmeasured"]
    assert ev.values["lean_flown_deg"] < 30
    assert spread["low"] == pytest.approx(ev.values["airspeed_flown_ms"], abs=0.05)
    assert spread["low"] < 20.0 < hold  # what SITL held lies inside


def test_the_airframe_envelope_per_payload_band(tmp_path: Path) -> None:
    keys = StoreKeys.generate()
    with open_store(tmp_path / "a.db", keys, "e" * 32) as store:
        cache = ColumnCache(tmp_path / "cache", keys)
        quad = airframe(store, PACK, "SITL quad")
        names = ("copter-quad-wind-calm", "copter-quad-wind", "copter-quad-wind-hover")
        ids = [_flown(store, cache, name, quad) for name in names]
        got = envelope(store, REG, quad, ids[2])
    assert got["reference_ms"] == 8.0
    [band] = got["bands"]  # the hover measured no drag of its own
    assert band["group"] == "band:0" and band["band"] == 0
    assert sorted(band["flights"]) == sorted(ids[:2])  # in the order they flew
    assert 10.0 < band["hold_ms"] < 11.0 and band["low_ms"] <= band["hold_ms"] <= band["high_ms"]
    assert band["limit_by"] == "lean" and band["limit_deg"] == 30.0
    assert band["lean_deg"] == pytest.approx(22, abs=1) and band["extrapolated"]
    assert not band["thrust_unmeasured"] and not band["current_limited"]
    assert got["group"] != "band:0"  # leaning 12 degrees, the hover has no band


def _flown(store: Store, cache: ColumnCache, name: str, af: str) -> str:
    batch = scan(store, PACK, bytes(32), [FIXTURES / f"{name}.bin"])
    [res] = commit(store, PACK, cache, batch, propose(store, PACK, batch),
                   [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
    run_analysis(store, cache, PACK, res.flights[0])
    return res.flights[0]
