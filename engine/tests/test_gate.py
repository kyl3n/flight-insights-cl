"""The Phase 0 gate: comparison results, the shareable report and the CLI."""

from __future__ import annotations

import json
import shutil
import struct
from collections.abc import Iterator
from pathlib import Path

import pytest
from df_builder import LogBuilder

from flight_insights import gate
from flight_insights.cli import main
from flight_insights.dataflash import DataflashLog
from flight_insights.dataflash.compat import Value

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def gps_log(n: int = 5) -> LogBuilder:
    log = LogBuilder()
    log.fmt(0x91, "GPS", "QBLLf", "TimeUS,I,Lat,Lng,Alt")
    log.units("GPS", "s#DUm", "F-GG0")
    for t in range(n):
        log.write("GPS", t, 0, -353632621 + t, 1491652374, 584.25)
    return log


def save(path: Path, log: LogBuilder) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(log.bytes())
    return path


def test_a_clean_log_matches(tmp_path: Path) -> None:
    result = gate.compare_log(save(tmp_path / "a.bin", gps_log()))
    assert result.status == "match"
    assert result.mismatches == 0
    assert result.types["GPS"] == 5
    assert result.messages == sum(result.types.values())


def test_engine_reading_more_than_pymavlink_is_a_mismatch(tmp_path: Path) -> None:
    """pymavlink stops at a type redefined with a new layout; the engine reads on."""
    log = LogBuilder()
    log.fmt(0x93, "LATE", "Qh", "TimeUS,X")
    log.write("LATE", 2, -7)
    log.fmt(0x93, "LATE", "QhB", "TimeUS,X,Y")
    log.write("LATE", 4, 8, 9)
    result = gate.compare_log(save(tmp_path / "b.bin", log))
    assert result.status == "mismatch"
    assert "more than pymavlink" in result.first_mismatches[-1].problem


def test_a_log_pymavlink_cannot_read_is_reported_as_such(tmp_path: Path) -> None:
    log = gps_log()
    log.raw(b"\xa3\x95\x80" + struct.pack("<BB4s16s64s", 0x94, 12, b"ODD", b"QX", b"T,O"))
    result = gate.compare_log(save(tmp_path / "c.bin", log))
    assert result.status == "reference_failed"
    assert result.error == "Exception"


def test_an_engine_failure_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_path: Path) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(gate, "read_dataflash", broken)
    result = gate.compare_log(save(tmp_path / "d.bin", gps_log()))
    assert (result.status, result.error) == ("engine_failed", "RuntimeError")


def test_differing_values_are_counted_and_located(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = gate.pymavlink_messages

    def off_by_one(log: DataflashLog) -> Iterator[tuple[str, dict[str, Value]]]:
        for typ, values in real(log):
            if typ == "GPS":
                alt = values["Alt"]
                assert isinstance(alt, float)
                values = values | {"Alt": alt + 1}
            yield typ, values

    monkeypatch.setattr(gate, "pymavlink_messages", off_by_one)
    result = gate.compare_log(save(tmp_path / "e.bin", gps_log(30)))
    assert result.status == "mismatch"
    assert result.mismatches == 30
    assert len(result.first_mismatches) == gate.MAX_LISTED
    first = result.first_mismatches[0]
    assert (first.type, first.column, first.problem) == ("GPS", "Alt", "value differs")


def test_the_report_holds_no_file_names_or_values(tmp_path: Path) -> None:
    name = "Somewhere Field 2026-10-20.bin"
    path = save(tmp_path / name, gps_log())
    data = gate.report([gate.compare_log(path)])
    text = json.dumps(data)
    assert "Somewhere" not in text and str(tmp_path) not in text
    assert "584.25" not in text and "-35.36" not in text and "149.16" not in text
    assert data["passed"] is True
    assert data["logs"][0]["id"] == gate.log_id(path)


def test_cli_passes_on_the_committed_fixtures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for log in sorted(FIXTURES.rglob("*.bin"))[:2]:
        shutil.copy(log, corpus / log.name.upper())  # .BIN, as flight controllers name them
    report = tmp_path / "report.json"
    assert main(["gate", "--corpus", str(corpus), "--report", str(report), "--jobs", "2"]) == 0
    assert "PASSED: 2 log(s): 2 match" in capsys.readouterr().out
    data = json.loads(report.read_text())
    assert data["summary"] == {"match": 2}


def test_cli_fails_on_a_mismatch_and_on_an_empty_corpus(tmp_path: Path) -> None:
    log = LogBuilder()
    log.fmt(0x93, "LATE", "Qh", "TimeUS,X")
    log.write("LATE", 2, -7)
    log.fmt(0x93, "LATE", "QhB", "TimeUS,X,Y")
    log.write("LATE", 4, 8, 9)
    save(tmp_path / "bad" / "x.bin", log)
    assert main(["gate", "--corpus", str(tmp_path / "bad"), "--jobs", "1"]) == 1
    (tmp_path / "empty").mkdir()
    assert main(["gate", "--corpus", str(tmp_path / "empty")]) == 2
