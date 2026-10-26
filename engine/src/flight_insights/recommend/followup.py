"""What a flight's plan carries over from the airframe's earlier plans (spec:
Recommendation engine, "Procedures" and "Closing the loop").

- Settings that were for a test flight only come out again once the change they served
  is confirmed: while a later flight still flies with them, its plan proposes their
  revert values, as a finding of its own with the revert as its change.
- The diagnostics that turn raw gyro or fast notch logging on for a filter check are
  not proposed again while the filter settings and the airframe's build are the same
  as on a flight that was checked with raw gyro data.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from typing import Any

from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.run import analysis_results
from flight_insights.history.persist import load_snapshot
from flight_insights.knowledge import Pack
from flight_insights.params.dialect import Dialect
from flight_insights.recommend.records import GRADES, TOLERANCE, flew_before, record_row
from flight_insights.store.db import Store

RECENT = 20  # earlier flights looked at for a filter check


def revert_findings(
    store: Store, flight_id: str, dialect: Dialect, params: Mapping[str, float]
) -> list[dict[str, Any]]:
    """A finding for each test-flight setting this flight still flies with, once the
    change it served is confirmed."""
    flight = store.get("flight", flight_id)
    if flight is None or flight["airframe_id"] is None:
        return []
    cur = store.conn.execute(
        "SELECT * FROM recommendation WHERE airframe_id = ? AND param IS NOT NULL "
        "ORDER BY created_hlc",
        (flight["airframe_id"],),
    )
    rows = [record_row(cur, r) for r in cur.fetchall()]
    out: dict[str, dict[str, Any]] = {}
    for rec in rows:
        detail = rec["detail"]
        native, revert = detail.get("param"), detail.get("revert")
        if native is None or revert is None or rec["status"] not in (*GRADES, "applied"):
            continue
        source = store.get("flight", rec["flight_id"]) if rec["flight_id"] else None
        if source is None or source["id"] == flight_id or not flew_before(source, flight):
            continue
        if not _served(rec, rows):
            continue
        name, converted = dialect.canonical(native, float(revert))
        back = float(revert) if converted is None else converted
        value = _still_on(params.get(name), rec["proposed_value"], back, _bitmask(dialect, native))
        if value is None or name in out:
            continue
        out[name] = {
            "id": f"followup:{rec['id']}",
            "analyzer": "followup",
            "code": "followup.revert_test_settings",
            "severity": "info",
            "confidence": "high",
            "params": {"param": native, "recommendation": rec["id"], "flight": source["id"]},
            "evidence": [f"recommendation:{rec['id']}"],
            "changes": [{"param": name, "proposed": value, "stage": "evidence"}],
            "stage": "evidence",
            "origin": "measured",
        }
    return list(out.values())


def _served(rec: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the change a test-flight setting served is confirmed: its findings' lasting
    changes that were on offer with it, or, for a diagnostic with none, itself.

    A later plan proposing the same test-flight setting keeps its first record, while
    its lasting changes may be new ones (other values, after another flight's data). So
    those graded by the same flight count too, and ones replaced or rejected do not."""
    codes = {s["code"] for s in rec["detail"].get("sources", ())}
    graded = rec.get("graded_flight_id")
    lasting = [
        r
        for r in rows
        if (r["flight_id"] == rec["flight_id"] or (graded and r.get("graded_flight_id") == graded))
        and r["id"] != rec["id"]
        and r["status"] not in ("superseded", "rejected")
        and r["detail"].get("revert") is None
        and codes & {s["code"] for s in r["detail"].get("sources", ())}
    ]
    if lasting:
        return all(r["status"] == "confirmed" for r in lasting)
    return rec["status"] == "confirmed"


def _bitmask(dialect: Dialect, native: str) -> bool:
    return bool((dialect.meta.get(native) or {}).get("bitmask"))


def _still_on(
    current: float | None, proposed: float | None, revert: float, bitmask: bool
) -> float | None:
    """The value that takes a test-flight setting out of `current`, or None when it is
    out already. On a bitmask, only the test flight's bits change back, whatever else
    changed since: those it switched on go off, those it switched off come back on."""
    if current is None or proposed is None:
        return None
    if bitmask:
        on = int(proposed) & ~int(revert)
        off = int(revert) & ~int(proposed)
        if (on and int(current) & on == on) or (off and not int(current) & off):
            return float(int(current) & ~on | off)
        return None
    return revert if abs(current - proposed) < TOLERANCE else None


def filters_checked_on(
    store: Store, pack: Pack, reg: Registry, flight_id: str, params: Mapping[str, float]
) -> str | None:
    """An earlier flight of the airframe checked with raw gyro data, with the same
    filter settings and build as this one; None when there is none."""
    flight = store.get("flight", flight_id)
    if flight is None or flight["airframe_id"] is None:
        return None
    patterns: Sequence[str] = reg.rules["logging"]["filter_settings"]["names"]

    def settings(values: Mapping[str, float]) -> dict[str, float]:
        return {k: v for k, v in values.items() if any(fnmatch.fnmatchcase(k, p) for p in patterns)}

    mine = settings(params)
    ids = store.conn.execute(
        "SELECT id FROM flight WHERE airframe_id = ? AND id != ?",
        (flight["airframe_id"], flight_id),
    ).fetchall()
    rows = [store.get("flight", i[0]) for i in ids]
    candidates = sorted(
        (r for r in rows if r is not None and flew_before(r, flight)),
        key=lambda r: r["created_hlc"],
        reverse=True,
    )[:RECENT]
    for other in candidates:
        if (
            other["build_version_id"] != flight["build_version_id"]
            or not other["param_snapshot_id"]
        ):
            continue
        results = analysis_results(store, other["id"], reg)
        filters = None if results is None else results["analyzers"].get("filters")
        if filters is None or filters.get("skipped"):
            continue  # not checked with raw gyro data
        snapshot = load_snapshot(store, other["param_snapshot_id"])
        theirs = settings(snapshot.dialect(pack).to_canonical(snapshot.values).values)
        if theirs.keys() == mine.keys() and all(abs(theirs[k] - mine[k]) < TOLERANCE for k in mine):
            return str(other["id"])
    return None
