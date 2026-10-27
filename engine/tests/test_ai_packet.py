"""The AI packet end to end: built from a real flight's analysis, checked, recorded; no
planted coordinate survives into it from any message type; the shared corpus is the
self-test's own cases; and the shell-only methods keep the tripwire from the UI."""

from __future__ import annotations

import json
import math
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from planted import PLANT_LAT, PLANT_LON, leaks, plant

from flight_insights.ai import selftest
from flight_insights.ai.packet import (
    WITHHELD,
    _analysis,  # pyright: ignore[reportPrivateUsage]
    number,
)
from flight_insights.ai.policy import Policy
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
    assert result == {"ok": True, "cases": 38, "failed": []}


def test_numbers_are_rounded_to_what_reasoning_needs() -> None:
    assert number(0.13500000536441803) == 0.135  # a float32 parameter's own digits
    assert number(655358.0) == 655358 and isinstance(number(655358.0), int)
    assert number(47.123456789) == 47.1235  # never more than four decimals in degrees
    assert number(0.000612345) == 0.000612345
    assert number(float("nan")) == 0


@pytest.mark.parametrize(
    ("log", "vehicle"),
    [("copter-heavylift-loaded.bin", "copter"), ("plane-circuit.bin", "plane"),
     ("heli-hover.bin", "copter"), ("quadplane-hover.bin", "plane"),
     ("copter-quad-tune-default.bin", "copter"), ("copter-quad-tune-low.bin", "copter"),
     ("copter-quad-autotune-saved.bin", "copter"),
     # flights with failsafes, whose error times in µs once stopped the packet
     ("copter-quad-fs-gcs.bin", "copter"), ("copter-quad-fs-off.bin", "copter")],
)  # fmt: skip
def test_each_fixture_makes_a_packet_that_may_leave(
    engine: tuple[Any, Any, Path], log: str, vehicle: str
) -> None:
    flight = flown(engine, FIXTURES / log, vehicle)
    reply = rpc(engine, "ai.prepare", flight_id=flight)["result"]
    assert reply["engine"] == {"ok": True, "reasons": []}, reply["engine"]
    packet = json.loads(reply["packet_json"])
    assert validate(packet) == [] and packet["findings"] and packet["parameters"]
    # Every metric the run measured is in the packet but the payload estimate: it is
    # scaled by the air's density from the barometer's pressure, which stays here.
    run = rpc(engine, "analysis.get", flight_id=flight)["result"]
    measured = [m for m in run["metrics"] if math.isfinite(m["value"])]
    held = [m for m in measured if m["metric"].startswith("payload.")]
    assert measured and len(packet["metrics"]) == len(measured) - len(held)
    assert not [m for m in packet["metrics"] if m["metric"].startswith("payload.")]
    assert "density" not in reply["packet_json"]
    assert packet["aircraft"]["simulated"] and packet["aircraft"]["vehicle"] == vehicle
    # Today's analyzers read nothing the location policy holds back.
    assert not [e["ref"] for e in packet["evidence"] if e["summary"] == WITHHELD]
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


def _ev(eid: str, kind: str, messages: tuple[str, ...], fields: tuple[str, ...]) -> dict[str, Any]:
    return {"id": eid, "kind": kind, "summary": f"{eid} read 512.25", "messages": list(messages),
            "fields": list(fields), "segment": None, "values": {"reading": 512.25}}  # fmt: skip


