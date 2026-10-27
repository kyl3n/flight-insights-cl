"""The AI exchange, engine side: the static contract, the local tools (every result
checked like a packet, none leaking a planted coordinate), and what a reply comes to
once it is checked and its changes go through the recommendation engine."""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path
from typing import Any, cast

import pytest
from planted import leaks, plant

from flight_insights.ai import exchange, tools
from flight_insights.ai.schema import validate
from flight_insights.knowledge import load_pack

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
ids = count(5000)
CALLS: list[tuple[str, dict[str, Any]]] = [
    ("spectrum", {"axis": "x", "stage": "before"}),
    ("spectrum", {"axis": "z", "stage": "flown"}),
    ("filter_simulation", {"changes": [{"name": "INS_HNTCH_BW", "value": 30}]}),
    ("parameter_doc", {"name": "INS_HNTCH_MODE"}),
    ("metric_history", {"metric": "vibration.p95", "segment": "hover"}),
    ("segment_stats", {"message": "BAT", "field": "Volt", "start_s": None, "end_s": None}),
    ("segment_stats", {"message": "GPS", "field": "Lat", "start_s": 0, "end_s": 10}),
    ("parameter_doc", {"name": "AHRS_ORIG_LAT"}),
]


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, Any, Path]:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, load_pack())
    data = tmp_path_factory.mktemp("exchange")
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(data), keys=keys())
    return server, out, data


def rpc(engine: tuple[Any, Any, Path], method: str, **params: Any) -> dict[str, Any]:
    from test_rpc import call

    server, out, _ = engine
    return call(server, out, next(ids), method, **params)


def flown(engine: tuple[Any, Any, Path], log: Path) -> str:
    pl = rpc(engine, "platform.create", name=log.stem, vehicle="copter")["result"]
    af = rpc(engine, "airframe.create", platform_id=pl["id"], name=log.stem)["result"]
    scanned = rpc(engine, "import.scan", paths=[str(log)])["result"]
    decisions = [{"log": "f1", "airframe_id": af["id"]}]
    [done] = rpc(engine, "import.commit", batch=scanned["batch"], decisions=decisions)["result"]
    return str(done["flights"][0])


def sent_packet(engine: tuple[Any, Any, Path], flight: str) -> dict[str, Any]:
    prepared = rpc(engine, "ai.prepare", flight_id=flight)["result"]
    rpc(engine, "ai.record", packet_id=prepared["packet_id"], shell_reasons=[], sent=True)
    return prepared


def test_the_contract_is_what_the_api_takes() -> None:
    spec = exchange.spec(load_pack())
    assert (
        spec["exchange"] == "fi.ai.exchange/1" and "never compute from raw data" in spec["system"]
    )
    assert [t["name"] for t in spec["tools"]] == list(tools.TOOL_NAMES)
    assert len(spec["tools"]) <= 20  # the API's limit on strict tools

    def strict_objects(node: Any) -> None:
        if isinstance(node, dict):
            obj = cast(dict[str, Any], node)
            if obj.get("type") == "object":
                assert obj["additionalProperties"] is False
                assert set(obj["required"]) == set(obj["properties"])
            for v in obj.values():
                strict_objects(v)
        elif isinstance(node, list):
            for v in cast(list[Any], node):
                strict_objects(v)

    for t in spec["tools"]:
        assert t["strict"] is True
        strict_objects(t["input_schema"])
    strict_objects(spec["reply_schema"])
    text = json.dumps(spec["reply_schema"])
    for limit in (*exchange.LIMITS, "minimum", "maximum"):
        assert f'"{limit}"' not in text  # structured outputs take none of these
    assert set(spec["language"]) == {"en", "uk"}


