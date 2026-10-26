"""What happens to recommendations: what a person decides, and what the next flight
shows (spec: Recommendation engine, "Closing the loop").

A plan's recommendations are kept per airframe, once each: the same change proposed
again by a later flight's plan is the same recommendation. A proposal that a later
plan replaces with another value before anyone acted on it is superseded.

A person accepts, rejects or marks a recommendation applied. The engine also sees a
change applied when a later flight of the same airframe flies with the proposed value.
That flight then grades it against its expected effect, which is that the findings
it answered clear:

- confirmed: none of them is raised again, or only at a lower severity;
- no effect: the lead finding is raised again at the same severity;
- regressed: one of them is raised at a higher severity.

A grade needs the analyzer that raised the finding to have judged the later flight; a
log without the data it needs leaves the recommendation applied, ungraded. The checks
its procedure lists for the next log (a message logged, a metric within a limit, a
parameter's value) are run too: a cleared finding with a failed check is no effect.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from flight_insights.history.persist import load_snapshot
from flight_insights.knowledge import Pack
from flight_insights.store.db import Store, StoreError

OPEN = ("proposed", "accepted", "applied")
GRADES = ("confirmed", "no effect", "regressed")
# What a person may change a status to; grades and "superseded" are the engine's.
TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "proposed": ("accepted", "rejected", "applied"),
    "accepted": ("proposed", "rejected", "applied"),
    "applied": ("accepted", "rejected"),
    "rejected": ("proposed",),
}
SEVERITY_RANK = {"info": 0, "caution": 1, "out": 2, "safety": 3}
TOLERANCE = 1e-6


class RecommendationError(ValueError):
    """A status change that is not allowed, or an unknown recommendation."""


class RecommendationConflict(RecommendationError):
    """The recommendation changed since the caller read it."""


def _same(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) < TOLERANCE


def record_row(cur: Any, row: Sequence[Any]) -> dict[str, Any]:
    out = {d[0]: v for d, v in zip(cur.description, row, strict=True)}
    out["detail"] = json.loads(out.pop("detail_json"))
    return out


def _open_for(store: Store, airframe_id: str) -> list[dict[str, Any]]:
    marks = ", ".join("?" for _ in OPEN)
    cur = store.conn.execute(
        f"SELECT * FROM recommendation WHERE airframe_id = ? AND status IN ({marks}) "
        "ORDER BY created_hlc",
        (airframe_id, *OPEN),
    )
    return [record_row(cur, r) for r in cur.fetchall()]


def _code(r: Mapping[str, Any]) -> str:
    return str(r["source"]).removeprefix("finding:")


def record_plan(store: Store, plan: dict[str, Any]) -> dict[str, Any]:
    """Keeps the plan's recommendations for its airframe and adds each one's stored
    `id`, `record_status` and `record_version` to the plan. Blocked ones are not kept:
    nothing can be done with them but read why. A flight with no airframe keeps nothing."""
    flight = store.get("flight", plan["flight"])
    airframe_id = flight["airframe_id"] if flight else None
    if airframe_id is None:
        return plan
    with store.transaction():
        existing = _open_for(store, airframe_id)
        for r in plan["recommendations"]:
            if r["status"] == "blocked":
                continue
            param, code = r["canonical"], _code(r)
            proposed = r.get("proposed_canonical")
            same = [
                e for e in existing
                if e["param"] == param and e["detail"].get("code") == code
                and (param is None or _same(e["proposed_value"], proposed))
            ]  # fmt: skip
            if same:
                r["id"], r["record_status"] = same[0]["id"], same[0]["status"]
                r["record_version"] = same[0]["version"]
                continue
            # The same change for this same flight, already decided: it stays decided, and
            # one record, which is also how every other laptop knows it (natural_key).
            cur = store.conn.execute(
                "SELECT * FROM recommendation WHERE flight_id = ? AND finding_code IS ? "
                "AND param IS ? AND proposed_value IS ?",
                (plan["flight"], code, param, proposed),
            )
            if (found := cur.fetchone()) is not None:
                decided = record_row(cur, found)
                r["id"], r["record_status"] = decided["id"], decided["status"]
                r["record_version"] = decided["version"]
                continue
            for old in existing:  # an earlier proposal for this parameter, never acted on
                if param is not None and old["param"] == param and old["status"] == "proposed":
                    old["detail"]["superseded_by_flight"] = plan["flight"]
                    store.update(
                        "recommendation", old["id"], old["version"],
                        {"status": "superseded", "detail_json": json.dumps(old["detail"])},
                    )  # fmt: skip
                    old["status"] = "superseded"
            detail = {
                k: v for k, v in r.items() if k not in ("id", "record_status", "record_version")
            }
            detail["code"] = code
            finding_id = r["finding"]
            if r["sources"] and r["sources"][0]["origin"] == "ai":  # kept under what it answers
                finding_id = r["sources"][0]["params"]["cited"][0]
            r["id"] = store.insert(
                "recommendation",
                {
                    "finding_id": finding_id,
                    "finding_code": code,
                    "airframe_id": airframe_id,
                    "flight_id": plan["flight"],
                    "param": param,
                    "current_value": r.get("current_canonical"),
                    "proposed_value": proposed,
                    "stage": r["stage"],
                    "status": "proposed",
                    "detail_json": json.dumps(detail),
                },
            )
            r["record_status"], r["record_version"] = "proposed", 1
            existing.append(
                {"id": r["id"], "param": param, "proposed_value": proposed, "status": "proposed",
                 "version": 1, "detail": detail}
            )  # fmt: skip
    return plan


def set_status(store: Store, rec_id: str, status: str, version: int) -> dict[str, Any]:
    """A person's decision. Grades are the engine's to give."""
    row = store.get("recommendation", rec_id)
    if row is None:
        raise RecommendationError(f"no recommendation {rec_id}")
    allowed = TRANSITIONS.get(row["status"], ())
    if status not in allowed:
        raise RecommendationError(f"a {row['status']} recommendation cannot become {status}")
    try:
        new_version = store.update("recommendation", rec_id, version, {"status": status})
    except StoreError as e:
        raise RecommendationConflict(str(e)) from None
    return {"id": rec_id, "status": status, "version": new_version}


def history(store: Store, airframe_id: str) -> list[dict[str, Any]]:
    cur = store.conn.execute(
        "SELECT * FROM recommendation WHERE airframe_id = ? ORDER BY created_hlc DESC",
        (airframe_id,),
    )
    return [record_row(cur, r) for r in cur.fetchall()]


def _when(flight: Mapping[str, Any]) -> tuple[int, Any]:
    """When a flight flew: UTC take-off where the log has it, else import order."""
    if flight.get("boot_utc_us") is not None and flight.get("arm_us") is not None:
        return (0, int(flight["boot_utc_us"]) + int(flight["arm_us"]))
    return (1, flight["created_hlc"])


def flew_before(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    ka, kb = _when(a), _when(b)
    if ka[0] == kb[0]:
        return bool(ka[1] < kb[1])
    return bool(a["created_hlc"] < b["created_hlc"])


def _check(
    check: Mapping[str, Any],
    cleared: bool,
    analysis: Mapping[str, Any],
    params: Mapping[str, float],
    messages: Collection[str] | None,
) -> bool | None:
    """One of a procedure's checks against the later flight; None when it cannot say."""
    kind = check.get("check")
    if kind == "finding_clears":
        return cleared
    if kind == "has_message":
        if messages is None:
            return None
        name = str(check["message"])
        return any(m == name or m.startswith((f"{name}[", f"{name}~")) for m in messages)
    if kind == "metric":
        values = [
            float(m["value"])
            for m in analysis.get("metrics", ())
            if m["metric"] == check["metric"] and m["value"] is not None
        ]
        if not values:
            return None
        if "below" in check:
            return all(v < float(check["below"]) for v in values)
        return all(v > float(check["above"]) for v in values) if "above" in check else None
    if kind == "has_metric":  # measured at all: a metric needs enough data to be given
        return any(m["metric"] == check["metric"] for m in analysis.get("metrics", ()))
    if kind == "param_is":
        value = params.get(str(check["param"]))
        return None if value is None else _same(value, float(check["value"]))
    return None