def test_evidence_that_read_a_location_field_is_withheld_with_its_metrics() -> None:
    # Analyzer evidence carries values from the fields it read. One the location policy
    # does not allow whole (a magnetic field, a GPS week, an absolute altitude, a rounded
    # pressure) keeps its reference but not its summary or values, and a metric measured
    # from it is left out.
    policy = Policy.load(load_pack(), "4.7.0")
    evidence = [
        _ev("vibe", "metric", ("VIBE",), ("VibeX",)),
        _ev("mag", "metric", ("MAG",), ("MagX",)),
        _ev("week", "series", ("GPS",), ("TimeUS", "GWk")),
        _ev("press", "metric", ("BARO",), ("Press",)),
        _ev("gps-whole", "fact", ("GPS",), ()),
        _ev("unknown", "metric", ("NOPE",), ("X",)),
        _ev("not-its-field", "metric", ("VIBE",), ("MagX",)),
        _ev("logging", "parameter", ("GYR",), ("LOG_BITMASK", "INS_RAW_LOG_OPT")),
        _ev("declination", "parameter", (), ("COMPASS_DEC",)),
    ]
    analysis: dict[str, Any] = {
        "evidence": evidence,
        "metrics": [{"metric": f"m.{e['id']}", "segment": "flight", "value": 1.5, "zone": None,
                     "evidence": e["id"]} for e in evidence],
        "findings": [{"key": "k", "code": "c", "severity": "info", "confidence": "high",
                      "params": {}, "evidence": [e["id"] for e in evidence]}],
    }  # fmt: skip
    keys: dict[str, str] = {}
    metrics, _, packed = _analysis(analysis, 0, policy, keys)
    held = {keys[e["ref"]] for e in packed if e["summary"] == WITHHELD}
    assert held == {"mag", "week", "press", "gps-whole", "unknown", "not-its-field", "declination"}
    for e in packed:
        if keys[e["ref"]] in held:
            assert e["values"] == {} and "512" not in json.dumps(e)
    by_id = {keys[e["ref"]]: e for e in packed}
    assert by_id["week"]["fields"] == ["TimeUS"]  # the allowed field is still named
    assert by_id["vibe"]["values"] == {"reading": 512.25}
    assert {m["metric"] for m in metrics} == {"m.vibe", "m.logging"}


def test_distances_from_positions_pass_alone_in_whole_metres() -> None:
    # The user's decision (2026-10-27): the AI may see how far GNSS and the estimator
    # parted, and how far the estimate drifted without GNSS, as one value per event, in
    # whole metres; never the summary, a finer value, a series or a direction.
    policy = Policy.load(load_pack(), "4.7.0")
    gap = _ev("gap", "fact", ("GPS", "POS"), ("Lat", "Lng")) | {"values": {
        "largest_track_gap_m": 61.84, "estimator_resets": 2, "largest_reset_m": 8.6,
        "east_m": 40.2, "bearing_deg": 212.0, "drift_m": [3.0, 5.0]}}  # fmt: skip
    drift = _ev("drift", "fact", ("GPS", "POS"), ("Lat", "Lng")) | {"values": {
        "drift_m": 14.3, "drift_m_per_min": 2.6, "minutes_to_tolerance": 3.846}}  # fmt: skip
    analysis: dict[str, Any] = {
        "evidence": [gap, drift], "metrics": [],
        "findings": [{"key": "k", "code": "gnss.spoofing", "severity": "warning",
                      "confidence": "high", "params": {}, "evidence": ["gap", "drift"]}],
    }  # fmt: skip
    keys: dict[str, str] = {}
    _, _, packed = _analysis(analysis, 0, policy, keys)
    by_id = {keys[e["ref"]]: e for e in packed}
    for e in packed:
        assert e["summary"] == WITHHELD and e["fields"] == []
    assert by_id["gap"]["values"] == {
        "largest_track_gap_m": 62, "estimator_resets": 2, "largest_reset_m": 9,
    }  # fmt: skip
    assert by_id["drift"]["values"] == {
        "drift_m": 14, "drift_m_per_min": 3, "minutes_to_tolerance": 3.8,
    }  # fmt: skip
    packet = selftest.cases()["clean"]["analysis"][1] | {"evidence": packed, "findings": []}
    assert validate(packet) == []
    # the schema holds the line on its own: a finer distance or a series does not fit
    finer = json.loads(json.dumps(packet))
    finer["evidence"][0]["values"]["largest_track_gap_m"] = 61.84
    series = json.loads(json.dumps(packet))
    series["evidence"][1]["values"]["drift_m"] = [3, 5, 8, 14]
    assert validate(finer) and validate(series)