def test_claude_is_told_each_limit_the_local_check_holds_it_to() -> None:
    # The first live run lost a whole analysis to one question's "why" past 300
    # characters: the API schema had dropped the limit, so Claude never saw it.
    reply = exchange.api_schema(exchange.REPLY)
    question = reply["properties"]["questions"]
    assert question["description"] == "At most 6 items."
    why = question["items"]["properties"]["why"]
    assert why == {"type": "string", "description": "At most 240 characters."}
    assert reply["properties"]["summary"]["description"] == "At most 1600 characters."
    refs = reply["properties"]["root_causes"]["items"]["properties"]["refs"]
    assert refs["description"] == "At most 16 items."
    chat = exchange.api_schema(exchange.CHAT_REPLY)
    assert chat["properties"]["answer"]["description"] == "At most 3200 characters."

    def said(local: Any, api: Any) -> None:
        # every limit the local check holds is said where the API schema has the field
        if isinstance(local, dict):
            node = cast(dict[str, Any], local)
            if "maxLength" in node or "maxItems" in node:
                assert "At most" in api["description"]
            for k, v in node.items():
                if k in api:
                    said(v, api[k])

    said(exchange.REPLY, reply)
    said(exchange.CHAT_REPLY, chat)


def test_every_tool_result_is_a_packet_that_may_leave(engine: tuple[Any, Any, Path]) -> None:
    flight = flown(engine, FIXTURES / "copter-heavylift-loaded.bin")
    parent = sent_packet(engine, flight)
    results: dict[str, Any] = {}
    for name, inputs in CALLS:
        got = rpc(engine, "ai.tool", packet_id=parent["packet_id"], name=name, input=inputs)
        reply = got["result"]
        assert reply["engine"] == {"ok": True, "reasons": []}, (name, reply["engine"])
        packet = json.loads(reply["packet_json"])
        assert validate(packet) == [] and packet["purpose"] == "tool"
        for lat, lon in reply["tripwire"]:
            assert (
                f"{lat:.4f}" not in reply["packet_json"]
                and f"{lon:.4f}" not in reply["packet_json"]
            )
        results[f"{name}:{json.dumps(inputs, sort_keys=True)}"] = packet["tool"]
    spectrum = results['spectrum:{"axis": "x", "stage": "before"}']
    assert spectrum["ok"] and 0 < len(spectrum["spectrum"]["hz"]) <= 256
    assert spectrum["spectrum"]["peaks"], "the hover spectrum has motor peaks"
    sim = results['filter_simulation:{"changes": [{"name": "INS_HNTCH_BW", "value": 30}]}']
    assert sim["ok"] and sim["filter_simulation"]["candidate"]["available"]
    doc = results['parameter_doc:{"name": "INS_HNTCH_MODE"}']["parameter_doc"]
    assert doc["values"] and doc["current"] is not None
    stats = results[
        'segment_stats:{"end_s": null, "field": "Volt", "message": "BAT", "start_s": null}'
    ]
    assert stats["ok"] and stats["segment_stats"]["p50"] > 0
    gps = results['segment_stats:{"end_s": 10, "field": "Lat", "message": "GPS", "start_s": 0}']
    assert not gps["ok"] and "segment_stats" not in gps  # a position is never read
    assert not results['parameter_doc:{"name": "AHRS_ORIG_LAT"}']["ok"]
    # Recorded under the packet they answer.
    kept = rpc(engine, "ai.sent", limit=50)["result"]
    tool_rows = [r for r in kept if r["parent_id"] == parent["packet_id"]]
    assert len(tool_rows) == len(CALLS)
    assert all("tripwire" not in json.dumps(r) for r in kept)


def test_no_planted_coordinate_leaves_through_a_tool(
    engine: tuple[Any, Any, Path], tmp_path: Path
) -> None:
    log = tmp_path / "planted.bin"
    plant(FIXTURES / "copter-heavylift-light.bin", log, load_pack())
    flight = flown(engine, log)
    parent = sent_packet(engine, flight)
    planted_reads = [
        # from well before arming, where the planted rows sit, to the end of the flight
        ("segment_stats", {"message": m, "field": f, "start_s": -100_000, "end_s": None})
        for m, f in (("POS", "Lat"), ("GPS", "Lng"), ("ORGN", "Lat"), ("GPS", "Alt"),
                     ("GPS", "GMS"), ("AHR2", "Lat"), ("BARO", "Alt"), ("MSG", "Message"),
                     ("MAVC", "X"), ("MAVC", "Y"), ("MAVC", "Z"), ("VNKF", "Lat"),
                     ("VNKF", "Lon"), ("RGPJ", "Lat"), ("RGPJ", "Alt"))
    ]  # fmt: skip
    for name, inputs in CALLS + planted_reads:
        reply = rpc(engine, "ai.tool", packet_id=parent["packet_id"], name=name, input=inputs)
        assert reply["result"]["engine"]["ok"], (name, inputs)
        assert leaks(reply["result"]["packet_json"]) == [], (name, inputs)
    # The policy refuses each planted position before it is read: the scrubber's own
    # check, which would withhold the result, is the second line, not the first.
    for _, inputs in planted_reads:
        if (inputs["message"], inputs["field"]) == ("BARO", "Alt"):
            continue  # height above the take-off point: allowed, and checked above
        reply = rpc(engine, "ai.tool", packet_id=parent["packet_id"], name="segment_stats",
                    input=inputs)  # fmt: skip
        tool = json.loads(reply["result"]["packet_json"])["tool"]
        assert not tool["ok"], inputs
        assert tool["error"] in (
            "that field is not available: it can reveal where the aircraft flew",
            "this flight's log has no such field",
        ), inputs


