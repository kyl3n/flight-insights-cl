"""A candidate propeller against the one flown (performance/candidate.py), on a made-up
heavy-lift quad: its propellers follow a known family's theory, its motors a known
model, and the anchor holds the arms' and frame's own losses. The flown propeller
compared with itself changes nothing; the most thrust matches a brute-force match of
motor and propeller; a bigger propeller hovers slower on less power; the checks fail
where they should."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import numpy as np
import pytest
from test_propfamily import RULES, TRUE

from flight_insights.performance.candidate import (
    Basis,
    CandidateError,
    Response,
    choose_anchor,
    compare,
    mass,
    most_thrust,
)
from flight_insights.performance.motor import RPM_TO_RAD, Motor
from flight_insights.performance.propeller import INCH, Anchor, G
from flight_insights.performance.propfamily import (
    Operating,
    Prop,
    coefficients,
    uncalibrated,
)
from flight_insights.performance.thermal import Thermal

FAM = "tmotor_p"
FAMILY = replace(uncalibrated(RULES, FAM), params=tuple(map(float, TRUE)), tables=1)
FLOWN = Prop(FAM, 22.0, 6.6)
MOTOR = Motor(kv=210.0, r0=0.06, i00=0.8)
RHO = 1.2
KT, KP = 0.95, 1.04  # the arms and frame: 5% less thrust, 4% more power
MASS = 20.0  # kg, a quad
DUTY = 0.98


def hover_rpm(prop: Prop, thrust: float) -> float:
    d, rpm = prop.diameter_in * INCH, 3000.0
    for _ in range(40):
        ct = KT * float(coefficients(TRUE, RULES, prop, rpm, RHO)[0][0])
        rpm = 60 * math.sqrt(thrust / (ct * RHO * d**4))
    return rpm


def operating(prop: Prop, thrust: float) -> Operating:
    rpm = hover_rpm(prop, thrust)
    ct, cp = coefficients(TRUE, RULES, prop, rpm, RHO)
    return Operating(rpm, KT * float(ct[0]), KP * float(cp[0]), RHO)


def basis(**kw: Any) -> Basis:
    base = Basis(flown=FLOWN, families={FAM: FAMILY}, motor=MOTOR,
                 at=operating(FLOWN, MASS * G / 4), motors=4, lowest_v=42.0, nominal_v=48.0,
                 arms=4, wheelbase_mm=1400.0,
                 thermals=(Thermal(1, 60.0, 0.02, 25.0, 0.3, 40.0),),
                 esc_limit_c=110.0, pack_limit_a=600.0, notch_low_hz=20.0)  # fmt: skip
    return replace(base, **kw)


def test_the_propeller_flown_against_itself() -> None:
    got = compare(basis(), RULES, FLOWN, None, [16.0, 20.0, 24.0], MASS, RHO, DUTY)
    for row in [*got["hover"], got["at"]]:
        assert row["share"] == pytest.approx(1.0) and row["share_from"] == "electric"
        assert row["flown"]["rpm"] == pytest.approx(row["candidate"]["rpm"])
    # at the anchor's weight the anchor itself comes back, its power through the motor
    at = got["at"]["flown"]
    op = basis().at
    assert op is not None
    assert at["rpm"] == pytest.approx(op.rpm, rel=1e-6) and at["notes"] == []
    d = FLOWN.diameter_in * INCH
    shaft = KP * float(coefficients(TRUE, RULES, FLOWN, op.rpm, RHO)[1][0]) * RHO * (
        op.rpm / 60) ** 3 * d**5  # fmt: skip
    assert at["shaft_w"] == pytest.approx(shaft, rel=1e-6)
    omega = op.rpm * RPM_TO_RAD
    assert at["electric_w"] == pytest.approx(float(MOTOR.power(shaft / omega, omega)), rel=1e-6)
    assert at["pack_a"] == pytest.approx(at["electric_w"] * 4 / 48.0)
    assert at["esc_c"] == pytest.approx(25.0 + 0.02 * (at["electric_w"] / 48.0) ** 2)
    assert at["motor_hz"] == pytest.approx(op.rpm / 60)
    assert at["tip_mach"] == pytest.approx(omega * d / 2 / RULES["sound_speed"])
    assert got["most"]["flown"] == got["most"]["candidate"]
    assert all(c["ok"] for c in got["checks"]), got["checks"]


def test_the_most_thrust_is_the_motor_and_propeller_matched() -> None:
    got = most_thrust(basis(), RULES, FLOWN, MOTOR, RHO, DUTY)
    assert got is not None
    # brute force: the fastest speed whose needed voltage is within the ESC's top duty
    d = FLOWN.diameter_in * INCH
    omega = np.linspace(100.0, MOTOR.k * 42.0, 20_001)
    rpm = omega / RPM_TO_RAD
    ct, cp = coefficients(TRUE, RULES, FLOWN, rpm, RHO)
    n = rpm / 60
    torque = KP * cp * RHO * n**2 * d**5 / (2 * math.pi)
    ok = np.flatnonzero(MOTOR.volts(torque, omega) <= DUTY * 42.0)[-1]
    assert got["rpm"] == pytest.approx(rpm[ok], rel=1e-3)
    assert got["thrust_n"] == pytest.approx(KT * ct[ok] * RHO * n[ok] ** 2 * d**4, rel=2e-3)
    assert got["pack_a"] == pytest.approx(got["electric_w"] * 4 / 42.0)
    assert most_thrust(basis(lowest_v=None), RULES, FLOWN, MOTOR, RHO, DUTY) is None


def test_a_bigger_propeller() -> None:
    big = Prop(FAM, 24.0, 7.2)
    got = compare(basis(), RULES, big, None, [20.0], MASS, RHO, DUTY)
    at = got["at"]
    # a bigger disc at the same thrust: slower, on less power, with the tips nearer
    assert at["candidate"]["rpm"] < at["flown"]["rpm"] and at["share"] < 1.0
    assert at["candidate"]["motor_hz"] < at["flown"]["motor_hz"]
    gap = got["clearance_mm"]
    assert gap["flown"] - gap["candidate"] == pytest.approx(2 * 25.4)
    assert gap["flown"] == pytest.approx(1400 * math.sin(math.pi / 4) - 22 * 25.4)
    # the candidate's coefficients are the true theory's times the aircraft's losses
    d = big.diameter_in * INCH
    rpm = hover_rpm(big, MASS * G / 4)
    assert at["candidate"]["rpm"] == pytest.approx(rpm, rel=1e-6)
    cp = KP * float(coefficients(TRUE, RULES, big, rpm, RHO)[1][0])
    assert at["candidate"]["shaft_w"] == pytest.approx(cp * RHO * (rpm / 60) ** 3 * d**5, rel=1e-6)
    assert set(got["notes"]) == {"candidate_not_measured", "current_not_measured"}


def test_the_checks_fail_where_they_should() -> None:
    def failed(**kw: Any) -> set[str]:
        prop = kw.pop("prop", FLOWN)
        got = compare(basis(**kw), RULES, prop, None, [], kw.pop("mass", MASS), RHO, DUTY)
        return {c["check"] for c in got["checks"] if not c["ok"]}

    assert failed() == set()
    assert failed(prop=Prop(FAM, 40.0, 12.0)) >= {"clearance"}  # tips past their neighbours
    assert failed(notch_low_hz=200.0) == {"notch"}  # the notch sits above the motors
    assert failed(pack_limit_a=20.0) == {"pack_current"}
    assert failed(esc_limit_c=30.0) == {"esc_temperature"}
    assert "thrust_to_weight" in failed(lowest_v=20.0)


def test_another_motor_and_no_motor() -> None:
    faster = Motor(kv=260.0, r0=0.06, i00=0.8)
    got = compare(basis(), RULES, FLOWN, faster, [], MASS, RHO, DUTY)
    assert got["other_motor"]
    assert got["most"]["candidate"]["thrust_n"] > got["most"]["flown"]["thrust_n"]
    # without a motor model only the shaft power is known: the drive's efficiency taken
    # as equal, and no most thrust
    bare = compare(basis(motor=None), RULES, Prop(FAM, 24.0, 7.2), None, [], MASS, RHO, DUTY)
    assert bare["at"]["share_from"] == "shaft" and "drive_efficiency_equal" in bare["notes"]
    assert bare["most"] is None and "electric_w" not in bare["at"]["candidate"]


def test_the_anchor_chosen() -> None:
    class Points:
        def __init__(self, fid: str, kg: float, a: Anchor | None) -> None:
            self.flight_id, self.mass_kg, self.anchor = fid, kg, a

    def anchor(kg: float, power_from: str = "escs", seconds: float = 30.0) -> Anchor:
        op = operating(FLOWN, kg * G / 4)
        d = FLOWN.diameter_in * INCH
        shaft = op.cp * RHO * (op.rpm / 60) ** 3 * d**5 if op.cp else 0.0
        omega = op.rpm * RPM_TO_RAD
        p = float(MOTOR.power(shaft / omega, omega))
        return Anchor(op.rpm, kg * G / 4, p, power_from, RHO, seconds, 4, 0.0)

    flights = [
        Points("light", 10.0, anchor(10.0)),
        Points("heavy", 25.0, anchor(25.0)),
        Points("packs", 20.0, anchor(20.0, "packs")),
        Points("none", 20.0, None),
    ]
    op, about = choose_anchor(flights, 22.0, MOTOR, 20.0 * G / 4) or (None, {})
    # nearest in thrust among those whose shaft power is known (the packs' holds avionics)
    assert about["flight_id"] == "heavy" and op is not None and op.cp is not None
    assert op.ct == pytest.approx(operating(FLOWN, 25.0 * G / 4).ct, rel=1e-4)
    assert choose_anchor([Points("none", 20.0, None)], 22.0, MOTOR, 100.0) is None
    # without a motor model no anchor has the shaft power: the nearest one serves
    _, about = choose_anchor(flights, 22.0, None, 20.0 * G / 4) or (None, {})
    assert about["flight_id"] == "packs"


MEASURED = Response(0.030, 0.010, "f1")  # 30 ms time constant, 10 ms dead time
BIG = Prop(FAM, 24.0, 7.2)


def damping(point: dict[str, Any]) -> float:
    """By hand: the motor's 1/(Kv^2 R) (no r2, no speed-dependent no-load current) and the
    propeller's 2Q/omega."""
    omega = point["rpm"] * RPM_TO_RAD
    return 1 / (MOTOR.k**2 * MOTOR.r0) + 2 * point["shaft_w"] / omega**2


