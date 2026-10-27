"""The airframe's model for ArduPilot's simulator (spec: Simulator checks, "The model";
rules in `knowledge/rules/sim.json`): the fields of the simulator's own frame model from
one of the airframe's flights, its setup and its parameters, with the parameters to boot
it and where every value came from.

The simulator's multirotor (libraries/SITL/SIM_Frame.cpp and SIM_Motor.cpp at
Copter-4.7.0) is a set of momentum-theory rotors on the frame's arms:

- **Hover.** A motor at output c (between its spin limits) makes its share of the
  model's weight times ((1 - e) c + e c^2) / hoverThrOut, times the air's density over
  the reference altitude's, times the square of the battery voltage over maxVoltage. So
  hoverThrOut is the flight's steady hover read through that same law: each kept sample's
  motors through the thrust curve (e = MOT_THST_EXPO), times the square of its voltage
  over the full pack's, times the tilt, at standard air. A heavier take-off than the
  flight's scales it in proportion.
- **Power.** The simulator draws the hover power, refVoltage times refCurrent, per newton
  of thrust whatever the air: the flight's measured hover power, at the take-off weight
  as an ideal rotor's (in the 1.5 power of the thrust).
- **The air.** The hover is read at the density the flight flew in: the barometer's
  pressure at the weather record's air temperature, or at the barometer's own (the
  board's, a little above the air's, so the hover reads light) when the flight has no
  record; in the simulator, its own standard atmosphere at that pressure.
- **Drag** is the flight's own measured drag (analyzers/wind.py), or its airframe's from
  flights in the same payload band and on the same build when the flight only hovered:
  -k1 v - k2 v|v| per unit mass. The simulator's momentum drag is mdrag_coef times the
  square root of air density, rotor area and thrust, so k1 = mdrag_coef sqrt(rho A g / m);
  its bluff-body drag is areaCd times the dynamic pressure, so k2 = areaCd rho / 2m. It
  takes areaCd only through a reference test, flying at refSpd tilted refAngle, so the
  angle is solved for the measured areaCd.
- **The rest** comes from the setup (wheelbase, propellers, pack) and the parameters
  (thrust curve, output range); where nothing says, the simulator's own default, named
  as such. The moment of inertia is left to the simulator's estimate from mass and size.

Written at standard sea-level air, the file says nothing about where the aircraft flew.
The parameters leave out what the location policy removes, the real hardware's wiring
and calibration, and what the simulator measures for itself.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from flight_insights.ai.policy import Policy
from flight_insights.analyzers.flight import FlightData, FlightError
from flight_insights.analyzers.motor_out import motor_frame
from flight_insights.analyzers.motors_health import MOTOR_FUNCTIONS
from flight_insights.analyzers.payload import Hover, hover_thrust
from flight_insights.analyzers.registry import Registry
from flight_insights.knowledge import Pack
from flight_insights.params.dialect import Dialect
from flight_insights.params.files import write_mission_planner
from flight_insights.payload_bands import Bands
from flight_insights.store.cache import CacheError, ColumnCache
from flight_insights.store.db import Store
from flight_insights.trends import Flight, analysed_flights, metric_of
from flight_insights.weather import record as weather_record
from flight_insights.weather.measured import drag

# The fields of Frame::Model in the order SIM_Frame.cpp declares them.
FIELDS = (
    "mass", "diagonal_size", "refSpd", "refAngle", "refVoltage", "refCurrent", "refAlt",
    "refTempC", "refBatRes", "maxVoltage", "battCapacityAh", "hoverThrOut", "propExpo",
    "refRotRate", "pwmMin", "pwmMax", "spin_min", "spin_max", "slew_max", "disc_area",
    "mdrag_coef", "num_motors",
)  # fmt: skip
MOTOR_FUNCTION = {n: f for f, n in MOTOR_FUNCTIONS.items()}  # motor number -> SERVOx_FUNCTION
INCH = 0.0254
Array = np.ndarray[Any, Any]


class ExportError(ValueError):
    """The airframe cannot be modelled yet, and why."""


@dataclass(frozen=True)
class Export:
    airframe: str
    airframe_name: str
    flight: str
    firmware: str
    frame: str  # the motor frame, as "OCTAQUAD/X"
    sitl: str  # the simulator's frame model with the same motors
    model: dict[str, float]  # the simulator's frame model fields
    sources: dict[str, str]  # each field, and where its value came from
    params: dict[str, float]  # the simulator's boot parameters, as this firmware names them
    left_out: dict[str, list[str]] = field(default_factory=dict[str, list[str]])  # why -> names


def isa_density(alt_m: float) -> float:
    """The 1976 standard atmosphere's air density in the troposphere, kg/m3."""
    t = 288.15 - 0.0065 * alt_m
    return 1.225 * (t / 288.15) ** (9.80665 / (287.05287 * 0.0065) - 1.0)