@pytest.mark.parametrize(
    ("log", "key"),
    [("copter-quad-gnss-step.bin", "largest_track_gap_m"),
     ("copter-quad-nognss-flow.bin", "drift_m")],  # optical flow: under 3 m
)  # fmt: skip
def test_a_gnss_flight_sends_its_distances_rounded(
    engine: tuple[Any, Any, Path], log: str, key: str
) -> None:
    flight = flown(engine, FIXTURES / log)
    reply = rpc(engine, "ai.prepare", flight_id=flight)["result"]
    assert reply["engine"] == {"ok": True, "reasons": []}, reply["engine"]
    packet = json.loads(reply["packet_json"])
    held = [e for e in packet["evidence"] if e["summary"] == WITHHELD]
    sent = [e["values"][key] for e in held if key in e["values"]]
    assert sent and all(isinstance(v, int) and v >= 0 for v in sent), sent
    allowed = set(load_pack().json("rules/scrubber.json")["position_derived"])
    assert all(set(e["values"]) <= allowed for e in held)


def test_when_something_happened_reaches_the_packet_as_seconds_after_arming() -> None:
    # A log's own clock in microseconds is in the form of a coordinate scaled by 1e7, and
    # the location check refuses the whole packet for it: every flight with a logged error
    # or a board-supply dip would never get an AI read. Evidence says when in seconds.
    import dataclasses

    import numpy as np
    import pyarrow as pa
    from flight_builder import ARM, DISARM, hover_flight, table

    from flight_insights.ai.scrubber import (
        _number_rules,  # pyright: ignore[reportPrivateUsage]
        _numbers,  # pyright: ignore[reportPrivateUsage]
    )
    from flight_insights.analyzers import Registry
    from flight_insights.analyzers.power import POWER
    from flight_insights.analyzers.system import SYSTEM

    pack = load_pack()
    reg = Registry.load(pack)
    err = [ARM + 23_500_000, ARM + 31_250_000]  # a battery failsafe, then a GPS glitch
    vcc = np.linspace(ARM, DISARM, 101).astype(np.int64)
    tables = {
        "ERR": table(err, {"Subsys": (pa.uint8(), [6, 11]), "ECode": (pa.uint8(), [1, 2])}),
        "POWR": table(vcc.tolist(), {"Vcc": (pa.float32(), [5.1] * 70 + [4.1] + [5.1] * 30)},
                      {"Vcc": {b"unit": b"V", b"multiplier": b"1.0"}}),
    }  # fmt: skip
    f = hover_flight(tables)
    results = [SYSTEM.run(f, reg), POWER.run(f, reg)]
    findings = [x for r in results for x in r.findings]
    evidence = {e.id: e for x in findings for e in x.evidence}
    assert {"system.failsafe", "system.error", "power.board_voltage"} <= {x.code for x in findings}
    analysis: dict[str, Any] = {
        "evidence": [dataclasses.asdict(e) for e in evidence.values()],
        "metrics": [],
        "findings": [{"key": x.code, "code": x.code, "severity": x.severity,
                      "confidence": x.confidence, "params": dict(x.params),
                      "evidence": [e.id for e in x.evidence]} for x in findings],
    }  # fmt: skip
    _, _, packed = _analysis(analysis, ARM, Policy.load(pack, "4.7.0"), {})
    refused = [(where, x) for where, x, d in _numbers(packed) if _number_rules(x, d)]
    assert refused == []
    values = [e["values"] for e in packed]
    assert {"subsystem": 6, "code": 1, "at_s": [23.5]} in values
    assert any(v.get("lowest_at_s") == pytest.approx(27.3) for v in values)


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
