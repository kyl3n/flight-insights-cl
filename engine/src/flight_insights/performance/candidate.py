"""A candidate propeller, and optionally another motor, for an airframe: what it would do
against the propeller flown (spec: Power and efficiency analysis, "Propeller and motor
choice", "What is predicted for a candidate"; rules in `knowledge/rules/props.json`
"candidate").

- **Both propellers are predicted the same way**, from the anchor the aircraft measured
  (`performance/propfamily.py`): the propeller flown at each weight (only the theory's
  change with speed), the candidate as a change from it. What the theory and the anchor
  get wrong for both alike cancels when the two are compared.
- **At each weight:** hover speed, shaft power and electrical power per motor (through
  the motor model: the one flown, or the candidate motor), current per motor and from the
  pack at its nominal voltage, the hottest ESC's settled temperature at that current
  (each ESC's measured thermal model), the tip Mach number and the motor frequency the
  harmonic notch must follow.
- **The most thrust:** the motor and propeller matched at full throttle (the ESC's top
  duty of the pack's lowest loaded voltage), against the weight (thrust-to-weight), with
  the current there per motor and from the pack against the pack's rating and
  `MOT_BAT_CURR_MAX`.
- **Clearance** between neighbouring propellers, the arms taken evenly round the
  wheelbase.
- **The share:** the candidate's electrical hover power over the flown propeller's at
  the same weight, which scales the airframe's model's propulsion power for its range,
  endurance and speeds (as a candidate bench table does). Without a motor model, the
  shaft powers' share, the drive's efficiency taken as equal (and said so).

Every figure is predicted (theory), never a maker's table; each carries the spread
`propfamily.predict` gives, a standard deviation of its logarithm.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, cast

from flight_insights.filters.harmonic import ESC, RPM, RPM2, SETS, NotchParams
from flight_insights.performance.motor import RPM_TO_RAD, Motor, from_datasheet
from flight_insights.performance.propeller import INCH, G, anchor_view
from flight_insights.performance.propfamily import (
    Family,
    Hover,
    Operating,
    Prop,
    anchored,
    predict,
)
from flight_insights.performance.thermal import Thermal


@dataclass(frozen=True)
class Basis:
    """What a candidate is predicted against."""

    flown: Prop
    families: Mapping[str, Family]
    motor: Motor | None  # the drive flown
    at: Operating | None  # the anchor
    motors: int
    lowest_v: float | None  # the pack's lowest loaded voltage
    nominal_v: float | None
    arms: int | None  # rotor discs: a coaxial pair shares an arm
    wheelbase_mm: float | None
    thermals: Sequence[Thermal] = ()
    esc_limit_c: float | None = None
    pack_limit_a: float | None = None  # the least of the pack's rating and MOT_BAT_CURR_MAX
    notch_low_hz: float | None = None  # the lowest frequency the harmonic notch sits at
    anchor: Mapping[str, Any] = field(default_factory=dict[str, Any])


class CandidateError(ValueError):
    pass


def _positive(values: Mapping[str, Any], key: str, most: float) -> float | None:
    v = values.get(key)
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, int | float) or not 0 < v <= most:
        raise CandidateError(f"{key} is a number above zero and at most {most:g}")
    return float(v)


def read(values: Mapping[str, Any], rules: Mapping[str, Any]) -> tuple[Prop, Motor | None]:
    """The candidate a person asks about: a propeller (family, diameter, pitch, blades)
    and, optionally, another motor from its datasheet (Kv, resistance, no-load current)."""
    family = values.get("family") or rules["default_family"]
    if family not in {f["id"] for f in rules["families"]}:
        raise CandidateError("family is one of the propeller families")
    d, p = _positive(values, "diameter_in", 100.0), _positive(values, "pitch_in", 100.0)
    if d is None or p is None:
        raise CandidateError("a candidate needs its diameter_in and pitch_in")
    blades = values.get("blades", 2)
    if isinstance(blades, bool) or not isinstance(blades, int) or not 2 <= blades <= 8:
        raise CandidateError("blades is a whole number from 2 to 8")
    motor = values.get("motor")
    if motor is None:
        return Prop(str(family), d, p, blades), None
    if not isinstance(motor, Mapping):
        raise CandidateError("motor is an object with kv, resistance_mohm and no_load_a")
    m = cast(Mapping[str, Any], motor)
    kv, ohm = _positive(m, "kv", 5000.0), _positive(m, "resistance_mohm", 10000.0)
    if kv is None or ohm is None:
        raise CandidateError("another motor needs its kv and resistance_mohm")
    return Prop(str(family), d, p, blades), from_datasheet(
        kv, ohm, _positive(m, "no_load_a", 500.0) or 0.0
    )


def notch_floor(params: Mapping[str, float]) -> float | None:
    """The lowest motor frequency the harmonic notch sets that follow the motors' speed
    (an RPM sensor or the ESCs) still track: below FREQ x FM_RAT a notch is held at it
    and fades out (libraries/Filter/HarmonicNotchFilter.cpp, 4.7.0), in the motors' own
    frequency (over REF). None when no set follows the motors' speed."""
    floors: list[float] = []
    for prefix in SETS:
        if f"{prefix}ENABLE" not in params:
            continue
        p = NotchParams.read(params, prefix)
        if p.enable and p.num_harmonics and p.mode in (RPM, ESC, RPM2) and p.ref > 0:
            floors.append(p.freq * p.fm_rat / p.ref)
    return max(floors) if floors else None


