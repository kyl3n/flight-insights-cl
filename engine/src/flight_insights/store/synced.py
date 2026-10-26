"""Which tables sync between laptops (spec: Shared storage and sync), in one place.

Every table is in exactly one group, and a test fails when a migration adds a table
that is not. Analysis results are recomputed on each laptop from the logs, so they
stay local in this release; so does anything that names this laptop's own folders.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Records: an id and clocks of their own; changes merge field by field.
RECORDS: tuple[str, ...] = (
    "laptop", "platform", "build_version", "airframe", "component", "battery_pack",
    "ground_station", "ground_station_version", "source_file", "param_snapshot", "flight",
    "change_set", "recommendation", "ai_packet", "ai_exchange",
)  # fmt: skip

# Links: rows that join two records, added once and never changed. Their key is the
# primary key's columns.
LINKS: dict[str, tuple[str, ...]] = {
    "flight_source": ("flight_id", "source_file_id", "role"),
    "flight_battery": ("flight_id", "battery_pack_id"),
    "build_component": ("build_version_id", "component_id", "position"),
}

# Children: rows written with their parent record in one go and never changed; they
# travel with the parent when it is first sent.
CHILDREN: dict[str, dict[str, str]] = {
    "param_snapshot": {"param_value": "snapshot_id", "param_change": "snapshot_id"},
}

# Records two laptops can each create for the same thing: the same flight or file
# imported on both, the parameters read from that file's start, and a flight's
# recommendations. They merge on these columns (natural_key).
NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "flight": ("flight_key",),
    "source_file": ("sha256",),
    "param_snapshot": ("source_file_id", "origin"),  # a log's start: one per file
    # the same plan made from the same flight on two laptops before either synced; the
    # value too, as a later plan for the flight can supersede one with another
    "recommendation": ("flight_id", "finding_code", "param", "proposed_value"),
}

# Fields of synced records that name this laptop's own rows (a finding, an analysis
# run): they stay here. Each record also carries a portable name for the same thing
# (recommendation.finding_code, ai_packet.run_key and its refs by finding key).
LOCAL_FIELDS: dict[str, frozenset[str]] = {
    "recommendation": frozenset({"finding_id"}),
    "ai_packet": frozenset({"run_id"}),
}

LOCAL: tuple[str, ...] = (
    "analysis_run", "finding", "metric_value",  # recomputed from the logs on each laptop
    "tlog_session",  # likewise, from the tlogs and onboard logs
    "flight_tiles",  # likewise, the weather tiles from each flight's positions
    "station_folder", "ignored_file", "source_location",  # this laptop's own folders and files
    "schema_migrations", "sync_state", "sync_pending", "field_clock", "sync_outbox",
    "sync_ledger", "sync_quarantine", "sync_conflict", "sync_alias",
)  # fmt: skip

SYNCED: frozenset[str] = frozenset(RECORDS)


def natural_key(table: str, row: Mapping[str, Any]) -> dict[str, Any] | None:
    """What makes a record the same thing as another laptop's, or None if nothing does."""
    columns = NATURAL_KEYS.get(table)
    if columns is None or row.get(columns[0]) is None:  # the rest may be empty alike
        return None
    if table == "param_snapshot" and row.get("origin") != "log start":
        return None  # a parameter file is kept per airframe, and a download per session
    return {c: row[c] for c in columns}


def link_key(table: str, row: dict[str, object]) -> str:
    """A link's key as one string."""
    return "|".join(str(row[c]) for c in LINKS[table])
