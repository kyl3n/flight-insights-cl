"""Platforms, airframes and their build versions in the store (spec: Platforms,
airframes and build configuration).

A platform's reference build holds its setup answers. An airframe inherits the
reference and records only where it differs; each recorded change makes a new
build version with its reason, and earlier flights keep the build they flew.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal

from flight_insights.platforms.completeness import Completeness, completeness
from flight_insights.platforms.schema import Schema, SetupError, check_value
from flight_insights.store.db import Store

ChangeReason = Literal["failure", "upgrade", "test", "detected"]
Spec = dict[str, dict[str, Any]]  # field id -> {"value": ..., "source": ...}


def _spec(row: dict[str, Any] | None) -> Spec:
    return json.loads(row["spec_json"]) if row else {}


def _apply(
    schema: Schema,
    spec: Spec,
    values: dict[str, Any],
    source: str,
    sources: dict[str, str] | None = None,
) -> Spec:
    """Checked values merged into a spec; None removes a field. `sources` gives a
    field its own provenance, such as "detected: FRAME_CLASS Quad"."""
    out = dict(spec)
    for field_id, value in values.items():
        checked = check_value(schema.field(field_id), value)
        if checked is None:
            out.pop(field_id, None)
        else:
            own = (sources or {}).get(field_id, source)
            if not own.startswith(source):
                raise SetupError(f"{field_id}: a source must start with {source!r}")
            out[field_id] = {"value": checked, "source": own[:200]}
    return out


def _completeness_json(c: Completeness) -> dict[str, Any]:
    return {
        "percent": c.percent,
        "ready": c.ready,
        "steps": c.steps,
        "gaps": [
            {"step": g.step, "field": g.field, "label": g.label, "prevents": g.prevents}
            for g in c.gaps
        ],
    }


def _require(store: Store, table: str, record_id: str) -> dict[str, Any]:
    row = store.get(table, record_id)
    if row is None:
        raise SetupError(f"no {table.replace('_', ' ')} {record_id}")
    return row


# --- platforms ------------------------------------------------------------------------


def create_platform(store: Store, schema: Schema, name: str, vehicle: str) -> str:
    spec = _apply(schema, {}, {"name": name, "vehicle": vehicle}, "entered")
    with store.transaction():
        platform = store.insert("platform", {"name": spec["name"]["value"], "vehicle": vehicle})
        build = store.insert(
            "build_version",
            {"platform_id": platform, "number": 1, "reason_kind": "setup",
             "spec_json": json.dumps(spec), "effective_date": date.today().isoformat()},
        )  # fmt: skip
        store.update("platform", platform, 1, {"reference_build_id": build})
    return platform


def platform_view(store: Store, schema: Schema, platform_id: str) -> dict[str, Any]:
    platform = _require(store, "platform", platform_id)
    build = _require(store, "build_version", platform["reference_build_id"])
    spec = _spec(build)
    airframes = store.conn.execute(
        "SELECT count(*) FROM airframe WHERE platform_id = ?", (platform_id,)
    ).fetchone()[0]
    return {
        "id": platform_id,
        "name": platform["name"],
        "vehicle": platform["vehicle"],
        "version": platform["version"],
        "reference_build": {"id": build["id"], "version": build["version"]},
        "spec": spec,
        "completeness": _completeness_json(completeness(schema, spec)),
        "airframes": airframes,
    }


def list_platforms(store: Store, schema: Schema) -> list[dict[str, Any]]:
    ids = [r[0] for r in store.conn.execute("SELECT id FROM platform ORDER BY name")]
    return [platform_view(store, schema, i) for i in ids]


def save_answers(
    store: Store,
    schema: Schema,
    platform_id: str,
    build_version: int,
    values: dict[str, Any],
    source: str = "entered",
    sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Save setup answers on the platform's reference build, which must still be at
    `build_version` (otherwise someone else changed it first)."""
    platform = _require(store, "platform", platform_id)
    build = _require(store, "build_version", platform["reference_build_id"])
    spec = _apply(schema, _spec(build), values, source, sources)
    if "name" in values and "name" not in spec:
        raise SetupError("a platform needs a name")
    if spec.get("vehicle", {}).get("value") not in (None, platform["vehicle"]):
        raise SetupError("the vehicle type is fixed once the platform exists")
    with store.transaction():
        store.update("build_version", build["id"], build_version, {"spec_json": json.dumps(spec)})
        if "name" in values:
            store.update(
                "platform", platform_id, platform["version"], {"name": spec["name"]["value"]}
            )
    return platform_view(store, schema, platform_id)


