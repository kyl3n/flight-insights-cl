"""The laptop's record of every AI packet (spec: AI layer, "Visible to the user"): what
was built, what each check said, and whether it was sent."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from flight_insights.ai.schema import SCHEMA_ID
from flight_insights.ai.scrubber import Violation
from flight_insights.store.db import Store, StoreError


def packet_text(packet: Any) -> str:
    """The packet as it is checked, stored, hashed and sent: one canonical form."""
    return json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_packet(
    store: Store,
    flight_id: str | None,
    purpose: str,
    text: str,
    engine_violations: Sequence[Violation],
) -> dict[str, Any]:
    packet_id = store.insert(
        "ai_packet",
        {
            "flight_id": flight_id,
            "purpose": purpose,
            "schema_id": SCHEMA_ID,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "packet_json": text,
            "engine_verdict": "refused" if engine_violations else "pass",
            "engine_reasons_json": json.dumps([asdict(v) for v in engine_violations]),
        },
    )
    row = store.get("ai_packet", packet_id)
    assert row is not None
    return row


def record_outcome(
    store: Store, packet_id: str, shell_reasons: Sequence[dict[str, str]], sent: bool
) -> dict[str, Any]:
    """What the shell's check said, and whether the packet went. A packet either check
    refused can never be marked sent."""
    row = store.get("ai_packet", packet_id)
    if row is None:
        raise StoreError(f"no AI packet {packet_id}")
    if row["shell_verdict"] is not None:
        raise StoreError(f"AI packet {packet_id} already has the shell's verdict")
    refused = bool(shell_reasons) or row["engine_verdict"] != "pass"
    if sent and refused:
        raise StoreError("a refused packet cannot have been sent")
    reasons = [
        {"path": str(r.get("path", "")), "rule": str(r.get("rule", ""))} for r in shell_reasons
    ]
    store.update(
        "ai_packet",
        packet_id,
        int(row["version"]),
        {
            "shell_verdict": "refused" if shell_reasons else "pass",
            "shell_reasons_json": json.dumps(reasons),
            "sent": int(sent),
            "sent_hlc": store.clock.now() if sent else None,
        },
    )
    out = store.get("ai_packet", packet_id)
    assert out is not None
    return out
