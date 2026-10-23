"""Parameter traffic in a tlog: who changed what, when, and whether the vehicle took it.

A ground station reads parameters with PARAM_VALUE replies, usually as a full
download on connection, and writes them with PARAM_SET, which the vehicle answers
with a PARAM_VALUE carrying the value it now holds (spec: Parameter history,
"tlog" source).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from flight_insights.tlog.decode import TlogLog

# A write counts as unanswered if no matching PARAM_VALUE arrives within this time.
REPLY_WINDOW_US = 3_000_000
NO_INDEX = 65535  # PARAM_VALUE sent on its own, not as part of a numbered download

Result = Literal["applied", "different", "no reply"]


@dataclass(frozen=True)
class ParamWrite:
    time_us: int
    sender: tuple[int, int]  # (sysid, compid) of the station that wrote
    target: tuple[int, int]
    name: str
    value: float
    result: Result
    reply_value: float | None = None
    reply_time_us: int | None = None


@dataclass(frozen=True)
class ParamDownload:
    """A complete set of parameters read from one vehicle component."""

    source: tuple[int, int]
    start_us: int
    end_us: int
    values: dict[str, float]


def _rows(log: TlogLog, name: str) -> list[dict[str, Any]]:
    """A message's rows with a usable time; a damaged timestamp cannot be ordered."""
    table = log.tables.get(name)
    rows = table.to_pylist() if table is not None else []
    return [r for r in rows if r["tlog_time_us"] is not None]


def _f32(value: float) -> float:
    return float(np.float32(value))


def param_writes(log: TlogLog, window_us: int = REPLY_WINDOW_US) -> list[ParamWrite]:
    """Every PARAM_SET in the tlog, paired with the vehicle's answer."""
    replies = sorted(_rows(log, "PARAM_VALUE"), key=lambda r: r["tlog_time_us"])
    out: list[ParamWrite] = []
    for s in sorted(_rows(log, "PARAM_SET"), key=lambda r: r["tlog_time_us"]):
        target = (s["target_system"], s["target_component"])
        reply = next(
            (
                r
                for r in replies
                if s["tlog_time_us"] <= r["tlog_time_us"] <= s["tlog_time_us"] + window_us
                and r["param_id"] == s["param_id"]
                and r["src_sysid"] == target[0]
                and (target[1] == 0 or r["src_compid"] == target[1])
            ),
            None,
        )
        if reply is None:
            result: Result = "no reply"
        elif _f32(reply["param_value"]) == _f32(s["param_value"]):
            result = "applied"
        else:
            result = "different"  # refused, clamped or overridden by the vehicle
        out.append(
            ParamWrite(
                time_us=s["tlog_time_us"],
                sender=(s["src_sysid"], s["src_compid"]),
                target=target,
                name=s["param_id"],
                value=s["param_value"],
                result=result,
                reply_value=None if reply is None else reply["param_value"],
                reply_time_us=None if reply is None else reply["tlog_time_us"],
            )
        )
    return out


def param_downloads(log: TlogLog) -> list[ParamDownload]:
    """Each complete parameter download: every index from 0 to param_count - 1 received."""
    by_source: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for r in sorted(_rows(log, "PARAM_VALUE"), key=lambda r: r["tlog_time_us"]):
        by_source.setdefault((r["src_sysid"], r["src_compid"]), []).append(r)
    out: list[ParamDownload] = []
    for source, rows in by_source.items():
        seen: dict[int, dict[str, Any]] = {}
        count = None
        for r in rows:
            if r["param_index"] == NO_INDEX or r["param_count"] == 0:
                continue
            if count is not None and r["param_count"] != count:
                seen = {}  # the parameter count changed: a new set, start again
            count = r["param_count"]
            seen[r["param_index"]] = r
            if len(seen) == count:
                ordered = [seen[i] for i in sorted(seen)]
                out.append(
                    ParamDownload(
                        source=source,
                        start_us=min(x["tlog_time_us"] for x in ordered),
                        end_us=max(x["tlog_time_us"] for x in ordered),
                        values={x["param_id"]: x["param_value"] for x in ordered},
                    )
                )
                seen = {}
    return out