def test_the_response_scales_with_inertia_over_damping() -> None:
    b = basis(response=MEASURED, prop_mass_g=60.0)
    same = compare(b, RULES, FLOWN, None, [MASS], MASS, RHO, DUTY)["response"]
    assert same["flown"] == same["candidate"]
    assert (
        same["candidate"]["response_ms"] == pytest.approx(40.0) and same["candidate"]["notes"] == []
    )
    got = compare(b, RULES, BIG, None, [MASS], MASS, RHO, DUTY)
    r = got["response"]["candidate"]
    # the measured time constant times the damping gives the whole inertia; the rotor is
    # what the propeller's share leaves, the same under the bigger propeller
    share = RULES["candidate"]["prop_inertia_share"]

    def prop_j(grams: float, d_in: float) -> float:
        return share * grams / 1000 * (d_in * INCH / 2) ** 2

    b_n, b_c = damping(got["at"]["flown"]), damping(got["at"]["candidate"])
    whole = 0.030 * b_n
    big_g = 60.0 * (24 / 22) ** 3  # the flown mass scaled with the diameter cubed
    want = (whole - prop_j(60.0, 22.0) + prop_j(big_g, 24.0)) / b_c
    assert r["time_constant_ms"] == pytest.approx(1000 * want, rel=1e-9)
    assert r["dead_time_ms"] == pytest.approx(10.0) and r["notes"] == ["mass_scaled"]
    assert r["response_ms"] > 40.0 and r["flight_id"] == "f1"
    # its own mass given: lighter blades answer sooner, and nothing is scaled
    light = compare(b, RULES, BIG, None, [MASS], MASS, RHO, DUTY, mass_g=50.0)
    assert light["response"]["candidate"]["response_ms"] < r["response_ms"]
    assert light["response"]["candidate"]["notes"] == []
    assert all(c["ok"] for c in got["checks"] if c["check"] == "response")
    # far heavier blades: slower than the rules allow
    heavy = compare(b, RULES, BIG, None, [MASS], MASS, RHO, DUTY, mass_g=400.0)
    [check] = [c for c in heavy["checks"] if c["check"] == "response"]
    assert not check["ok"] and check["limit"] == pytest.approx(1.5 * 40.0)


def test_the_response_without_a_motor_model_or_masses() -> None:
    # the inertia all the propeller's, its mass with the diameter cubed; the damping kept
    b = basis(response=MEASURED, motor=None)
    r = compare(b, RULES, BIG, None, [MASS], MASS, RHO, DUTY)["response"]["candidate"]
    assert r["time_constant_ms"] == pytest.approx(30.0 * (24 / 22) ** 5)
    assert set(r["notes"]) == {"rotor_inertia_left_out", "damping_equal"}
    # nothing measured on the aircraft: no response, and no check of it
    got = compare(basis(), RULES, BIG, None, [MASS], MASS, RHO, DUTY)
    assert got["response"] is None and all(c["check"] != "response" for c in got["checks"])


def test_a_candidates_mass() -> None:
    assert mass({"mass_g": 52}) == 52.0 and mass({}) is None and mass({"mass_g": None}) is None
    for bad in (0, -5, "heavy", True, 20000):
        with pytest.raises(CandidateError, match="mass_g"):
            mass({"mass_g": bad})
