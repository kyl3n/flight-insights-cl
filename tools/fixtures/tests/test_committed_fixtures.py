"""Checks on the fixtures committed under tests/fixtures/."""

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from pymavlink import DFReader, mavutil

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "tests" / "fixtures"
SCHEMA = json.loads((REPO / "tools" / "fixtures" / "label.schema.json").read_text())
LOGS = sorted(FIXTURES.rglob("*.bin")) + sorted(FIXTURES.rglob("*.tlog"))
LABELS = sorted(FIXTURES.rglob("*.yaml"))


def test_there_are_fixtures() -> None:
    assert LOGS and LABELS


@pytest.mark.parametrize("label", LABELS, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_labels_follow_the_schema(label: Path) -> None:
    jsonschema.validate(yaml.safe_load(label.read_text()), SCHEMA)


FLEET_LABEL = """
source: fleet
airframe: HL1
vehicle: copter
frame: octo X
archetype: heavy-lift with variable payload
firmware: 4.7.2
scenario: payload trial
purpose: payload-trial
description: Hover and slow survey with the spray tank half full.
payload: {mass_kg: 4.2, type: spray tank, mount: "under the centre, rigid"}
takeoff_mass_kg: 24.5
packs: [B-12, B-14]
conditions: {wind: light, temperature_c: 18}
handling: {rating: 4, note: slight wobble in yaw on descent}
changes_since_last: [INS_HNTCH_FREQ 70 to 85, new propellers]
faults:
  - {kind: oscillation, axis: yaw, severity: mild, description: wobble on descent}
expected: [vibration high on Z when loaded]
fixes: []
labelled_by: engineer
labelled_on: "2026-10-23"
"""


def test_a_heavy_lift_fleet_label_follows_the_schema() -> None:
    jsonschema.validate(yaml.safe_load(FLEET_LABEL), SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(yaml.safe_load(FLEET_LABEL.replace("oscillation", "wobble")), SCHEMA)


@pytest.mark.parametrize("log", LOGS, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_every_log_has_a_label(log: Path) -> None:
    assert log.with_suffix(".yaml").exists()


@pytest.mark.parametrize("log", LOGS, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_pymavlink_reads_every_message(log: Path) -> None:
    count = 0
    if log.suffix == ".bin":
        reader = DFReader.DFReader_binary(str(log), zero_time_base=False)
        while reader.recv_msg() is not None:
            count += 1
    else:
        conn = mavutil.mavlink_connection(str(log), dialect="ardupilotmega")
        while conn.recv_match() is not None:
            count += 1
    assert count > 1000