# --- airframes ------------------------------------------------------------------------


def create_airframe(store: Store, platform_id: str, name: str) -> str:
    platform = _require(store, "platform", platform_id)
    if not name.strip():
        raise SetupError("an airframe needs a name or tail number")
    with store.transaction():
        airframe = store.insert("airframe", {"name": name.strip(), "platform_id": platform_id})
        build = store.insert(
            "build_version",
            {"airframe_id": airframe, "number": 1, "based_on_id": platform["reference_build_id"],
             "reason_kind": "setup", "reason": "built from the platform's reference build",
             "spec_json": "{}", "effective_date": date.today().isoformat()},
        )  # fmt: skip
        store.update("airframe", airframe, 1, {"current_build_id": build})
    return airframe


def airframe_view(store: Store, schema: Schema, airframe_id: str) -> dict[str, Any]:
    airframe = _require(store, "airframe", airframe_id)
    platform = _require(store, "platform", airframe["platform_id"])
    reference = _spec(_require(store, "build_version", platform["reference_build_id"]))
    current = _require(store, "build_version", airframe["current_build_id"])
    overrides = _spec(current)
    effective = reference | overrides
    history = [
        {"id": r[0], "number": r[1], "reason_kind": r[2], "reason": r[3], "effective_date": r[4],
         "changed": sorted(json.loads(r[5]))}
        for r in store.conn.execute(
            "SELECT id, number, reason_kind, reason, effective_date, spec_json FROM build_version "
            "WHERE airframe_id = ? ORDER BY number",
            (airframe_id,),
        )
    ]  # fmt: skip
    return {
        "id": airframe_id,
        "name": airframe["name"],
        "platform": {
            "id": platform["id"],
            "name": platform["name"],
            "vehicle": platform["vehicle"],
        },
        "build": {"id": current["id"], "number": current["number"]},
        "spec": effective,
        "differs_from_reference": sorted(overrides),
        "completeness": _completeness_json(completeness(schema, effective)),
        "history": history,
    }


def list_airframes(
    store: Store, schema: Schema, platform_id: str | None = None
) -> list[dict[str, Any]]:
    query, args = "SELECT id FROM airframe ORDER BY name", ()
    if platform_id is not None:
        query, args = "SELECT id FROM airframe WHERE platform_id = ? ORDER BY name", (platform_id,)
    return [airframe_view(store, schema, r[0]) for r in store.conn.execute(query, args)]


def record_change(
    store: Store,
    schema: Schema,
    airframe_id: str,
    changes: dict[str, Any],
    reason_kind: ChangeReason,
    reason: str = "",
    effective_date: str | None = None,
) -> dict[str, Any]:
    """A new build version for the airframe. A value of None returns that field to
    the platform's reference."""
    if reason_kind not in ("failure", "upgrade", "test", "detected"):
        raise SetupError("a change's reason is failure, upgrade, test or detected")
    if not changes:
        raise SetupError("a change must change something")
    airframe = _require(store, "airframe", airframe_id)
    current = _require(store, "build_version", airframe["current_build_id"])
    overrides = _apply(schema, _spec(current), changes, "entered")
    when = effective_date or date.today().isoformat()
    date.fromisoformat(when)  # raises ValueError on a bad date
    with store.transaction():
        build = store.insert(
            "build_version",
            {"airframe_id": airframe_id, "number": current["number"] + 1,
             "based_on_id": current["id"], "reason_kind": reason_kind, "reason": reason,
             "spec_json": json.dumps(overrides), "effective_date": when},
        )  # fmt: skip
        store.update("airframe", airframe_id, airframe["version"], {"current_build_id": build})
    return airframe_view(store, schema, airframe_id)
