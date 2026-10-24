"""The AI packet end to end: built from a real flight's analysis, checked, recorded; no
planted coordinate survives into it from any message type; the shared corpus is the
self-test's own cases; and the shell-only methods keep the tripwire from the UI."""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from planted import PLANT_LAT, PLANT_LON, leaks, plant

from flight_insights.ai import selftest
from flight_insights.ai.packet import number
from flight_insights.ai.schema import validate
from flight_insights.knowledge import load_pack

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
CORPUS = Path(__file__).resolve().parents[2] / "tests/scrubber"
ids = count(1000)


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, Any, Path]:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, load_pack())
    data = tmp_path_factory.mktemp("ai")
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(data), keys=keys())
    return server, out, data


def rpc(engine: tuple[Any, Any, Path], method: str, **params: Any) -> dict[str, Any]:
    from test_rpc import call

    server, out, _ = engine
    return call(server, out, next(ids), method, **params)


def flown(engine: tuple[Any, Any, Path], log: Path, vehicle: str = "copter") -> str:
    pl = rpc(engine, "platform.create", name=log.stem, vehicle=vehicle)["result"]
    af = rpc(engine, "airframe.create", platform_id=pl["id"], name=log.stem)["result"]
    scanned = rpc(engine, "import.scan", paths=[str(log)])["result"]
    decisions = [{"log": "f1", "airframe_id": af["id"]}]
    [done] = rpc(engine, "import.commit", batch=scanned["batch"], decisions=decisions)["result"]
    return str(done["flights"][0])


def test_the_corpus_files_are_the_self_tests_own_cases() -> None:
    for kind, items in selftest.cases().items():
        files = {p.stem for p in (CORPUS / "packets" / kind).glob("*.json")}
        assert files == set(items), f"run python -m flight_insights.ai.selftest {CORPUS}"
        for name, (why, packet) in items.items():
            saved = json.loads((CORPUS / "packets" / kind / f"{name}.json").read_text("utf-8"))
            assert saved == {"why": why, "packet": packet}, name


def test_the_self_test_passes(engine: tuple[Any, Any, Path]) -> None:
    result = rpc(engine, "ai.selftest")["result"]
    assert result == {"ok": True, "cases": 31, "failed": []}


def test_numbers_are_rounded_to_what_reasoning_needs() -> None:
    assert number(0.13500000536441803) == 0.135  # a float32 parameter's own digits
    assert number(655358.0) == 655358 and isinstance(number(655358.0), int)
    assert number(47.123456789) == 47.1235  # never more than four decimals in degrees
    assert number(0.000612345) == 0.000612345
    assert number(float("nan")) == 0


@pytest.mark.parametrize(
    ("log", "vehicle"),
    [("copter-heavylift-loaded.bin", "copter"), ("plane-circuit.bin", "plane"),
     ("heli-hover.bin", "copter"), ("quadplane-hover.bin", "plane")],
)  # fmt: skip
def test_each_fixture_makes_a_packet_that_may_leave(
    engine: tuple[Any, Any, Path], log: str, vehicle: str
) -> None:
    flight = flown(engine, FIXTURES / log, vehicle)
    reply = rpc(engine, "ai.prepare", flight_id=flight)["result"]
    assert reply["engine"] == {"ok": True, "reasons": []}, reply["engine"]
    packet = json.loads(reply["packet_json"])
    assert validate(packet) == [] and packet["findings"] and packet["parameters"]
    assert packet["aircraft"]["simulated"] and packet["aircraft"]["vehicle"] == vehicle
    # The flight's own positions came back for the shell's check, and none is in the packet.
    assert reply["tripwire"]
    for lat, lon in reply["tripwire"]:
        assert f"{lat:.4f}" not in reply["packet_json"] and f"{lon:.4f}" not in reply["packet_json"]
    names = set(packet["parameters"])
    assert not any(n.startswith(("SIM_", "FENCE_", "AHRS_ORIG")) for n in names)
    assert "COMPASS_DEC" not in names and "SYSID_THISMAV" not in names
    # Recorded as built, with the engine's verdict; the shell's verdict is still to come.
    kept = rpc(engine, "ai.record", packet_id=reply["packet_id"], shell_reasons=[], sent=True)
    assert kept["result"] == {
        "packet_id": reply["packet_id"],
        "shell_verdict": "pass",
        "sent": True,
    }
    again = rpc(engine, "ai.record", packet_id=reply["packet_id"], shell_reasons=[], sent=True)
    assert again["error"]["code"] == -32004  # one verdict per packet


def test_no_planted_coordinate_survives_from_any_message(
    engine: tuple[Any, Any, Path], tmp_path: Path
) -> None:
    log = tmp_path / "planted.bin"
    planted = plant(FIXTURES / "copter-heavylift-light.bin", log, load_pack())  # not imported above
    assert len(planted) >= 30 and planted["PARM"] >= 8
    flight = flown(engine, log)
    reply = rpc(engine, "ai.prepare", flight_id=flight)["result"]
    assert reply["engine"]["ok"], reply["engine"]["reasons"]  # the scrubber found no leak
    assert leaks(reply["packet_json"]) == []  # and neither does an independent search
    # The planted positions reached the encrypted cache: the tripwire holds them.
    assert any(
        abs(lat - PLANT_LAT) < 1e-4 and abs(lon - PLANT_LON) < 1e-4
        for lat, lon in reply["tripwire"]
    )


def test_a_refused_packet_goes_no_further(tmp_path: Path) -> None:
    from flight_insights.ai.record import record_outcome, record_packet
    from flight_insights.ai.scrubber import Violation
    from flight_insights.store import StoreError, StoreKeys, open_store

    with open_store(tmp_path / "f.db", StoreKeys.generate(), "e" * 32) as store:
        row = record_packet(store, None, "chat", "{}", [Violation("$.x", "shape")])
        assert row["engine_verdict"] == "refused" and json.loads(row["engine_reasons_json"])
        with pytest.raises(StoreError, match="cannot have been sent"):
            record_outcome(store, row["id"], [], sent=True)
        ok = record_packet(store, None, "chat", "{}", [])
        with pytest.raises(StoreError, match="cannot have been sent"):
            record_outcome(store, ok["id"], [{"path": "$", "rule": "shape"}], sent=True)
        done = record_outcome(store, ok["id"], [{"path": "$", "rule": "shape"}], sent=False)
        assert done["shell_verdict"] == "refused" and not done["sent"]


def test_the_methods_check_their_input(engine: tuple[Any, Any, Path]) -> None:
    assert rpc(engine, "ai.prepare", flight_id="")["error"]["code"] == -32602
    assert rpc(engine, "ai.prepare", flight_id="x", purpose="leak")["error"]["code"] == -32602
    assert rpc(engine, "ai.prepare", flight_id="no-such")["error"]["code"] == -32003
    assert rpc(engine, "ai.record", packet_id="no-such")["error"]["code"] == -32004
