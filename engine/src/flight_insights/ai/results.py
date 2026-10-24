"""What the AI layer's accepted replies hold, read back from the store: for the flight
report, and for the recommendation engine, which validates and merges the parameter
changes a reply proposes like any analyzer's (spec: AI layer, "Request flow" 5).

An AI change always answers a measured finding: it cites one, takes that finding's
tuning stage, and cites the evidence the finding rests on. It ranks below a measured
change for the same parameter, and a measured finding against it disputes it. It is
never graded on later flights by itself: no analyzer judges an AI suggestion.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from flight_insights.analyzers.run import run_finding_keys
from flight_insights.store.db import Store

AI_CODE = "ai.recommendation"


def latest(store: Store, flight_id: str, purpose: str = "analysis") -> dict[str, Any] | None:
    """The flight's latest accepted exchange, with its packet's references."""
    row = store.conn.execute(
        "SELECT x.id, x.reply_json, x.recommendations_json, x.model, x.created_hlc, "
        "p.refs_json, p.run_id, p.run_key "
        "FROM ai_exchange x JOIN ai_packet p ON p.id = x.packet_id "
        "WHERE x.flight_id = ? AND x.purpose = ? AND x.outcome = 'accepted' "
        "ORDER BY x.created_hlc DESC LIMIT 1",
        (flight_id, purpose),
    ).fetchone()
    if row is None:
        return None
    xid, reply, verdicts, model, created, refs, run_id, run_key = row
    return {
        "id": xid,
        "reply": json.loads(reply),
        "recommendations": json.loads(verdicts),
        "model": model,
        "created": created,
        "refs": json.loads(refs),
        "run_id": run_id,
        "run_key": run_key,
    }


def same_run(exchange: Mapping[str, Any], analysis: Mapping[str, Any]) -> bool:
    """Whether a reply was made from this analysis: on any laptop, one made with the
    same versions; a packet from before run keys, the very run."""
    if exchange.get("run_key"):
        return exchange["run_key"] == analysis.get("run_key")
    return exchange.get("run_id") == analysis.get("run")


def _target(target: str, keys: Mapping[str, str]) -> str | None:
    """The finding a reference stands for, from {finding id: key}: by key, or by id in
    a packet from before keys."""
    if target in keys:
        return target
    return next((fid for fid, key in keys.items() if key == target), None)


def local_refs(store: Store, flight_id: str, refs: Mapping[str, str]) -> dict[str, str]:
    """A packet's references as this laptop's records: each F ref as the finding in the
    flight's latest run that it names, so the report can open it; E refs as they are."""
    run = store.conn.execute(
        "SELECT id FROM analysis_run WHERE flight_id = ? ORDER BY created_hlc DESC LIMIT 1",
        (flight_id,),
    ).fetchone()
    keys = run_finding_keys(store, run[0]) if run else {}
    out: dict[str, str] = {}
    for ref, target in refs.items():
        if not ref.startswith("F"):
            out[ref] = target
        elif (found := _target(target, keys)) is not None:
            out[ref] = found
    return out


def ai_findings(
    exchange: Mapping[str, Any] | None, findings: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The exchange's recommendations as findings the plan reads, each with the change
    it proposes in the firmware's own names (`native_changes`). `findings` are the
    analysis's own, to take stage and evidence from the ones cited; a recommendation
    citing none of them proposes nothing."""
    if exchange is None:
        return []
    by_id = {str(f["id"]): f for f in findings}
    keys = {str(f["id"]): str(f["key"]) for f in findings}
    refs: Mapping[str, str] = exchange["refs"]
    local = {x: _target(t, keys) for x, t in refs.items() if x.startswith("F")}
    out: list[dict[str, Any]] = []
    for i, r in enumerate(exchange["reply"]["recommendations"]):
        cited = [by_id[f] for x in r["refs"] if (f := local.get(x)) is not None]
        if not cited:
            continue
        evidence = sorted(
            {refs[x] for x in r["refs"] if x.startswith("E") and x in refs}
            | {e for f in cited for e in f["evidence"]}
        )
        out.append(
            {
                "id": f"ai:{exchange['id']}:{i}",
                "analyzer": "ai",
                "code": AI_CODE,
                "severity": "info",
                "confidence": r["confidence"],
                "params": {
                    "reason": r["reason"],
                    "answers": [f["code"] for f in cited],
                    "cited": [str(f["id"]) for f in cited],
                },
                "evidence": evidence,
                "origin": "ai",
                "stage": cited[0]["stage"],
                "native_changes": [(r["param"], float(r["value"]))],
            }
        )
    return out