def area_cd(model: Mapping[str, float], g: float) -> tuple[float, float]:
    """The bluff-body drag area and momentum drag coefficient the simulator works out from
    the model's reference test (Frame::init), at its reference altitude's air."""
    mass, angle = model["mass"], math.radians(model["refAngle"])
    rho = isa_density(model["refAlt"])
    drag_force = mass * g * math.tan(angle)
    cos_tilt = math.cos(angle)
    momentum = (cos_tilt * model["mdrag_coef"] * model["refSpd"] * cos_tilt
                * math.sqrt(mass * g / cos_tilt * rho * model["disc_area"]))  # fmt: skip
    if momentum > drag_force:
        return 0.0, model["mdrag_coef"] * drag_force / momentum
    return (drag_force - momentum) / (0.5 * rho * model["refSpd"] ** 2), model["mdrag_coef"]


def ref_angle(model: Mapping[str, float], target: float, g: float) -> float:
    """The reference tilt, degrees, at which the simulator works out `target` as areaCd
    (Frame::init): the drag the tilt holds grows with it while the momentum drag shrinks,
    so one angle gives it."""
    rho = isa_density(model["refAlt"])
    mass, speed = model["mass"], model["refSpd"]

    def excess(degrees: float) -> float:
        c = math.cos(math.radians(degrees))
        momentum = (
            c * model["mdrag_coef"] * speed * c * math.sqrt(mass * g / c * rho * model["disc_area"])
        )
        return mass * g * math.tan(math.radians(degrees)) - momentum - target * 0.5 * rho * speed**2

    low, high = 0.0, 89.9
    if excess(high) < 0:
        raise ExportError("the measured drag is too large for the simulator's reference test")
    for _ in range(100):
        mid = 0.5 * (low + high)
        low, high = (low, mid) if excess(mid) > 0 else (mid, high)
    return 0.5 * (low + high)


def isa_density_at(pressure_pa: Array) -> Array:
    """The standard atmosphere's density at a pressure (troposphere): the simulator's own
    air, which follows it exactly."""
    return 1.225 * (pressure_pa / 101325.0) ** (1.0 - 287.05287 * 0.0065 / 9.80665)


def air_density(store: Store, flight: FlightData, hover: Hover, gas_constant: float
                ) -> tuple[Array, str]:  # fmt: skip
    """The air's density through the hover, kg/m3, and where it came from: in the
    simulator, its standard atmosphere at the barometer's pressure; else the barometer's
    pressure at the weather record's air temperature, or at the barometer's own
    temperature (the board's, a little above the air's, so the density reads low)."""
    if flight.simulated:
        return isa_density_at(hover.pressure), "the simulator's standard atmosphere"
    temperature = weather_record.kept(store, [flight.id]).get(flight.id, {}).get("temperature_c")
    if isinstance(temperature, int | float) and math.isfinite(temperature):
        kelvin = float(temperature) + 273.15
        return hover.pressure / (gas_constant * kelvin), "the weather record's air temperature"
    kelvin_baro = hover.temperature + 273.15
    return (
        hover.pressure / (gas_constant * kelvin_baro),
        "the barometer's own temperature (the board's, a little above the air's)",
    )