def _reply(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "summary": "Motor noise near 80 Hz passes the notch; widen it.",
        "root_causes": [{"title": "Notch too narrow", "explanation": "See the spectrum.",
                         "refs": ["F1"]}],
        "recommendations": [],
        "disagreements": [],
        "questions": [{"question": "Were the props changed?", "why": "The peak moved."}],
        "tests_to_fly": [{"test": "Hover 60 s", "look_for": "Lower gyro noise above 60 Hz"}],
    }  # fmt: skip
    return base | over


def accept(engine: tuple[Any, Any, Path], packet_id: str, reply: Any, **over: Any) -> Any:
    params: dict[str, Any] = {
        "packet_id": packet_id, "model": "test-model", "stop_reason": "end_turn",
        "reply": reply if reply is None or isinstance(reply, str) else json.dumps(reply),
        "usage": {"input_tokens": 1000, "output_tokens": 200}, "tool_calls": 2,
        "cost_usd": 0.01,
    } | over  # fmt: skip
    return rpc(engine, "ai.accept", **params)


def test_a_reply_is_checked_and_its_changes_validated(engine: tuple[Any, Any, Path]) -> None:
    flight = flown(engine, FIXTURES / "copter-quad-hover.bin")
    prepared = sent_packet(engine, flight)
    pid = prepared["packet_id"]
    packet = json.loads(prepared["packet_json"])
    finding = packet["findings"][0]["ref"]
    # Not shown: a reference the packet does not hold, or a reply that breaks the schema.
    bad = accept(engine, pid, _reply(root_causes=[{"title": "x", "explanation": "y",
                                                   "refs": ["F999"]}]))["result"]  # fmt: skip
    assert bad["outcome"] == "invalid" and "F999" in bad["reasons"][0] and bad["reply"] is None
    assert accept(engine, pid, {"summary": 3})["result"]["outcome"] == "invalid"
    assert accept(engine, pid, "not json")["result"]["outcome"] == "invalid"
    assert accept(engine, pid, None, stop_reason="refusal")["result"]["outcome"] == "declined"
    cut = accept(engine, pid, "{", stop_reason="max_tokens")["result"]
    assert cut["outcome"] == "failed"
    # Accepted: each change through the recommendation engine.
    recs: list[dict[str, Any]] = [
        {"param": "INS_HNTCH_BW", "value": 35, "confidence": "medium", "reason": "Widen the notch.",
         "refs": [finding]},
        {"param": "INS_HNTCH_MODE", "value": 9, "confidence": "low", "reason": "Out of range.",
         "refs": [finding]},
        {"param": "FS_THR_ENABLE", "value": 0, "confidence": "low", "reason": "Never automatic.",
         "refs": [finding]},
        {"param": "INS_HNTCH_ATT", "value": 50, "confidence": "low", "reason": "No finding.",
         "refs": []},
    ]  # fmt: skip
    good = accept(engine, pid, _reply(recommendations=recs))["result"]
    assert good["outcome"] == "accepted" and good["reply"]["summary"].startswith("Motor noise")
    verdicts = {v["param"]: v for v in good["recommendations"]}
    assert verdicts["INS_HNTCH_MODE"]["status"] == "blocked"
    assert verdicts["FS_THR_ENABLE"]["status"] == "blocked"
    assert verdicts["INS_HNTCH_ATT"]["status"] == "refused"  # cites no finding
    assert verdicts["INS_HNTCH_BW"]["status"] in ("next", "waiting", "disputed", "outranked")
    # The plan carries the validated AI change, labelled as AI, with what it needs first
    # (the notch switched on), and nothing it refused.
    plan = rpc(engine, "recommend.plan", flight_id=flight)["result"]
    ai = [r for r in plan["recommendations"] if any(s["origin"] == "ai" for s in r["sources"])]
    shown = {r["canonical"] for r in ai if r["status"] != "blocked"}
    assert "INS_HNTCH_BW" in shown and shown <= {"INS_HNTCH_BW", "INS_HNTCH_ENABLE"}
    assert all(r["why"]["en"] for r in ai)
    # The flight report reads the latest exchange; the cap reads the month's spend.
    assert rpc(engine, "ai.results", flight_id=flight)["result"]["id"] == good["id"]
    spend = rpc(engine, "ai.spend")["result"]
    assert spend["exchanges"] >= 6 and spend["cost_usd"] >= 0.06


