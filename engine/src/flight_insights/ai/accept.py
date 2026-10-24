"""What came back from Claude, checked before anything is shown (spec: AI layer,
"Request flow" 4-5; "The UI never shows unvalidated AI output as a parameter change").

A reply is accepted only if it fits the reply schema with its limits and cites only
references its packet holds. Its parameter changes then go through the recommendation
engine like any analyzer's: the firmware's metadata, step limits, the changes never
made automatically, the dialect, and conflicts with measured findings. Each change's
verdict is kept beside the reply, which is kept exactly as it came.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, cast

from flight_insights import recommend
from flight_insights.ai.exchange import REPLY
from flight_insights.ai.results import AI_CODE
from flight_insights.ai.schema import validate
from flight_insights.analyzers.flight import FlightError
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.run import analysis_results
from flight_insights.knowledge import Pack
from flight_insights.store.db import Store, StoreError

Outcome = Literal["accepted", "invalid", "declined", "failed"]
USAGE = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _refs_in(reply: Mapping[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key in ("root_causes", "recommendations"):
        for i, item in enumerate(reply[key]):
            out += [(f"$.{key}[{i}]", r) for r in item["refs"]]
    out += [(f"$.disagreements[{i}]", d["ref"]) for i, d in enumerate(reply["disagreements"])]
    return out


def check_reply(reply: Any, refs: Mapping[str, str]) -> list[str]:
    """Why the reply cannot be shown: where it breaks the schema, or a reference its
    packet does not hold. Empty when it fits."""
    problems = validate(reply, REPLY)
    if problems:
        return problems
    return [
        f"{path}: cites {ref}, which the packet does not hold"
        for path, ref in _refs_in(cast(Mapping[str, Any], reply))
        if ref not in refs
    ]


def _verdicts(
    store: Store, pack: Pack, flight_id: str, reply: Mapping[str, Any], refs: Mapping[str, str]
) -> list[dict[str, Any]]:
    """What the recommendation engine made of each change the reply proposes."""
    base: list[dict[str, Any]] = [
        {"param": r["param"], "value": r["value"], "status": "refused", "notes": []}
        for r in reply["recommendations"]
    ]
    for v, r in zip(base, reply["recommendations"], strict=True):
        if not any(x.startswith("F") for x in r["refs"]):
            v["notes"] = [{"reason": "cites_no_finding"}]
    try:
        plan = recommend.flight_plan(
            store, None, pack, flight_id, _analysis(store, pack, flight_id)
        )
    except FlightError:
        return base
    planned = recommend.to_json(plan, Registry.load(pack))["recommendations"]
    for rec in planned:
        for s in rec["sources"] + [s for a in rec["alternatives"] for s in a["sources"]]:
            if s["code"] != AI_CODE:
                continue
            i = int(str(s["finding"]).rsplit(":", 1)[1])
            lost = s not in rec["sources"]
            base[i] |= {
                "status": "outranked" if lost else rec["status"],
                "notes": [{"reason": n["reason"], "text": n["text"]} for n in rec["notes"]],
                "native": rec["param"],
            }
    for v in base:
        if v["status"] == "refused" and not v["notes"]:  # the flight already flies it
            v["status"] = "unchanged"
    return base


def _analysis(store: Store, pack: Pack, flight_id: str) -> dict[str, Any]:
    found = analysis_results(store, flight_id, Registry.load(pack))
    if found is None:
        raise FlightError(f"flight {flight_id} has no analysis")
    return found


def accept(
    store: Store,
    pack: Pack,
    packet_id: str,
    *,
    model: str,
    stop_reason: str | None,
    reply_text: str | None,
    usage: Mapping[str, int] | None = None,
    tool_calls: int = 0,
    cost_usd: float = 0.0,
    failure: str | None = None,
) -> dict[str, Any]:
    """Records one exchange the packet started and what its reply came to."""
    packet = store.get("ai_packet", packet_id)
    if packet is None:
        raise StoreError(f"no AI packet {packet_id}")
    if not packet["sent"] or packet["purpose"] == "tool":
        raise StoreError("only a packet that was sent can start an exchange")
    refs: dict[str, str] = json.loads(packet["refs_json"])
    reasons: list[str] = []
    reply: Any = None
    outcome: Outcome
    if stop_reason == "refusal":
        outcome = "declined"
    elif failure is not None or reply_text is None or stop_reason != "end_turn":
        outcome = "failed"
        reasons = [failure or f"the reply stopped early ({stop_reason})"]
    else:
        try:
            reply = json.loads(reply_text)
        except json.JSONDecodeError:
            reply = None
        reasons = ["$: not JSON"] if reply is None else check_reply(reply, refs)
        outcome = "invalid" if reasons else "accepted"
    xid = store.insert(
        "ai_exchange",
        {
            "packet_id": packet_id,
            "flight_id": packet["flight_id"],
            "purpose": packet["purpose"],
            "month": datetime.now(UTC).strftime("%Y-%m"),
            "model": model,
            "stop_reason": stop_reason,
            "outcome": outcome,
            "reasons_json": json.dumps(reasons[:32]),
            "reply_json": reply_text if outcome in ("accepted", "invalid") else None,
            "tool_calls": int(tool_calls),
            "cost_usd": float(cost_usd),
            **{k: int((usage or {}).get(k, 0)) for k in USAGE},
        },
    )
    if outcome == "accepted" and packet["flight_id"]:
        verdicts = _verdicts(store, pack, packet["flight_id"], cast(dict[str, Any], reply), refs)
        row = store.get("ai_exchange", xid)
        assert row is not None
        store.update(
            "ai_exchange", xid, int(row["version"]), {"recommendations_json": json.dumps(verdicts)}
        )
    out = store.get("ai_exchange", xid)
    assert out is not None
    return _view(out)


def _view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "packet_id": row["packet_id"],
        "flight_id": row["flight_id"],
        "purpose": row["purpose"],
        "model": row["model"],
        "outcome": row["outcome"],
        "reasons": json.loads(row["reasons_json"]),
        "reply": json.loads(row["reply_json"]) if row["outcome"] == "accepted" else None,
        "recommendations": json.loads(row["recommendations_json"]),
        "tool_calls": row["tool_calls"],
        "cost_usd": row["cost_usd"],
        "created": row["created_hlc"],
    }


def results_for(store: Store, flight_id: str) -> dict[str, Any] | None:
    """The flight's latest exchange of any outcome, for the flight report."""
    row = store.conn.execute(
        "SELECT id FROM ai_exchange WHERE flight_id = ? AND purpose = 'analysis' "
        "ORDER BY created_hlc DESC LIMIT 1",
        (flight_id,),
    ).fetchone()
    if row is None:
        return None
    got = store.get("ai_exchange", row[0])
    assert got is not None
    packet = store.get("ai_packet", got["packet_id"])
    return _view(got) | {"run_id": packet["run_id"] if packet else None}


def spend(store: Store, month: str) -> dict[str, Any]:
    total, count = store.conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM ai_exchange WHERE month = ?", (month,)
    ).fetchone()
    return {"month": month, "cost_usd": round(float(total), 4), "exchanges": int(count)}


def sent(store: Store, limit: int = 100) -> list[dict[str, Any]]:
    """What left this machine, newest first: every packet as checked, with both
    verdicts. Never the tripwire, which is never kept."""
    cur = store.conn.execute(
        "SELECT id, created_hlc, flight_id, purpose, parent_id, sha256, packet_json, "
        "engine_verdict, engine_reasons_json, shell_verdict, shell_reasons_json, sent, sent_hlc "
        "FROM ai_packet ORDER BY created_hlc DESC LIMIT ?",
        (int(limit),),
    )
    names = [d[0] for d in cur.description]
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        row = dict(zip(names, r, strict=True))
        row["engine_reasons"] = json.loads(row.pop("engine_reasons_json"))
        row["shell_reasons"] = json.loads(row.pop("shell_reasons_json"))
        row["sent"] = bool(row["sent"])
        out.append(row)
    return out