def rotors(motors: Sequence[Mapping[str, Any]]) -> int:
    """Rotor discs: the motors on distinct arms (a coaxial pair shares its arm's disc, as
    the simulator's disc_area counts it)."""
    arms = {
        round(math.atan2(float(m["pitch"]), float(m["roll"])), 3)
        for m in motors
        if abs(float(m["roll"])) + abs(float(m["pitch"])) > 1e-9
    }
    return max(1, len(arms))


def ideal_power(thrust_n: float, rho: float, disc_area: float) -> float:
    """An ideal rotor's power to make this thrust, W (momentum theory): the least any real
    one draws."""
    return thrust_n**1.5 / math.sqrt(2.0 * rho * disc_area)


def _weight(mine: Flight | None, bands: Bands, answers: Mapping[str, Any]) -> tuple[float, str]:
    """The flight's take-off weight, kg: weighed at import, else from the airframe's
    weighed flights through the payload bands, else the setup's typical take-off."""
    if mine is not None and mine.takeoff_kg:
        return float(mine.takeoff_kg), "weighed at import"
    if mine is not None and (kg := bands.kg(mine.thrust)) is not None:
        return kg, "estimated from the airframe's weighed flights (payload bands)"
    typical = answers.get("typical_takeoff_kg")
    if isinstance(typical, int | float) and typical > 0:
        return float(typical), "the setup's typical take-off weight (the flight was not weighed)"
    raise ExportError(
        "the flight's weight is not known: weigh one of the airframe's flights at import, or "
        "enter its typical take-off weight in the setup"
    )


def _flight(store: Store, flights: Sequence[Flight], airframe_id: str, flight_id: str | None
            ) -> str:  # fmt: skip
    if flight_id is not None:
        row = store.get("flight", flight_id)
        if row is None or row["airframe_id"] != airframe_id:
            raise ExportError("that flight is not one of this airframe's")
        return flight_id
    hovered = [f for f in flights if f.thrust is not None]
    if not hovered:
        raise ExportError(
            "none of the airframe's flights hovered steadily long enough to measure its thrust"
        )
    return hovered[-1].id


def _full_volts(flight: FlightData, rules: Mapping[str, Any]) -> tuple[float, str]:
    volt_max = float(flight.params.get("MOT_BAT_VOLT_MAX", 0.0))
    if volt_max > 0:
        return volt_max, "MOT_BAT_VOLT_MAX"
    cells = flight.answers.get("cells") or flight.fingerprint.get("cells", {}).get("value")
    if not cells:
        raise ExportError("the pack's cell count is not known: enter it in the setup")
    chemistry = str(flight.answers.get("battery_chemistry") or rules["default_chemistry"])
    per_cell = float(rules["full_cell_v"].get(chemistry, rules["full_cell_v"]["LiPo"]))
    return float(cells) * per_cell, f"{int(cells)} cells of {chemistry} at {per_cell:g} V"


Params = dict[str, float]


def boot_params(flight: FlightData, pack: Pack, rules: Mapping[str, Any], motors: int,
                pwm: tuple[float, float]) -> tuple[Params, dict[str, list[str]]]:  # fmt: skip
    """The flight's parameters for the simulator, as its firmware names them, and what was
    left out, by why."""
    firmware = flight.firmware or pack.versions[-1]
    dialect = Dialect(pack, "copter", firmware)
    policy = Policy.load(pack, firmware)
    native = dialect.from_canonical(flight.params)
    drops = [(re.compile(d["pattern"]), str(d["why"])) for d in rules["drop"]]
    classes = set(rules["drop_classes"])
    kept: dict[str, float] = {}
    left: dict[str, list[str]] = {}
    for name, value in sorted(native.items()):
        why: str | None = None
        cls = dialect.param_class(name, native)
        if cls in classes:
            why = f"{cls}: the simulator's own"
        elif policy.parameter_removed(name):
            why = "removed by the location policy"
        else:
            why = next((w for p, w in drops if p.search(name)), None)
        if why is None:
            kept[name] = value
        else:
            left.setdefault(why, []).append(name)
    for n in range(1, motors + 1):
        kept[f"SERVO{n}_FUNCTION"] = float(MOTOR_FUNCTION[n])
    kept["MOT_PWM_MIN"], kept["MOT_PWM_MAX"] = pwm
    return kept, left