def grade_flight(
    store: Store,
    pack: Pack,
    flight_id: str,
    analysis: Mapping[str, Any],
    messages: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """Grades the airframe's open recommendations from earlier flights against this
    flight; returns the ones it graded or saw applied. `messages`, the flight's logged
    message types, lets the procedures' message checks run."""
    flight = store.get("flight", flight_id)
    if flight is None or flight["airframe_id"] is None or not flight["param_snapshot_id"]:
        return []
    snapshot = load_snapshot(store, flight["param_snapshot_id"])
    params = snapshot.dialect(pack).to_canonical(snapshot.values).values
    raised: dict[str, str] = {}
    for f in analysis["findings"]:
        if SEVERITY_RANK[f["severity"]] >= SEVERITY_RANK.get(raised.get(f["code"], ""), -1):
            raised[f["code"]] = f["severity"]
    judged = {name for name, o in analysis["analyzers"].items() if not o.get("skipped")}
    judged.add("followup")  # the plan's own follow-ups: judged by the parameters alone
    out: list[dict[str, Any]] = []
    with store.transaction():
        for rec in _open_for(store, flight["airframe_id"]):
            source = store.get("flight", rec["flight_id"]) if rec["flight_id"] else None
            if source is None or source["id"] == flight_id or not flew_before(source, flight):
                continue
            if rec["param"] is not None:
                if not _same(params.get(rec["param"]), rec["proposed_value"]):
                    continue  # not flown with the change (yet)
            elif rec["status"] != "applied":
                continue  # an action is graded once a person says it was done
            sources: list[dict[str, Any]] = rec["detail"].get("sources", [])
            lead = sources[0] if sources else {"code": rec["detail"]["code"], "severity": "info"}
            grade: str | None = None
            if any(s.get("analyzer") not in judged for s in sources):
                status = "applied"  # the later log cannot judge it
            else:
                before = SEVERITY_RANK[lead["severity"]]
                after = {s["code"]: raised[s["code"]] for s in sources if s["code"] in raised}
                worst = max((SEVERITY_RANK[v] for v in after.values()), default=None)
                cleared = SEVERITY_RANK.get(after.get(lead["code"], ""), -1) < before
                checks = [
                    dict(c) | {"passed": _check(c, cleared, analysis, params, messages)}
                    for step in rec["detail"].get("procedure", ())
                    if step.get("kind") == "verify"
                    for c in step.get("checks", ())
                ]
                if worst is not None and worst > before:
                    grade = "regressed"
                elif lead["code"] in after and SEVERITY_RANK[after[lead["code"]]] == before:
                    grade = "no effect"
                elif any(c["passed"] is False for c in checks):
                    grade = "no effect"  # cleared, but the next log does not show it worked
                else:
                    grade = "confirmed"
                status = grade
                rec["detail"]["grade"] = {
                    "flight": flight_id, "before": lead["severity"], "after": after,
                    "checks": checks,
                }  # fmt: skip
            if status == rec["status"]:
                continue
            store.update(
                "recommendation", rec["id"], rec["version"],
                {"status": status, "graded_flight_id": flight_id if grade else None,
                 "detail_json": json.dumps(rec["detail"])},
            )  # fmt: skip
            out.append({"id": rec["id"], "param": rec["param"], "status": status})
    return out
