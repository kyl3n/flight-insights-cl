"""The track record on propeller changes (performance/propgrade.py): the made-up quad of
test_candidate flies a 22x6.6, then a 24x7.2 of the same family. Graded from the flights
before, the change is predicted as the true theory has it: the errors come back near
zero and inside the interval; a change of motor as well is not graded."""

from __future__ import annotations

from dataclasses import replace

from test_candidate import FAMILY, FLOWN, MOTOR, operating
from test_propfamily import RULES

from flight_insights.performance.motor import RPM_TO_RAD
from flight_insights.performance.propeller import INCH, Anchor, G
from flight_insights.performance.propfamily import Prop
from flight_insights.performance.propgrade import Flown, grade, record

BIG = Prop(FLOWN.family, 24.0, 7.2)
FAMILIES = {FLOWN.family: replace(FAMILY, tables=2)}


def flown(fid: str, order: float, prop: Prop, kg: float, motor: str = "V605") -> Flown:
    """A flight's hover as the true theory and motor give it, with the airframe's losses."""
    thrust = kg * G / 4
    op = operating(prop, thrust)
    assert op.cp is not None
    d = prop.diameter_in * INCH
    shaft = op.cp * op.density * (op.rpm / 60) ** 3 * d**5
    omega = op.rpm * RPM_TO_RAD
    power = float(MOTOR.power(shaft / omega, omega))
    return Flown(fid, order, Anchor(op.rpm, thrust, power, "escs", op.density, 60.0, 4, 0.0),
                 prop, motor)  # fmt: skip


def test_a_propeller_change_graded_from_the_flights_before() -> None:
    flights = [flown("a", 1, FLOWN, 18.0), flown("b", 2, FLOWN, 22.0), flown("c", 3, BIG, 20.0),
               flown("d", 4, BIG, 23.0)]  # fmt: skip
    [g] = grade(RULES, FAMILIES, MOTOR, flights)
    assert (g["before"], g["after"], g["flights"], g["graded"]) == (FLOWN.key, BIG.key, 2, True)
    # the true theory with the same losses on both: the change is predicted to within the
    # theory's change of speed with weight read from the nearest anchor
    assert g["speed_error"] < 0.002 and g["power_error"] < 0.005 and g["inside_95"] == 1.0
    r = record([g])
    assert r["changes"] == 1 and r["flights"] == 2 and r["power_error"] == g["power_error"]


def test_what_is_not_graded() -> None:
    # back to the first propeller: two changes, each graded against the flights before
    flights = [flown("a", 1, FLOWN, 20.0), flown("b", 2, BIG, 20.0), flown("c", 3, FLOWN, 20.0)]
    graded = grade(RULES, FAMILIES, MOTOR, flights)
    assert [(g["before"], g["after"], g["flights"]) for g in graded] == [
        (FLOWN.key, BIG.key, 1), (BIG.key, FLOWN.key, 1)]  # fmt: skip
    # a new motor as well: not graded, and why
    moved = [flown("a", 1, FLOWN, 20.0), flown("b", 2, BIG, 20.0, motor="MN605")]
    [g] = grade(RULES, FAMILIES, MOTOR, moved)
    assert not g["graded"] and g["why"] == "the motor changed too"
    # without a motor model only the speed is graded
    [g] = grade(RULES, FAMILIES, None, flights[:2])
    assert g["graded"] and "power_error" not in g and g["speed_error"] < 0.002
    assert record([]) == {"changes": 0, "flights": 0, "speed_error": None, "power_error": None}
