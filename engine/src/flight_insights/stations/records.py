"""Ground stations and their versions in the store (spec: Ground station profiles).

A station's current version holds its whole profile. While a version is still
being set up and no flight or file refers to it, saves edit it in place; after
that, any change (a new antenna, a radio firmware, an operating system update,
another computer) makes a new numbered version with its reason, and earlier
flights keep the version they flew with. Values that change minute to minute
(free disk space, clock speed, power source, clock sync) are refreshed in place
and never make a version on their own.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, cast

from flight_insights.platforms.schema import Schema, SetupError, check_value
from flight_insights.stations.computer import SOURCE, computer_answers
from flight_insights.stations.detect import Installed, software_answers
from flight_insights.store.db import Store, StoreError

Spec = dict[str, dict[str, Any]]  # field id -> {"value": ..., "source": ...}
Profile = dict[str, Any]  # part -> Spec, or a list of Specs for radios and antennas
COLUMNS = {
    "computer": "computer_json", "software": "software_json",
    "connections": "connections_json", "clock": "clock_json",
    "radios": "radios_json", "antennas": "antennas_json",
}  # fmt: skip
CHANGE_REASONS = ("failure", "upgrade", "test", "detected")


def _require(store: Store, table: str, record_id: str) -> dict[str, Any]:
    row = store.get(table, record_id)
    if row is None:
        raise SetupError(f"no {table.replace('_', ' ')} {record_id}")
    return row


def _profile(row: dict[str, Any]) -> Profile:
    return {part: json.loads(row[column]) for part, column in COLUMNS.items()}


def _apply(
    schema: Schema, part: str, spec: Spec, values: dict[str, Any], source: str,
    sources: dict[str, str] | None = None,
) -> Spec:  # fmt: skip
    """Checked values merged into one part; None removes a field."""
    out = dict(spec)
    for field_id, value in values.items():
        field = schema.field(field_id)
        if field.step != part:
            raise SetupError(f"{field_id} belongs in {field.step}, not {part}")
        checked = check_value(field, value)
        if checked is None:
            out.pop(field_id, None)
            continue
        own = (sources or {}).get(field_id, source)
        if not own.startswith(source):
            raise SetupError(f"{field_id}: a source must start with {source!r}")
        out[field_id] = {"value": checked, "source": own[:200]}
    return out


def _merge(
    schema: Schema, profile: Profile, changes: dict[str, Any], source: str,
    sources: dict[str, dict[str, str]] | None = None,
) -> Profile:  # fmt: skip
    """The profile with `changes` applied: a part's values merge into it, and a
    list for radios or antennas replaces the whole list."""
    out = dict(profile)
    for part, values in changes.items():
        if part not in COLUMNS:
            raise SetupError(f"no profile part {part!r}")
        if part in schema.repeating:
            if not isinstance(values, list):
                raise SetupError(f"{part} must be a list")
            entries = cast(list[Any], values)
            if not all(isinstance(e, dict) for e in entries):
                raise SetupError(f"each of {part} must be an object")
            out[part] = [_apply(schema, part, {}, e, source) for e in entries]
        else:
            if not isinstance(values, dict):
                raise SetupError(f"{part} must be an object")
            given = cast(dict[str, Any], values)
            out[part] = _apply(schema, part, out[part], given, source, (sources or {}).get(part))
    return out


def _lasting(schema: Schema, profile: Profile) -> dict[str, Any]:
    """The profile without its volatile values or sources: what a version is."""

    def values(spec: Spec) -> dict[str, Any]:
        return {k: e["value"] for k, e in spec.items() if not schema.field(k).volatile}

    return {
        part: [values(e) for e in profile[part]] if part in schema.repeating
        else values(profile[part])
        for part in COLUMNS
    }  # fmt: skip


def _changed(schema: Schema, before: Profile, after: Profile) -> list[str]:
    """What changed, as short lines such as "os_version 10.0.22631 → 10.0.26100"."""
    a, b = _lasting(schema, before), _lasting(schema, after)
    lines: list[str] = []
    for part in COLUMNS:
        if part in schema.repeating:
            if a[part] != b[part]:
                lines.append(f"{part}: {len(a[part])} → {len(b[part])} entries"
                             if len(a[part]) != len(b[part]) else f"{part} changed")  # fmt: skip
            continue
        for k in sorted(a[part].keys() | b[part].keys()):
            if a[part].get(k) != b[part].get(k):
                lines.append(f"{k} {a[part].get(k, '—')} → {b[part].get(k, '—')}")
    return lines


def _in_use(store: Store, version_id: str) -> bool:
    """Whether a flight or a file refers to the version, which freezes it."""
    for table in ("flight", "source_file"):
        row = store.conn.execute(
            f"SELECT 1 FROM {table} WHERE ground_station_version_id = ? LIMIT 1", (version_id,)
        ).fetchone()
        if row:
            return True
    return False


# --- stations -------------------------------------------------------------------------


def create_station(store: Store, name: str) -> str:
    if not name.strip() or len(name) > 200:
        raise SetupError("a ground station needs a name of up to 200 characters")
    with store.transaction():
        station = store.insert("ground_station", {"name": name.strip()})
        version = store.insert(
            "ground_station_version",
            {"ground_station_id": station, "number": 1, "reason_kind": "setup",
             "effective_date": date.today().isoformat()},
        )  # fmt: skip
        store.update("ground_station", station, 1, {"current_version_id": version})
    return station


def station_view(store: Store, station_id: str, host_hash: str | None = None) -> dict[str, Any]:
    station = _require(store, "ground_station", station_id)
    current = _require(store, "ground_station_version", station["current_version_id"])
    history = [
        {"id": r[0], "number": r[1], "reason_kind": r[2], "reason": r[3], "effective_date": r[4]}
        for r in store.conn.execute(
            "SELECT id, number, reason_kind, reason, effective_date FROM ground_station_version "
            "WHERE ground_station_id = ? ORDER BY number",
            (station_id,),
        )
    ]
    view: dict[str, Any] = {
        "id": station_id,
        "name": station["name"],
        "version": station["version"],
        "current": {
            "id": current["id"],
            "number": current["number"],
            "version": current["version"],
            "reason_kind": current["reason_kind"],
            "reason": current["reason"],
            "effective_date": current["effective_date"],
            "in_use": _in_use(store, current["id"]),
            "computer_known": current["host_hash"] is not None,
            "profile": _profile(current),
        },
        "history": history,
    }
    if host_hash is not None:
        view["this_computer"] = current["host_hash"] == host_hash
    return view


def list_stations(store: Store, host_hash: str | None = None) -> list[dict[str, Any]]:
    ids = [r[0] for r in store.conn.execute("SELECT id FROM ground_station ORDER BY name")]
    return [station_view(store, i, host_hash) for i in ids]


def station_for_host(store: Store, host_hash: str) -> str | None:
    """The station whose current version was detected on this computer."""
    row = store.conn.execute(
        "SELECT s.id FROM ground_station s JOIN ground_station_version v "
        "ON v.id = s.current_version_id WHERE v.host_hash = ? ORDER BY s.name LIMIT 1",
        (host_hash,),
    ).fetchone()
    return row[0] if row else None


def _save(
    store: Store, schema: Schema, station_id: str, base_version: int, after: Profile,
    reason_kind: str, reason: str, effective_date: str | None, host_hash: str | None,
) -> bool:  # fmt: skip
    """Store `after` as the station's profile. Returns whether anything changed."""
    station = _require(store, "ground_station", station_id)
    if station["version"] != base_version:
        raise StoreError(f"ground_station {station_id} is not at version {base_version}")
    current = _require(store, "ground_station_version", station["current_version_id"])
    before = _profile(current)
    host = host_hash or current["host_hash"]
    if after == before and host == current["host_hash"]:
        return False
    columns = {COLUMNS[p]: json.dumps(after[p]) for p in COLUMNS}
    lasting = _changed(schema, before, after)
    if current["host_hash"] is not None and host != current["host_hash"]:
        lasting.append("a different computer")
    in_place = (reason_kind == "setup" and not _in_use(store, current["id"])) or not lasting
    if reason_kind == "setup" and not in_place:
        raise SetupError("this version has been flown with: record a change and its reason")
    with store.transaction():
        if in_place:  # still being set up, or only volatile values moved
            store.update("ground_station_version", current["id"], current["version"],
                         columns | {"host_hash": host})  # fmt: skip
            store.update("ground_station", station_id, base_version, {})  # readers see it moved
            return True
        when = effective_date or date.today().isoformat()
        date.fromisoformat(when)  # raises ValueError on a bad date
        version = store.insert(
            "ground_station_version",
            columns | {"ground_station_id": station_id, "number": current["number"] + 1,
                       "based_on_id": current["id"], "reason_kind": reason_kind,
                       "reason": reason or "; ".join(lasting)[:500], "effective_date": when,
                       "host_hash": host},
        )  # fmt: skip
        store.update("ground_station", station_id, base_version, {"current_version_id": version})
    return True


