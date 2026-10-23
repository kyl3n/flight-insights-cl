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