def choose_anchor(flights: Sequence[Any], diameter_in: float, motor: Motor | None,
                  thrust_n: float) -> tuple[Operating, dict[str, Any]] | None:  # fmt: skip
    """The flight anchor nearest the thrust asked for, its shaft power known (the ESCs'
    own power and a motor model) where any is; ties to the longest hover."""
    found: list[tuple[bool, float, float, Operating, dict[str, Any]]] = []
    for f in flights:
        a = getattr(f, "anchor", None)
        if a is None:
            continue
        v = anchor_view(a, diameter_in, motor)
        op = Operating(a.rpm, float(v["ct"]), v.get("cp"), a.density)
        about = {"flight_id": f.flight_id, "mass_kg": f.mass_kg, "power_from": a.power_from,
                 "seconds": a.seconds, "rpm": a.rpm, "thrust_n": a.thrust_n}  # fmt: skip
        found.append((op.cp is None, abs(math.log(a.thrust_n / thrust_n)), -a.seconds, op, about))
    if not found:
        return None
    best = min(found, key=lambda x: x[:3])
    return best[3], best[4]


def _point(basis: Basis, rules: Mapping[str, Any], prop: Prop, motor: Motor | None,
           thrust_n: float, density: float) -> dict[str, Any]:  # fmt: skip
    h: Hover = predict(rules, basis.families, prop, thrust_n, density, basis.flown, basis.at)
    omega = h.rpm * RPM_TO_RAD
    radius = prop.diameter_in * INCH / 2
    out: dict[str, Any] = {
        "rpm": h.rpm, "rpm_spread": h.rpm_spread, "shaft_w": h.shaft_w,
        "power_spread": h.power_spread, "ct": h.ct, "cp": h.cp, "motor_hz": h.rpm / 60,
        "tip_mach": omega * radius / float(rules["sound_speed"]), "notes": list(h.notes),
        "calibrated": h.calibrated, "anchored": h.anchored,
    }  # fmt: skip
    if h.shaft_w is not None and motor is not None:
        q = h.shaft_w / omega
        electric = float(motor.power(q, omega))
        amps = float(motor.current(q, omega))
        out |= {"electric_w": electric, "motor_a": amps, "efficiency": h.shaft_w / electric}
        if basis.nominal_v:
            pack = electric * basis.motors / basis.nominal_v
            out["pack_a"] = pack
            esc = electric / basis.nominal_v  # each ESC's input current
            if basis.thermals:
                out["esc_c"] = max(t.steady(esc) for t in basis.thermals)
    return out


def most_thrust(basis: Basis, rules: Mapping[str, Any], prop: Prop, motor: Motor,
                density: float, duty: float) -> dict[str, Any] | None:  # fmt: skip
    """The motor and propeller matched at full throttle at the pack's lowest loaded
    voltage: the speed where the voltage the motor needs reaches the ESC's top duty of
    it, and the thrust, current and power there."""
    if not basis.lowest_v:
        return None
    coeffs = anchored(rules, basis.families, prop, basis.flown, basis.at)
    d = prop.diameter_in * INCH

    def torque(omega: Any) -> Any:
        n = max(float(omega) / (2 * math.pi), 1e-6)
        return coeffs(n * 60, density)[1] * density * n**2 * d**5 / (2 * math.pi)

    omega = motor.top_speed(torque, basis.lowest_v, duty)
    n = omega / (2 * math.pi)
    ct, cp = coeffs(n * 60, density)
    q = cp * density * n**2 * d**5 / (2 * math.pi)
    amps = float(motor.current(q, omega))
    electric = float(motor.power(q, omega))
    out = {"thrust_n": ct * density * n**2 * d**4, "rpm": n * 60, "motor_a": amps,
           "electric_w": electric, "volts": duty * basis.lowest_v,
           "tip_mach": omega * d / 2 / float(rules["sound_speed"])}  # fmt: skip
    out["pack_a"] = electric * basis.motors / basis.lowest_v
    return out