def save_profile(
    store: Store,
    schema: Schema,
    station_id: str,
    base_version: int,
    changes: dict[str, Any],
    reason_kind: str = "setup",
    reason: str = "",
    effective_date: str | None = None,
    source: str = "entered",
    sources: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Save profile answers. "setup" edits the version still being set up; any other
    reason (failure, upgrade, test, detected) makes a new version."""
    if reason_kind not in ("setup", *CHANGE_REASONS):
        raise SetupError("a change's reason is setup, failure, upgrade, test or detected")
    station = _require(store, "ground_station", station_id)
    current = _require(store, "ground_station_version", station["current_version_id"])
    after = _merge(schema, _profile(current), changes, source, sources)
    _save(store, schema, station_id, base_version, after, reason_kind, reason, effective_date,
          None)  # fmt: skip
    return station_view(store, station_id)


def rename_station(store: Store, station_id: str, base_version: int, name: str) -> dict[str, Any]:
    if not name.strip() or len(name) > 200:
        raise SetupError("a ground station needs a name of up to 200 characters")
    store.update("ground_station", station_id, base_version, {"name": name.strip()})
    return station_view(store, station_id)


def record_computer(
    store: Store,
    schema: Schema,
    station_id: str,
    base_version: int,
    facts: dict[str, Any],
    installed: list[Installed],
    host_hash: str | None,
) -> dict[str, Any]:
    """Record this computer's facts and installed software on the station.

    Detected facts replace what was there for the same fields. If the station was
    last detected on another computer, the whole computer part is replaced. The
    program and its version are filled from detection only where they are empty
    or were themselves detected, so a person's own answer stands.
    """
    station = _require(store, "ground_station", station_id)
    current = _require(store, "ground_station_version", station["current_version_id"])
    profile = _profile(current)
    other_computer = None not in (current["host_hash"], host_hash) and (
        current["host_hash"] != host_hash
    )
    if other_computer:
        profile["computer"] = {}
    answers = computer_answers(facts)
    after = _merge(schema, profile, answers, SOURCE)
    software: Spec = dict(after["software"])
    found = software_answers(installed)
    for field_id, entry in found.items():
        mine = software.get(field_id)
        if (
            field_id == "gcs_version"
            and software.get("gcs", {}).get("value") != found["gcs"]["value"]
        ):
            continue  # the version of another program than the one in use
        if field_id == "installed_gcs" or mine is None or mine["source"].startswith("detected"):
            software[field_id] = {"value": check_value(schema.field(field_id), entry["value"]),
                                  "source": entry["source"][:200]}  # fmt: skip
    if not installed:
        software.pop("installed_gcs", None)
    after["software"] = software
    reason = "detected on a different computer" if other_computer else ""
    unused = not _in_use(store, current["id"])  # nothing flown with it: edit it in place
    _save(store, schema, station_id, base_version, after, "setup" if unused else "detected",
          reason, None, host_hash)  # fmt: skip
    return station_view(store, station_id, host_hash)
