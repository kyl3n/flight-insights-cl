"""The scrubber's rules against every message each release can log (knowledge/logs, read
from ArduPilot's own source): a message that holds a position must be classified, a
field cleared in one must not reveal the place, and every pressure field must be either
rounded or known to be a difference. A release that adds such a message fails here until
the rules say what to do with it."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

from flight_insights.ai.policy import Policy
from flight_insights.knowledge import Pack, load_pack

PACK = load_pack()
VERSIONS = sorted(PACK.manifest["logs"])
RULES: dict[str, Any] = PACK.json("rules/scrubber.json")


def definitions(pack: Pack = PACK) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for version in VERSIONS:
        for name, message in pack.log_messages(version).items():
            yield version, name, message


NAMED = re.compile(RULES["position_names"])


def is_position(name: str, field: str, spec: dict[str, Any]) -> bool:
    """By its units or type, its name, or the rules' list of unitless positions."""
    return (
        spec["units"] in RULES["position_units"]
        or any(t in spec["type"] for t in RULES["position_types"])
        or bool(NAMED.match(field))
        or field in RULES["unitless_positions"].get(name, {})
    )


def holds_position(name: str, message: dict[str, Any]) -> bool:
    return any(is_position(name, field, f) for field, f in message["fields"].items())


def defined(message: str, field: str) -> bool:
    return any(n == message and field in m["fields"] for _, n, m in definitions())


def test_every_release_in_the_pack_has_its_log_messages() -> None:
    assert PACK.versions == VERSIONS
    assert all(len(PACK.log_messages(v)) > 300 for v in VERSIONS)


def test_every_message_that_holds_a_position_is_classified() -> None:
    missing = sorted(
        {
            f"{version} {name}"
            for version, name, message in definitions()
            if holds_position(name, message)
            and name not in RULES["position_messages"]
            and name not in RULES["blocked_messages"]
        }
    )
    assert missing == [], "classify these in knowledge/rules/scrubber.json"


def test_no_position_field_is_ever_readable() -> None:
    for version in VERSIONS:
        policy = Policy.load(PACK, version)
        for name, message in PACK.log_messages(version).items():
            for field, spec in message["fields"].items():
                if is_position(name, field, spec):
                    assert not policy.field(name, field).readable, (version, name, field)


def test_every_unitless_position_is_a_real_field_with_no_position_unit() -> None:
    for name, fields in RULES["unitless_positions"].items():
        assert name in RULES["position_messages"], name
        for field, holds in fields.items():
            assert holds and defined(name, field), f"{name}.{field}"
            for _, n, m in definitions():
                if n == name and field in m["fields"]:
                    assert m["fields"][field]["units"] not in RULES["position_units"]


def test_a_coordinate_with_no_position_unit_is_still_denied() -> None:
    policy = Policy.load(PACK, VERSIONS[-1])
    for name, field in (
        ("MAVC", "X"),
        ("MAVC", "Y"),
        ("MAVC", "Z"),
        ("VNKF", "Lat"),
        ("VNKF", "Lon"),
        ("RGPJ", "Lat"),
        ("RGPJ", "Lon"),
        ("RGPJ", "Alt"),
    ):
        assert not policy.field(name, field).readable, (name, field)
    # What a command did, and who sent it, stays readable.
    for field in ("Cmd", "Res", "SS", "SC"):
        assert policy.field("MAVC", field).readable, field


ALTITUDE = re.compile(r"(?i)alt|hgt|height|elev")


@pytest.mark.parametrize("name", sorted(RULES["position_messages"]))
def test_fields_cleared_in_a_position_message_do_not_give_the_place(name: str) -> None:
    held = RULES["position_messages"][name]
    assert held["reason"]
    for field in held["allow"]:
        assert defined(name, field), f"{name}.{field} is not a field any release defines"
        # Only altitudes relative to home or the origin, and altitude errors, may pass.
        if ALTITUDE.search(field):
            assert field.startswith("Rel") or field.endswith("E"), f"{name}.{field}"
        for _, n, m in definitions():
            if n == name and field in m["fields"]:
                spec = m["fields"][field]
                assert spec["units"] not in RULES["position_units"], f"{name}.{field}"
                assert spec["units"] not in RULES["location_units"], f"{name}.{field}"