def test_only_a_sent_packet_starts_an_exchange(engine: tuple[Any, Any, Path]) -> None:
    flight = flown(engine, FIXTURES / "plane-circuit.bin")
    prepared = rpc(engine, "ai.prepare", flight_id=flight)["result"]
    assert accept(engine, prepared["packet_id"], _reply())["error"]["code"] == -32003
    no_tool = rpc(engine, "ai.tool", packet_id=prepared["packet_id"], name="spectrum",
                  input={"axis": "x", "stage": "before"})  # fmt: skip
    assert no_tool["error"]["code"] == -32003
    assert rpc(engine, "ai.tool", packet_id="x", name="nope", input={})["error"]["code"] == -32602
    assert rpc(engine, "ai.spend", month="2026")["error"]["code"] == -32602


def test_a_chat_turn_carries_the_question_and_the_conversation(
    engine: tuple[Any, Any, Path],
) -> None:
    flight = flown(engine, FIXTURES / "quadplane-hover.bin")
    assert rpc(engine, "ai.prepare", flight_id=flight, purpose="chat")["error"]["code"] == -32602
    long = rpc(engine, "ai.prepare", flight_id=flight, purpose="chat", question="x" * 501)
    assert long["error"]["code"] == -32602
    first = rpc(
        engine, "ai.prepare", flight_id=flight, purpose="chat", question="Why the yaw wobble?"
    )
    packet = json.loads(first["result"]["packet_json"])
    assert packet["question"] == "Why the yaw wobble?" and packet["conversation"] == []
    rpc(engine, "ai.record", packet_id=first["result"]["packet_id"], shell_reasons=[], sent=True)
    ref = packet["findings"][0]["ref"]
    answer = {"answer": f"The rate gains are untuned ({ref}).", "refs": [ref]}
    got = accept(engine, first["result"]["packet_id"], answer)["result"]
    assert got["outcome"] == "accepted" and got["recommendations"] == []
    assert (
        accept(engine, first["result"]["packet_id"], {"answer": "x", "refs": ["F999"]})["result"][
            "outcome"
        ]
        == "invalid"
    )
    history = rpc(engine, "ai.chat_history", flight_id=flight)["result"]
    assert [h["question"] for h in history] == ["Why the yaw wobble?"]
    assert history[0]["ref_targets"][ref]
    # The next turn carries the earlier one, and is checked like the rest.
    second = rpc(engine, "ai.prepare", flight_id=flight, purpose="chat", question="And the fix?")
    turns = json.loads(second["result"]["packet_json"])["conversation"]
    assert turns == [{"role": "user", "text": "Why the yaw wobble?"},
                     {"role": "assistant", "text": answer["answer"]}]  # fmt: skip
    # A typed position never leaves: the engine refuses the packet.
    leak = rpc(engine, "ai.prepare", flight_id=flight, purpose="chat",
               question="Wobble near 47.12345, 8.54321 by the quarry?")["result"]  # fmt: skip
    assert leak["engine"]["ok"] is False and "packet_json" not in leak