def clearance(basis: Basis, prop: Prop) -> float | None:
    """The gap between neighbouring propellers' tips, mm, the arms evenly round the
    wheelbase; None without the wheelbase or the arms."""
    if not (basis.arms and basis.wheelbase_mm):
        return None
    return basis.wheelbase_mm * math.sin(math.pi / basis.arms) - prop.diameter_in * 25.4


def assess(basis: Basis, rules: Mapping[str, Any], prop: Prop, motor: Motor | None,
           mass_kg: float, density: float, duty: float) -> dict[str, Any]:  # fmt: skip
    """One propeller on the aircraft at a weight: its hover, its most thrust (with a motor
    model) and the checks against the rules' limits."""
    spec: Mapping[str, Any] = rules["candidate"]
    point = _point(basis, rules, prop, motor, mass_kg * G / basis.motors, density)
    most = most_thrust(basis, rules, prop, motor, density, duty) if motor else None
    if most is not None:
        most["thrust_to_weight"] = most["thrust_n"] * basis.motors / (mass_kg * G)
    checks: list[dict[str, Any]] = []

    def check(name: str, value: float, limit: float, ok: bool) -> None:
        checks.append({"check": name, "value": value, "limit": limit, "ok": ok})

    if most is not None:
        least = float(spec["least_thrust_to_weight"])
        ratio = most["thrust_to_weight"]
        check("thrust_to_weight", ratio, least, ratio >= least)
        if basis.pack_limit_a:
            check("pack_current", most["pack_a"], basis.pack_limit_a,
                  most["pack_a"] <= basis.pack_limit_a)  # fmt: skip
    tip = max(point["tip_mach"], most["tip_mach"] if most else 0.0)
    check("tip_mach", tip, float(spec["most_tip_mach"]), tip <= float(spec["most_tip_mach"]))
    gap = clearance(basis, prop)
    if gap is not None:
        least = float(spec["least_clearance_mm"])
        check("clearance", gap, least, gap >= least)
    esc = point.get("esc_c")
    if esc is not None and basis.esc_limit_c:
        limit = basis.esc_limit_c - float(spec["esc_margin_c"])
        check("esc_temperature", esc, limit, esc <= limit)
    if basis.notch_low_hz:
        hz = point["motor_hz"]
        check("notch", hz, basis.notch_low_hz, hz >= basis.notch_low_hz)
    return {"point": point, "most": most, "checks": checks}


def compare(basis: Basis, rules: Mapping[str, Any], candidate: Prop, motor: Motor | None,
            masses: Sequence[float], mass_kg: float, density: float,
            duty: float) -> dict[str, Any]:  # fmt: skip
    """The candidate against the propeller flown: at each weight and at the one asked
    for, the most thrust, clearance and the checks against the rules' limits."""
    drive_n = motor or basis.motor

    def share(now: Mapping[str, Any], then: Mapping[str, Any], kg: float) -> dict[str, Any]:
        key = "electric_w" if "electric_w" in now and "electric_w" in then else "shaft_w"
        ratio = (then[key] / now[key]) if now.get(key) and then.get(key) else None
        return {"mass_kg": kg, "flown": now, "candidate": then, "share": ratio,
                "share_from": "electric" if key == "electric_w" else "shaft",
                "share_spread": then.get("power_spread")}  # fmt: skip

    def both(kg: float) -> dict[str, Any]:
        thrust = kg * G / basis.motors
        return share(_point(basis, rules, basis.flown, basis.motor, thrust, density),
                     _point(basis, rules, candidate, drive_n, thrust, density), kg)  # fmt: skip

    flown = assess(basis, rules, basis.flown, basis.motor, mass_kg, density, duty)
    then = assess(basis, rules, candidate, drive_n, mass_kg, density, duty)
    out: dict[str, Any] = {
        "flown_prop": asdict(basis.flown), "candidate_prop": asdict(candidate),
        "other_motor": motor is not None,
        "at": share(flown["point"], then["point"], mass_kg),
        "hover": [both(kg) for kg in masses], "anchor": dict(basis.anchor) or None,
    }  # fmt: skip
    notes = set(out["at"]["candidate"]["notes"])
    if out["at"]["share_from"] == "shaft":
        notes.add("drive_efficiency_equal")
    out["notes"] = sorted(notes)
    out["calibrated"] = bool(out["at"]["candidate"]["calibrated"])
    out["most"] = ({"flown": flown["most"], "candidate": then["most"]}
                   if basis.motor is not None and drive_n is not None else None)  # fmt: skip
    gaps = clearance(basis, basis.flown), clearance(basis, candidate)
    if gaps[0] is not None:
        out["clearance_mm"] = {"flown": gaps[0], "candidate": gaps[1]}
    out["checks"] = then["checks"]
    return out
