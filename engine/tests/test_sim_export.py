"""The airframe's model for ArduPilot's simulator (sim/export.py): the simulator's own
arithmetic reproduced; ArduPilot's default quad and its Callisto heavy-lift recovered from
flights of their models; a heavier take-off scaled as the simulator scales a payload;
parameters that boot the simulator without the real hardware's wiring or anything about
where the aircraft flew; and nothing exported that the setup cannot say."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from flight_insights.ai.policy import Policy
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.run import run_analysis
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.params.files import parse_params
from flight_insights.platforms.records import (
    create_airframe,
    create_platform,
    platform_view,
    save_answers,
)
from flight_insights.platforms.schema import load_schema
from flight_insights.sim import export as sx
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store, open_store
from flight_insights.store.keys import StoreKeys

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
PACK = load_pack()
G = 9.80665
QUAD = {"wheelbase_mm": 350, "prop_diameter_in": 13.78}  # SITL's default: 0.35 m, 0.385 m2
CALLISTO = {"wheelbase_mm": 1325, "prop_diameter_in": 30}  # Callisto.json: 1.325 m, 1.82 m2
# ArduPilot's own models (libraries/SITL/SIM_Frame.cpp's defaults; Callisto.json)
DEFAULT_MODEL = {"mass": 3.0, "refSpd": 15.08, "refAngle": 45.0, "refAlt": 593.0,
                 "disc_area": 0.385, "mdrag_coef": 0.2}  # fmt: skip


def airframe(store: Store, name: str, answers: dict[str, Any]) -> str:
    schema = load_schema(PACK)
    platform = create_platform(store, schema, name, "copter")
    version = platform_view(store, schema, platform)["reference_build"]["version"]
    save_answers(store, schema, platform, version, answers)
    return create_airframe(store, platform, name)


def flown(store: Store, cache: ColumnCache, name: str, af: str, kg: float | None) -> str:
    batch = scan(store, PACK, bytes(32), [FIXTURES / f"{name}.bin"])
    [res] = commit(store, PACK, cache, batch, propose(store, PACK, batch),
                   [Decision(batch[0].id, airframe_id=af, takeoff_kg=kg)])  # fmt: skip
    flight = res.flights[0]
    run_analysis(store, cache, PACK, flight)
    return flight


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> tuple[Store, ColumnCache, dict[str, str]]:
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("sim")
    store = open_store(folder / "s.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    quad = airframe(store, "SITL quad", QUAD)
    callisto = airframe(store, "Callisto", CALLISTO)
    loaded = airframe(store, "Callisto loaded", CALLISTO)
    ids = {
        "quad": quad, "callisto": callisto, "loaded": loaded,
        "calm": flown(store, cache, "copter-quad-wind-calm", quad, 3.0),
        "hover": flown(store, cache, "copter-quad-hover", quad, 3.0),
        "light": flown(store, cache, "copter-heavylift-light", callisto, 32.5),
        "heavy": flown(store, cache, "copter-heavylift-loaded", loaded, 40.5),
    }  # fmt: skip
    return store, cache, ids


# --- the simulator's arithmetic --------------------------------------------------------------


def test_the_drag_area_is_the_simulators_own() -> None:
    """Frame::init prints its drag as EK3_DRAG_BCOEF = mass / areaCd: 16.713 for Callisto
    at 32.5 kg with the default reference test, 16.657 for a 3 kg quad flown at 10 m/s
    tilted 30.0134 degrees (local SITL boots of exported models)."""
    callisto = {"mass": 32.5, "refSpd": 15.08, "refAngle": 45.0, "refAlt": 0.0,
                "disc_area": 1.82415, "mdrag_coef": 0.2}  # fmt: skip
    assert 32.5 / sx.area_cd(callisto, G)[0] == pytest.approx(16.713, abs=0.001)
    quad = DEFAULT_MODEL | {"refSpd": 10.0, "refAngle": 30.0134, "refAlt": 0.0,
                            "disc_area": 0.384871, "mdrag_coef": 0.198728}  # fmt: skip
    assert 3.0 / sx.area_cd(quad, G)[0] == pytest.approx(16.657, abs=0.002)


def test_the_reference_angle_gives_the_drag_asked_for() -> None:
    for target in (0.0, 0.05, 0.1743, 1.9):
        angle = sx.ref_angle(DEFAULT_MODEL | {"refAlt": 0.0, "refSpd": 10.0}, target, G)
        got, mdrag = sx.area_cd(DEFAULT_MODEL | {"refAlt": 0.0, "refSpd": 10.0,
                                                 "refAngle": angle}, G)  # fmt: skip
        assert got == pytest.approx(target, abs=1e-6)
        assert mdrag == pytest.approx(0.2, rel=1e-6)  # the momentum drag kept as it is


def test_standard_air() -> None:
    assert sx.isa_density(0.0) == pytest.approx(1.225)
    for h in (0.0, 584.0, 2500.0):  # the standard atmosphere's density at its pressure
        pressure = 101325.0 * (1 - 0.0065 * h / 288.15) ** (G / (287.05287 * 0.0065))
        assert float(sx.isa_density_at(pressure)) == pytest.approx(sx.isa_density(h), rel=1e-9)


def test_an_ideal_rotors_power() -> None:
    """Momentum theory: the induced velocity is sqrt(T / 2 rho A), and the power T times it."""
    assert sx.ideal_power(1000.0, 1.25, 2.0) == pytest.approx(1000.0 * math.sqrt(200.0))


def test_coaxial_pairs_share_a_disc() -> None:
    frames = PACK.frames("4.7.0")
    assert sx.rotors(frames["4"]["types"]["1"]["motors"]) == 4  # X8
    assert sx.rotors(frames["4"]["types"]["20"]["motors"]) == 4  # co-rotating X8
    assert sx.rotors(frames["5"]["types"]["default"]["motors"]) == 3  # Y6
    assert sx.rotors(frames["2"]["types"]["1"]["motors"]) == 6  # hexa
    assert sx.rotors(frames["1"]["types"]["1"]["motors"]) == 4


# --- ArduPilot's own models, recovered --------------------------------------------------------


def test_the_default_quad_comes_back(fleet: tuple[Store, ColumnCache, dict[str, str]]) -> None:
    """Flown at 604 m in the simulator's standard air, its hover reads through the same
    law the simulator's motors follow; the drag comes from the flight's own legs."""
    store, cache, ids = fleet
    ex = sx.export(store, cache, PACK, ids["quad"], ids["calm"])
    m = ex.model
    assert ex.sitl == "+" and ex.frame == "QUAD/PLUS" and m["num_motors"] == 4
    # hover 0.39 at 593 m, full pack 12.6 V; here at sea level and MOT_BAT_VOLT_MAX 12.8 V
    expected = 0.39 * sx.isa_density(593.0) / sx.isa_density(0.0) * (12.6 / 12.8) ** 2
    assert m["hoverThrOut"] == pytest.approx(expected, rel=1e-3)
    assert m["refVoltage"] * m["refCurrent"] == pytest.approx(12.09 * 29.3, rel=5e-3)
    assert m["mdrag_coef"] == pytest.approx(0.2, rel=0.02)
    assert sx.area_cd(m, G)[0] == pytest.approx(sx.area_cd(DEFAULT_MODEL, G)[0], rel=0.05)
    assert (m["diagonal_size"], m["disc_area"]) == pytest.approx((0.35, 0.385), rel=1e-3)
    assert (m["refAlt"], m["refTempC"]) == (0.0, 15.0)


def test_a_hover_takes_its_airframes_drag(
    fleet: tuple[Store, ColumnCache, dict[str, str]],
) -> None:
    """A flight that only hovered measured no drag: it takes the drag of the airframe's
    flight with wind-measuring legs in the same payload band."""
    store, cache, ids = fleet
    hover = sx.export(store, cache, PACK, ids["quad"], ids["hover"])
    calm = sx.export(store, cache, PACK, ids["quad"], ids["calm"])
    assert hover.model["mdrag_coef"] == pytest.approx(calm.model["mdrag_coef"], rel=0.01)
    assert ids["calm"] in hover.sources["mdrag_coef"]


def test_callisto_comes_back(fleet: tuple[Store, ColumnCache, dict[str, str]]) -> None:
    store, cache, ids = fleet
    ex = sx.export(store, cache, PACK, ids["callisto"])
    m = ex.model
    assert ex.flight == ids["light"] and ex.sitl == "octa-quad" and ex.frame == "OCTAQUAD/X"
    assert m["hoverThrOut"] == pytest.approx(
        0.36 * sx.isa_density(26.0) / sx.isa_density(0.0), rel=1e-3
    )  # Callisto.json: 0.36 at 26 m
    assert m["mass"] == 32.5 and m["num_motors"] == 8 and m["maxVoltage"] == 50.4
    assert (m["propExpo"], m["pwmMin"], m["pwmMax"]) == (0.5, 1000.0, 1940.0)
    assert (m["spin_min"], m["spin_max"]) == pytest.approx((0.2, 0.975))
    assert m["battCapacityAh"] == 44.0 and m["disc_area"] == pytest.approx(1.82, rel=5e-3)
    assert "mdrag_coef" not in m and "no drag measured" in ex.sources["mdrag_coef"]
    assert 0 < m["refBatRes"] < 0.024  # the firmware's estimate, still settling


def test_a_heavier_take_off_is_scaled_as_the_simulator_carries_a_payload(
    fleet: tuple[Store, ColumnCache, dict[str, str]],
) -> None:
    """The loaded fixture is Callisto carrying 8 kg (40.5 kg): its hover output in
    proportion to the weight and its power in the 1.5 power (run_scenario.carry_payload)."""
    store, cache, ids = fleet
    light = sx.export(store, cache, PACK, ids["callisto"]).model
    asked = sx.export(store, cache, PACK, ids["callisto"], takeoff_kg=40.5).model
    flown_heavy = sx.export(store, cache, PACK, ids["loaded"]).model
    r = 40.5 / 32.5
    assert asked["mass"] == 40.5
    assert asked["hoverThrOut"] == pytest.approx(light["hoverThrOut"] * r, rel=1e-5)
    assert asked["hoverThrOut"] == pytest.approx(flown_heavy["hoverThrOut"], rel=0.01)
    power = asked["refVoltage"] * asked["refCurrent"]
    assert power == pytest.approx(light["refVoltage"] * light["refCurrent"] * r**1.5, rel=1e-5)
    assert power == pytest.approx(flown_heavy["refVoltage"] * flown_heavy["refCurrent"], rel=0.02)
    with pytest.raises(sx.ExportError, match="more than their full thrust"):
        sx.export(store, cache, PACK, ids["callisto"], takeoff_kg=95.0)


# --- the parameters and the files ------------------------------------------------------------


def test_the_parameters_boot_the_simulator_and_say_nothing_of_the_site(
    fleet: tuple[Store, ColumnCache, dict[str, str]],
) -> None:
    store, cache, ids = fleet
    ex = sx.export(store, cache, PACK, ids["callisto"])
    policy = Policy.load(PACK, ex.firmware)
    assert not [n for n in ex.params if policy.parameter_removed(n)]
    assert not [n for n in ex.params if n.startswith(("SIM_", "SERIAL", "COMPASS_", "GPS"))]
    assert not [n for n in ex.params if n.startswith("INS_") and n.endswith("_ID")]
    assert not [n for n in ex.params if "_LAT" in n or "_LNG" in n or "_LON" in n]
    assert [ex.params[f"SERVO{n}_FUNCTION"] for n in range(1, 9)] == list(range(33, 41))
    assert (ex.params["MOT_PWM_MIN"], ex.params["MOT_PWM_MAX"]) == (1000.0, 1940.0)
    assert ex.params["FRAME_CLASS"] == 4 and ex.params["FRAME_TYPE"] == 1
    assert ex.params["ATC_RAT_RLL_P"] > 0  # the tune comes along
    left = {n for names in ex.left_out.values() for n in names}
    assert {"INS_ACC_ID", "STAT_FLTTIME", "BATT_MONITOR", "SERIAL1_PROTOCOL"} <= left
    # what the autopilot keeps for itself, among it the field's elevation and pressure
    assert {"BARO_FIELD_ELV", "BARO1_GND_PRESS"} <= set(ex.left_out["learned: the simulator's own"])


def test_the_files(fleet: tuple[Store, ColumnCache, dict[str, str]]) -> None:
    store, cache, ids = fleet
    ex = sx.export(store, cache, PACK, ids["callisto"])
    files = {f["filename"]: f["text"] for f in sx.files(ex)}
    assert set(files) == {"callisto-32.5kg.json", "callisto-32.5kg.param",
                          "callisto-32.5kg-README.txt"}  # fmt: skip
    model = json.loads(files["callisto-32.5kg.json"])
    assert set(model) <= set(sx.FIELDS) and model == ex.model
    params = parse_params(files["callisto-32.5kg.param"])
    assert {e.name: e.value for e in params.entries} == pytest.approx(ex.params, rel=1e-6)
    readme = files["callisto-32.5kg-README.txt"]
    assert "--model octa-quad:callisto-32.5kg.json" in readme
    assert "hoverThrOut: 0.359" in readme and "standard sea-level air" in readme


def test_nothing_is_exported_that_the_setup_cannot_say(tmp_path: Path) -> None:
    keys = StoreKeys.generate()
    with open_store(tmp_path / "e.db", keys, "e" * 32) as store:
        cache = ColumnCache(tmp_path / "cache", keys)
        bare = airframe(store, "no setup", {})
        flown(store, cache, "copter-heavylift-light", bare, 32.5)
        with pytest.raises(sx.ExportError, match="wheelbase and the propeller diameter"):
            sx.export(store, cache, PACK, bare)
        unweighed = airframe(store, "unweighed", CALLISTO)
        flown(store, cache, "copter-heavylift-loaded", unweighed, None)
        with pytest.raises(sx.ExportError, match="weight is not known"):
            sx.export(store, cache, PACK, unweighed)
        never = airframe(store, "never flown", CALLISTO)
        with pytest.raises(sx.ExportError, match="none of the airframe's flights"):
            sx.export(store, cache, PACK, never)
        assert Registry.load(PACK).rules["sim"]["ref_alt_m"] == 0.0


def test_the_engine_method(tmp_path: Path) -> None:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, PACK)
    n = iter(range(1, 100))

    def rpc(method: str, **params: object) -> dict[str, Any]:
        return call(server, out, next(n), method, **params)

    assert "result" in rpc("engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys())
    pl = rpc("platform.create", name="Callisto", vehicle="copter")["result"]
    rpc("platform.save", id=pl["id"], build_version=pl["reference_build"]["version"],
        values=CALLISTO)  # fmt: skip
    af = rpc("airframe.create", platform_id=pl["id"], name="Callisto")["result"]
    batch = rpc("import.scan", paths=[str(FIXTURES / "copter-heavylift-light.bin")])["result"]
    [done] = rpc("import.commit", batch=batch["batch"],
                 decisions=[{"log": "f1", "airframe_id": af["id"], "takeoff_kg": 32.5}]
                 )["result"]  # fmt: skip
    rpc("analysis.run", flight_id=done["flights"][0])
    got = rpc("airframe.sim_export", airframe_id=af["id"], takeoff_kg=40.5)
    assert "result" in got, got
    assert got["result"]["sitl"] == "octa-quad" and got["result"]["model"]["mass"] == 40.5
    assert next(f["filename"] for f in got["result"]["files"]) == "callisto-40.5kg.json"
    assert rpc("airframe.sim_export", airframe_id=af["id"], takeoff_kg=-1)["error"]["code"]
    bad = rpc("airframe.sim_export", airframe_id="nope")["error"]
    assert "airframe" in bad["message"] and math.isfinite(bad["code"])