def export(
    store: Store,
    cache: ColumnCache,
    pack: Pack,
    airframe_id: str,
    flight_id: str | None = None,
    takeoff_kg: float | None = None,
) -> Export:
    """The airframe's simulator model from `flight_id` (else its latest flight with a
    steady hover), at `takeoff_kg` (else that flight's weight)."""
    airframe = store.get("airframe", airframe_id)
    if airframe is None:
        raise ExportError("no such airframe")
    reg = Registry.load(pack)
    rules: Mapping[str, Any] = reg.rules["sim"]
    g = float(rules["gravity"])
    flights, bands = analysed_flights(store, reg, airframe_id)
    chosen = _flight(store, flights, airframe_id, flight_id)
    mine = next((f for f in flights if f.id == chosen), None)
    try:
        flight = FlightData.load(store, cache, pack, chosen)
    except (FlightError, CacheError) as e:
        raise ExportError(str(e)) from None
    if flight.vehicle != "copter":
        raise ExportError("the simulator model is exported for Copter")
    found = motor_frame(flight, reg)
    if isinstance(found, str):
        raise ExportError(found)
    frame, frame_name = found
    sitl = frame.get("sitl")
    if not sitl:
        raise ExportError(f"ArduPilot's simulator has no model with the motors of a {frame_name}")
    wheelbase = flight.answers.get("wheelbase_mm")
    prop = flight.answers.get("prop_diameter_in")
    if not (isinstance(wheelbase, int | float) and wheelbase > 0) or not (
        isinstance(prop, int | float) and prop > 0
    ):
        raise ExportError(
            "enter the wheelbase and the propeller diameter in the setup: the simulator puts "
            "its motors on arms of that size and works out its rotors' drag from their area"
        )
    hover = hover_thrust(flight, reg)
    if isinstance(hover, str):
        raise ExportError(hover)
    motors: list[Mapping[str, Any]] = frame["motors"]
    if hover.command.shape[1] != len(motors):
        raise ExportError(f"{hover.command.shape[1]} motor outputs for a {len(motors)}-motor frame")

    flight_kg, kg_from = _weight(mine, bands, flight.answers)
    mass = float(takeoff_kg) if takeoff_kg else flight_kg
    if mass <= 0:
        raise ExportError("the take-off weight must be above zero")
    heavier = mass / flight_kg
    full_v, full_from = _full_volts(flight, rules)
    rho_ref = isa_density(float(rules["ref_alt_m"]))
    rho, air_from = air_density(store, flight, hover, float(reg.rules["payload"]["gas_constant"]))
    e = hover.expo
    curve = ((1.0 - e) * hover.command + e * hover.command**2).mean(axis=1)
    scale = (hover.voltage / full_v) ** 2 * hover.tilt * rho / rho_ref
    hover_out = float(np.median(curve * scale)) * heavier
    if hover_out >= 1.0:
        raise ExportError(f"at {mass:g} kg the motors would need more than their full thrust "
                          "to hover")  # fmt: skip
    flight_rho = float(np.median(rho))
    volts = float(np.median(hover.voltage))
    sources: dict[str, str] = {}
    model: dict[str, float] = {"mass": mass}
    sources["mass"] = f"{mass:g} kg as asked" if takeoff_kg else f"{mass:g} kg, {kg_from}"
    model["hoverThrOut"] = hover_out
    sources["hoverThrOut"] = (
        f"flight {chosen}'s steady hover ({hover.seconds:.0f} s) through the thrust curve, at "
        f"standard air (the flight's air from {air_from}) and full pack voltage"
        + (f", scaled from {flight_kg:.1f} kg" if abs(heavier - 1.0) > 1e-9 else "")
    )
    model["refVoltage"] = volts
    discs = rotors(motors)
    model["diagonal_size"] = float(wheelbase) / 1000.0
    sources["diagonal_size"] = "the setup's wheelbase"
    model["disc_area"] = discs * math.pi * (float(prop) * INCH / 2.0) ** 2
    sources["disc_area"] = f"{discs} rotor discs of the setup's {prop:g}-inch propellers"
    area = model["disc_area"]
    # the simulator draws the same power per newton whatever the air
    if hover.current is not None and float(np.median(hover.current)) > 0:
        power = float(np.median(hover.voltage * hover.current)) * heavier**1.5
        said = f"the hover's voltage and power ({power:.0f} W at this weight)"
    else:  # never none: the simulator sizes its rotors from the hover power
        power = ideal_power(mass * g, flight_rho, area)
        said = (f"the hover's voltage, and an ideal rotor's power ({power:.0f} W; a real one "
                "draws more): the log has no battery current")  # fmt: skip
    model["refCurrent"] = power / volts
    sources["refVoltage"] = sources["refCurrent"] = said
    model["refAlt"], model["refTempC"] = float(rules["ref_alt_m"]), float(rules["ref_temp_c"])
    sources["refAlt"] = sources["refTempC"] = (
        "standard sea-level air: the model holds nothing about where the aircraft flew"
    )
    model["maxVoltage"] = full_v
    sources["maxVoltage"] = full_from

    run = mine.run_id if mine is not None else None
    resistance = metric_of(store, run, "power.battery_resistance", None) if run else None
    if resistance is not None and resistance[0] > 0:
        model["refBatRes"] = resistance[0] / 1000.0
        sources["refBatRes"] = (
            "the firmware's own estimate the flight ended with (BAT.Res), as ArduPilot's "
            "models take it; it settles slowly, so a short flight reads low"
        )
    else:
        sources["refBatRes"] = "the simulator's default: the flight measured no resistance"
    capacity = flight.answers.get("capacity_mah") or flight.params.get("BATT_CAPACITY")
    model["battCapacityAh"] = float(capacity) / 1000.0 if capacity else 0.0
    sources["battCapacityAh"] = (
        ("the setup's pack" if flight.answers.get("capacity_mah") else "BATT_CAPACITY")
        if capacity else "unlimited: no pack capacity in the setup or BATT_CAPACITY"
    )  # fmt: skip

    p = flight.params
    low = float(p.get("MOT_PWM_MIN", 0)) or float(p.get("RC3_MIN", 1000))
    high = float(p.get("MOT_PWM_MAX", 0)) or float(p.get("RC3_MAX", 2000))
    model["propExpo"] = e
    model["pwmMin"], model["pwmMax"] = low, high
    model["spin_min"] = float(p.get("MOT_SPIN_MIN", 0.15))
    model["spin_max"] = float(p.get("MOT_SPIN_MAX", 0.95))
    for name, param in (("propExpo", "MOT_THST_EXPO"), ("pwmMin", "MOT_PWM_MIN"),
                        ("pwmMax", "MOT_PWM_MAX"), ("spin_min", "MOT_SPIN_MIN"),
                        ("spin_max", "MOT_SPIN_MAX")):  # fmt: skip
        sources[name] = param
    model["num_motors"] = float(len(motors))
    sources["num_motors"] = f"the {frame_name} frame"

    measured = drag(store, reg, chosen)
    if measured is not None:
        fit, came_from = measured
        model["mdrag_coef"] = fit.k1 * math.sqrt(flight_kg / (g * flight_rho * area))
        target = 2.0 * flight_kg * fit.k2 / flight_rho
        model["refSpd"] = float(rules["ref_speed_ms"])
        model["refAngle"] = ref_angle(model | {"refAngle": 0.0, "disc_area": area}, target, g)
        said = (
            "its own wind-measuring legs"
            if came_from == (chosen,)
            else f"the airframe's flights {', '.join(came_from)} in the same payload band"
        )
        sources["mdrag_coef"] = f"the momentum drag measured on {said}"
        sources["refSpd"] = sources["refAngle"] = (
            f"a reference test that gives the bluff-body drag measured on {said}"
        )
    else:
        for name in ("mdrag_coef", "refSpd", "refAngle"):
            sources[name] = ("the simulator's default: no drag measured (fly the "
                             "wind-measuring legs on this airframe)")  # fmt: skip
    for name in ("refRotRate", "slew_max"):
        sources[name] = "the simulator's default: not measured"
    sources["moment_inertia"] = "the simulator's estimate from mass and size"

    params, left = boot_params(flight, pack, rules, len(motors), (low, high))
    return Export(
        airframe=airframe_id, airframe_name=str(airframe["name"]), flight=chosen,
        firmware=flight.firmware or pack.versions[-1], frame=frame_name, sitl=str(sitl),
        model={k: _rounded(model[k]) for k in FIELDS if k in model}, sources=sources,
        params=params, left_out=left,
    )  # fmt: skip


