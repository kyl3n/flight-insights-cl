"""One log, end to end: decode, validate, segment, parameters and fingerprint, as plain
JSON-ready data for the CLI (`fi-engine analyze`) and the shell (JSON-RPC).

The result holds no positions. Coordinates stay in the decoded columns, which
only on-laptop map views read; nothing here copies them.
"""

from __future__ import annotations

import dataclasses
import secrets
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flight_insights import __version__
from flight_insights.dataflash.log import DataflashLog
from flight_insights.fingerprint import Attr, Fingerprint, fingerprint, score_archetypes
from flight_insights.history import from_log, param_set_id
from flight_insights.knowledge import Pack
from flight_insights.segment import (
    boot_identities,
    find_boots,
    flight_key,
    flights,
    timeline,
    validate,
)
from flight_insights.store.register import file_sha256

TOP_ARCHETYPES = 5


def summary(df: DataflashLog) -> dict[str, Any]:
    """What the file holds: message counts, skipped bytes, how decoding ended."""
    counts: Counter[str] = Counter()
    for fid, fmt in enumerate(df.index.formats):
        n = df.count_format(fid)
        if n:
            counts[fmt.name] += n
    idx = df.index
    return {
        "messages": sum(counts.values()),
        "types": dict(sorted(counts.items())),
        "skipped_bytes": idx.skipped_bytes,
        "rejected_messages": len(idx.rejected),
        "end": idx.stop,
    }


def _attr(a: Attr[Any]) -> dict[str, Any]:
    v: Any = a.value
    value = dataclasses.asdict(v) if dataclasses.is_dataclass(v) and not isinstance(v, type) else v
    return {"value": value, "source": a.source, "note": a.note}


FINGERPRINT_ATTRS = (
    "imu_count",
    "imu_rates_hz",
    "frame",
    "motor_protocol",
    "bidirectional_dshot",
    "rpm_sources",
    "notch_mode",
    "cells",
    "capacity_mah",
    "hover_throttle",
    "learned_hover_throttle",
    "motor_hz_at_hover",
    "gps",
    "compasses",
    "other_nav",
    "ekf_sources",
)


def fingerprint_record(fp: Fingerprint, board: dict[str, Any]) -> dict[str, Any]:
    """A fingerprint as JSON-ready data: each attribute with where it came from, and the
    steady-hover windows that analyzers read their hover metrics in."""
    return {
        "vehicle": fp.vehicle,
        "firmware": fp.firmware,
        "board": board,
        **{name: _attr(getattr(fp, name)) for name in FINGERPRINT_ATTRS},
        "hover_s": fp.hover_s,
        "flight_s": fp.flight_s,
        "mode_s": fp.mode_s,
        "hover_windows": [
            dataclasses.asdict(w) | {"duration_s": w.duration_s} for w in fp.hover_windows
        ],
    }


def analyze_log(
    path: Path,
    df: DataflashLog,
    pack: Pack,
    board_key: bytes | None,
    progress: Callable[[float, str], None] = lambda _f, _m: None,
) -> dict[str, Any]:
    """Everything Phase 0 knows about a log. Without `board_key` the board's unique ID
    is hashed with a throwaway key and the hash left out, since it would match nothing."""
    progress(0.1, "parameters")
    snapshot = from_log(df)
    vehicle, firmware = snapshot.vehicle, snapshot.firmware
    boots = find_boots(timeline(df))
    flown = flights(df, boots, pack, vehicle, firmware)
    progress(0.3, "validation")
    check = validate(df, [(f.boot, f.arm_us, f.disarm_us) for f in flown])
    ids = boot_identities(df, boots)
    progress(0.6, "fingerprint")
    fp = fingerprint(df, pack, board_key or secrets.token_bytes(32))
    board = dataclasses.asdict(fp.board)
    if board_key is None:
        board["id_hash"] = None
    unknown = snapshot.dialect(pack).to_canonical(snapshot.values).unknown
    return {
        "engine": __version__,
        "file": {"name": path.name, "size": path.stat().st_size, "sha256": file_sha256(path)},
        "decode": summary(df),
        "boots": [dataclasses.asdict(i) for i in ids],
        "validation": {
            "imu_samples_complete": check.imu_samples_complete,
            "issues": [dataclasses.asdict(i) for i in check.issues],
        },
        "flights": [
            {
                "number": f.number,
                "boot": f.boot,
                "arm_us": f.arm_us,
                "disarm_us": f.disarm_us,
                "duration_s": f.duration_s,
                "start": f.start,
                "end": f.end,
                "modes": [dataclasses.asdict(m) for m in f.modes],
                "key": flight_key(ids[f.boot], f, board["id_hash"]),
            }
            for f in flown
        ],
        "hover_windows": [
            dataclasses.asdict(w) | {"duration_s": w.duration_s} for w in fp.hover_windows
        ],
        "parameters": {
            "vehicle": vehicle,
            "firmware": firmware,
            "set_id": param_set_id(snapshot, pack),
            "count": len(snapshot.values),
            "snapshot_us": snapshot.time_us,
            "unknown": unknown,
            "in_flight_changes": [dataclasses.asdict(c) for c in snapshot.changes],
        },
        "fingerprint": fingerprint_record(fp, board),
        "archetypes": {
            "calibrated": fp.rules_calibrated,
            "ranked": [dataclasses.asdict(s) for s in score_archetypes(fp, pack)[:TOP_ARCHETYPES]],
        },
    }