def test_every_pressure_is_rounded_or_a_difference() -> None:
    loose = sorted(
        {
            f"{name}.{field}"
            for _, name, message in definitions()
            for field, spec in message["fields"].items()
            if spec["units"] == "Pa"
            and name not in RULES["position_messages"]
            and name not in RULES["blocked_messages"]
            and field not in RULES["coarsen"].get(name, {})
            and field not in RULES["pressure_ok"].get(name, [])
        }
    )
    assert loose == [], "round these, or list them as differences, in scrubber.json"


def test_every_rule_names_a_real_field() -> None:
    for group in ("location_fields", "coarsen"):
        for name, fields in RULES[group].items():
            for field in fields:
                assert defined(name, field), f"{group}: {name}.{field}"
    for name, fields in RULES["pressure_ok"].items():
        for field in fields:
            assert defined(name, field), f"pressure_ok: {name}.{field}"


def test_the_field_gate() -> None:
    policy = Policy.load(PACK, "4.7.2")
    assert not policy.field("GPS", "Lat").readable
    assert not policy.field("GPS", "Alt").readable  # absolute, beside a position
    assert not policy.field("GPS", "GWk").readable
    assert policy.field("GPS", "NSats").readable and policy.field("GPS", "HDop").readable
    assert policy.field("POS", "RelHomeAlt").readable and not policy.field("POS", "Alt").readable
    assert not policy.field("MSG", "Message").readable
    assert not policy.field("MAG", "MagX").readable  # field strength varies with place
    assert not policy.field("BARO", "AltAMSL").readable
    assert policy.field("BARO", "Alt").readable  # relative to where it was calibrated
    press = policy.field("BARO", "Press")
    assert press.verdict == "coarsen" and policy.value(press, 101_337.0) == 101_300.0
    assert policy.field("ESC", "RPM").readable and policy.field("PM", "Load").readable
    # Deny by default: unknown messages and fields, and anything the log marks itself.
    assert not policy.field("XYZW", "A").readable
    assert not policy.field("ESC", "NoSuchField").readable
    assert not policy.field("ESC", "RPM", is_position=True).readable
    assert not policy.field("ESC", "RPM", units="deglatitude").readable
    with pytest.raises(ValueError):
        policy.value(policy.field("GPS", "Lat"), 47.0)


def test_location_revealing_parameters_are_removed() -> None:
    policy = Policy.load(PACK, "4.7.2")
    removed = [
        "AHRS_ORIG_LAT", "AHRS_ORIGIN_LON", "COMPASS_DEC", "FENCE_RADIUS", "RALLY_LIMIT_KM",
        "SCR_USER3", "SIM_OPOS_LAT", "SYSID_THISMAV", "MAV_SYSID", "BRD_SERIAL_NUM",
        "BATT2_SERIAL_NUM", "ADSB_ICAO_ID", "BCN_LATITUDE", "BCN_ALT", "FOLL_SYSID",
    ]  # fmt: skip
    kept = ["ATC_RAT_PIT_P", "INS_HNTCH_FREQ", "ARM_FENCE", "MOT_PWM_TYPE", "BATT_CAPACITY"]
    out = policy.parameters({n: 1.0 for n in removed + kept})
    assert sorted(out) == sorted(kept)
    # Every parameter of every release named as a coordinate is removed.
    for version in VERSIONS:
        for vehicle in ("copter", "plane"):
            for name in PACK.metadata(version, vehicle):
                if re.search(r"(_LAT|_LNG|_LON|LATITUDE|LONGITUDE)$", name):
                    assert policy.parameter_removed(name), name
