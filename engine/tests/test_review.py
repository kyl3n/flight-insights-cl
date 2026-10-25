"""Check a parameter file: the review a flight gets, from the file alone. Planted faults
are found, values left at the firmware's default are not flagged, what only a log shows
is asked about rather than assumed, and a file for the wrong vehicle is refused."""

from __future__ import annotations

from itertools import count
from pathlib import Path
from typing import Any

import pytest

from flight_insights.knowledge import load_pack

FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
ids = count(100)  # each request its own id: the replies share one output


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, Any, str]:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, load_pack())
    data = tmp_path_factory.mktemp("review")
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(data), keys=keys())
    pl = call(server, out, 2, "platform.create", name="HL", vehicle="copter")["result"]
    af = call(server, out, 3, "airframe.create", platform_id=pl["id"], name="HL-1")["result"]
    log = FIXTURE / "copter-heavylift-loaded.bin"
    scanned = call(server, out, 4, "import.scan", paths=[str(log)])["result"]
    call(
        server, out, 5, "import.commit", batch=scanned["batch"],
        decisions=[{"log": "f1", "airframe_id": af["id"]}],
    )  # fmt: skip
    return server, out, af["id"]


def planted(tmp_path: Path, changes: dict[str, str], suffix: str = ".param") -> Path:
    """The fixture's own parameter file with some values changed, as a person might."""
    source = FIXTURE / f"copter-heavylift-loaded{suffix}"
    lines = source.read_text(encoding="utf-8").splitlines()
    sep = "," if suffix == ".param" else "\t"
    out: list[str] = []
    for line in lines:
        parts = line.split(sep)
        name_at = 0 if suffix == ".param" else 2
        if len(parts) > name_at + 1 and parts[name_at] in changes:
            parts[name_at + 1] = changes[parts[name_at]]
        out.append(sep.join(parts))
    target = tmp_path / f"proposed{suffix}"
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    return target


def review(engine: tuple[Any, Any, str], path: Path, airframe: bool = True) -> dict[str, Any]:
    from test_rpc import call

    server, out, af = engine
    airframe_id = af if airframe else None
    return call(server, out, next(ids), "review.file", path=str(path), airframe_id=airframe_id)


def codes(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["code"]: f for f in result["findings"]}


def test_the_flown_file_reads_as_its_flight_does(engine: tuple[Any, Any, str]) -> None:
    r = review(engine, FIXTURE / "copter-heavylift-loaded.param")["result"]
    assert (r["vehicle"], r["firmware"], r["firmware_from"]) == ("copter", "4.7.0", "airframe")
    assert r["defaults_from"] == "airframe" and r["parameters"] > 1000
    found = codes(r)
    # The firmware's defaults are not judged against the advisory ranges; SIM_ parameters
    # mark the simulator, as a SITL log's board does.
    assert "params.out_of_range" not in found
    assert set(found["params.gain_out_of_range"]["params"]["names"]) == {
        "ATC_RAT_PIT_P", "ATC_RAT_RLL_P",
    }  # fmt: skip
    # What only a log shows is asked about, never assumed.
    assert "option.question.esc_dshot_capable" in found
    assert r["plan"]["flight"] == "file" and r["plan"]["recommendations"]


def test_planted_faults_are_found_in_either_format(
    engine: tuple[Any, Any, str], tmp_path: Path
) -> None:
    for suffix in (".param", ".params"):
        path = planted(tmp_path, {"FS_THR_ENABLE": "0", "ATC_RAT_YAW_P": "9"}, suffix)
        found = codes(review(engine, path)["result"])
        assert found["params.rc_failsafe_off"]["severity"] == "safety"
        assert "ATC_RAT_YAW_P" in found["params.gain_out_of_range"]["params"]["names"]


def test_a_logging_cap_in_a_file_is_found_before_it_flies(
    engine: tuple[Any, Any, str], tmp_path: Path
) -> None:
    found = codes(review(engine, planted(tmp_path, {"LOG_FILE_RATEMAX": "25"}))["result"])
    f = found["logging.rate_limited"]
    assert f["analyzer"] == "logging" and f["params"]["cap"] == 25.0
    assert [(c["param"], c["proposed"]) for c in f["changes"]] == [("LOG_FILE_RATEMAX", 0.0)]


def test_without_an_airframe_the_newest_firmware_is_assumed(
    engine: tuple[Any, Any, str],
) -> None:
    r = review(engine, FIXTURE / "copter-heavylift-loaded.param", airframe=False)["result"]
    assert r["firmware_from"] == "newest" and r["firmware"] == load_pack().versions[-1]


def test_a_wrong_or_unreadable_file_is_refused(
    engine: tuple[Any, Any, str], tmp_path: Path
) -> None:
    plane = review(engine, FIXTURE / "plane-circuit.param")
    assert plane["error"]["code"] == -32003 and "plane" in plane["error"]["message"]
    junk = tmp_path / "junk.param"
    junk.write_text("not,a,parameter,file\n", encoding="utf-8")
    assert review(engine, junk)["error"]["code"] == -32003
    log = review(engine, FIXTURE / "copter-heavylift-loaded.bin")
    assert log["error"]["code"] == -32003
