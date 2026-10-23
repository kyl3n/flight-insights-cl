"""Keeping snapshots and change sets in the store.

A snapshot is stored in the dialect it flew with, exactly as the firmware held
it, with its parameter-set ID. Change sets between two flights are grouped by
origin, each change with the evidence for its label.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

from flight_insights.history.changes import LabelledChange
from flight_insights.history.snapshot import ParamChange, Snapshot, SnapshotError, param_set_id
from flight_insights.knowledge import Pack
from flight_insights.store.db import Store


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def save_snapshot(
    store: Store, snapshot: Snapshot, pack: Pack, source_file_id: str | None = None
) -> str:
    bad = [n for n, v in snapshot.values.items() if math.isnan(v)]
    bad += [c.name for c in snapshot.changes if math.isnan(c.old) or math.isnan(c.new)]
    if bad:
        # SQLite stores NaN as NULL; refuse rather than lose the value silently.
        raise SnapshotError(f"NaN parameter values cannot be stored: {', '.join(sorted(bad))}")
    with store.transaction() as conn:
        snapshot_id = store.insert(
            "param_snapshot",
            {
                "set_id": param_set_id(snapshot, pack),
                "vehicle": snapshot.vehicle,
                "firmware": snapshot.firmware,
                "source_file_id": source_file_id,
                "origin": snapshot.origin,
                "confidence": snapshot.confidence,
                "taken_us": snapshot.time_us,
            },
        )
        conn.executemany(
            "INSERT INTO param_value (snapshot_id, name, value, default_value) VALUES (?, ?, ?, ?)",
            [
                (snapshot_id, n, v, _finite(snapshot.defaults.get(n)))
                for n, v in sorted(snapshot.values.items())
            ],
        )
        conn.executemany(
            "INSERT INTO param_change VALUES (?, ?, ?, ?, ?, ?)",
            [
                (snapshot_id, i, c.time_us, c.name, c.old, c.new)
                for i, c in enumerate(snapshot.changes)
            ],
        )
    return snapshot_id


def load_snapshot(store: Store, snapshot_id: str) -> Snapshot:
    row = store.get("param_snapshot", snapshot_id)
    if row is None:
        raise SnapshotError(f"no parameter snapshot {snapshot_id}")
    rows = store.conn.execute(
        "SELECT name, value, default_value FROM param_value WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchall()
    values = {n: v for n, v, _ in rows}
    defaults = {n: d for n, _, d in rows if d is not None}
    changes = tuple(
        ParamChange(t, n, old, new)
        for t, n, old, new in store.conn.execute(
            "SELECT time_us, name, old_value, new_value FROM param_change "
            "WHERE snapshot_id = ? ORDER BY seq",
            (snapshot_id,),
        )
    )
    return Snapshot(
        values,
        row["vehicle"],
        row["firmware"],
        row["origin"],
        row["confidence"],
        row["taken_us"],
        changes,
        defaults,
    )


def save_change_sets(
    store: Store,
    airframe_id: str,
    before_flight_id: str | None,
    after_flight_id: str,
    changes: Sequence[LabelledChange],
) -> list[str]:
    """One change set per origin. Returns their ids."""
    by_origin: dict[str, list[dict[str, Any]]] = {}
    for c in changes:
        by_origin.setdefault(c.origin, []).append(
            {
                "name": c.diff.name,
                "before": c.diff.before,
                "after": c.diff.after,
                "class": c.diff.param_class,
                "evidence": [
                    {"kind": e.kind, "detail": e.detail, "time_us": e.time_us, "source": e.source}
                    for e in c.evidence
                ],
            }
        )
    ids: list[str] = []
    with store.transaction():
        for origin, items in sorted(by_origin.items()):
            ids.append(
                store.insert(
                    "change_set",
                    {
                        "airframe_id": airframe_id,
                        "kind": "parameter",
                        "origin": origin,
                        "before_flight_id": before_flight_id,
                        "after_flight_id": after_flight_id,
                        "details_json": json.dumps({"changes": items}, allow_nan=False),
                    },
                )
            )
    return ids