def _rounded(value: float) -> float:
    return float(f"{value:.6g}")


def stem(ex: Export) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", ex.airframe_name.lower()).strip("-") or "airframe"
    return f"{name}-{ex.model['mass']:g}kg"


FLIGHT_SCRIPT = "fly_sitl.py"  # in the pack's sim/: flies the model in ArduPilot's simulator


def files(ex: Export, pack: Pack) -> list[dict[str, str]]:
    """The model, its boot parameters, the script that flies them and how, as files for a
    person to save."""
    base = stem(ex)
    model_text = json.dumps(ex.model, indent=2) + "\n"
    header = [
        f"Flight Insights: ArduPilot simulator parameters for {ex.airframe_name}, from flight "
        f"{ex.flight} ({ex.firmware}); load after the simulator's copter.parm",
        f"Model: {ex.sitl}:{base}.json",
    ]
    params_text = write_mission_planner(ex.params, header=header)
    lines = [
        f"{ex.airframe_name}: a model for ArduPilot's simulator (SITL), {ex.firmware}",
        "",
        f"Frame {ex.frame}, the simulator's '{ex.sitl}' model, at {ex.model['mass']:g} kg.",
        "",
        "Fly it with ArduPilot's own simulator, from a checkout of the same release with the",
        "copter simulator built (./waf configure --board sitl && ./waf copter):",
        f"  uv run {FLIGHT_SCRIPT} --ardupilot <checkout> --model {base}.json \\",
        f"    --frame {ex.sitl} --params {base}.param --flight motors --out {base}-motors.bin",
        "Flights: hover (the hover and power the model came from), legs (what the drag is",
        "measured from) and motors (each motor stopped in turn, against the motor-out",
        "margin). Import the log it keeps to compare it with the airframe's flights. Or",
        "start the simulator yourself:",
        f"  build/sitl/bin/arducopter --model {ex.sitl}:{base}.json \\",
        f"    --defaults Tools/autotest/default_params/copter.parm,{base}.param",
        "",
        "The model is written at standard sea-level air; the simulator flies it in the air",
        "of the home it is given (--home), so give it the altitude your flights take off at",
        "to compare with them. Nothing in these files says where the aircraft flew.",
        "",
        "Where each value came from:",
        *(f"  {k}: {ex.model[k]:g}  ({ex.sources[k]})" if k in ex.model
          else f"  {k}: ({ex.sources[k]})" for k in (*FIELDS, "moment_inertia")
          if k in ex.sources),
        "",
        "Parameters left out of the simulator's set:",
        *(f"  {why}: {len(names)}" for why, names in sorted(ex.left_out.items())),
        "",
        "The simulator is for behaviour, failsafes and margins; it does not reproduce motor",
        "noise or vibration, so it is never used for filter tuning.",
    ]  # fmt: skip
    return [
        {"filename": f"{base}.json", "text": model_text},
        {"filename": f"{base}.param", "text": params_text},
        {"filename": f"{base}-README.txt", "text": "\n".join(lines) + "\n"},
        {"filename": FLIGHT_SCRIPT,
         "text": (pack.root / "sim" / FLIGHT_SCRIPT).read_text(encoding="utf-8")},
    ]  # fmt: skip
