"""Each pack's capacity and resistance across its flights, for a flight's plan (spec: Power
and efficiency analysis, "The battery's usable energy"; rules in
`knowledge/rules/performance.json` "packs"). Each flight's own fit is `analyzers/pack.py`'s.

- **The capacity a monitor reads** (`measured`): the median of the capacities the
  airframe's latest flights measured on that monitor, up to the flight planned.
- **`BATTn_CAPACITY` above it** (`performance.capacity_setting`): lowered to it. The
  capacity failsafe counts the charge used against `BATTn_CAPACITY - BATTn_LOW_MAH`, so a
  setting above the pack acts with less left than `BATTn_LOW_MAH`; a safety finding when
  that failsafe acts. A setting below it only acts early, and is left. While the setting
  agrees with what was measured, the setup's rated capacity is no longer proposed for it
  (`params.battery_capacity` is held back).
- **A pack aged:** its measured capacity under the end-of-life share of its rated capacity
  (`performance.pack_worn`), or its resistance up to the end-of-life multiple of its first
  flights' (`performance.pack_resistance`). Only flights where the pack flew alone, on one
  monitor, count: the import does not record which monitor read which of several packs.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.analyzers.pack import EVIDENCE
from flight_insights.analyzers.registry import Registry
from flight_insights.store.db import Store
from flight_insights.trends import Flight, analysed_flights

CAPACITY = "performance.capacity_setting"
WORN = "performance.pack_worn"  # its capacity under the end-of-life share
WORN_RESISTANCE = "performance.pack_resistance"  # its resistance up the end-of-life multiple
AUDIT = "params.battery_capacity"  # the setup's rated capacity, from the parameter audit


@dataclass(frozen=True)
class Fit:
    """One monitor's pack on one flight, as its fit followed it."""

    flight_id: str
    monitor: str  # BATT, BATT2 ...
    capacity_mah: float | None
    resistance_mohm: float  # R0 + R1: under a held load
    label_mah: float | None  # the pack's capacity in the platform setup


@dataclass(frozen=True)
class Measured:
    capacity_mah: float
    flights: tuple[str, ...]


def fits(store: Store, run_id: str) -> dict[str, Fit]:
    """A run's pack fits that followed their pack's voltage, by monitor."""
    row = store.conn.execute("SELECT flight_id, evidence_json FROM analysis_run WHERE id = ?",
                             (run_id,)).fetchone()  # fmt: skip
    out: dict[str, Fit] = {}
    for e in json.loads(row[1]) if row else ():
        v: Mapping[str, Any] = e.get("values") or {}
        if not str(e.get("id", "")).startswith(EVIDENCE) or not v.get("follows"):
            continue
        cap = v.get("capacity_mah")
        out[str(v["monitor"])] = Fit(
            str(row[0]), str(v["monitor"]),
            float(cap) if isinstance(cap, int | float) else None,
            float(v["resistance_mohm"]) + float(v.get("slow_resistance_mohm") or 0.0),
            float(v["label_mah"]) if isinstance(v.get("label_mah"), int | float) else None,
        )  # fmt: skip
    return out


def flown_upto(store: Store, reg: Registry, flight_id: str) -> list[Flight]:
    """The analysed flights of the flight's airframe, in the order they flew, up to it."""
    row = store.get("flight", flight_id)
    if row is None or row["airframe_id"] is None:
        return []
    flights, _ = analysed_flights(store, reg, str(row["airframe_id"]))
    ids = [f.id for f in flights]
    return flights[: ids.index(flight_id) + 1] if flight_id in ids else []


def measured(store: Store, reg: Registry, flight_id: str) -> dict[str, Measured]:
    """Each monitor's measured capacity over the airframe's latest flights up to this one."""
    spec: Mapping[str, Any] = reg.rules["performance"]["packs"]
    by: dict[str, list[Fit]] = {}
    for f in flown_upto(store, reg, flight_id):
        assert f.run_id is not None
        for monitor, fit in fits(store, f.run_id).items():
            if fit.capacity_mah is not None:
                by.setdefault(monitor, []).append(fit)
    out: dict[str, Measured] = {}
    for monitor, got in by.items():
        latest = got[-int(spec["flights"]) :]
        if len(latest) >= int(spec["min_flights"]):
            out[monitor] = Measured(float(np.median([x.capacity_mah or 0.0 for x in latest])),
                                    tuple(x.flight_id for x in latest))  # fmt: skip
    return out


def history(store: Store, reg: Registry, pack_id: str, upto: Sequence[Flight]) -> list[Fit]:
    """The pack's fits, in flight order, on flights where it flew alone on one monitor."""
    flown = {
        r[0]
        for r in store.conn.execute(
            "SELECT fb.flight_id FROM flight_battery fb WHERE fb.battery_pack_id = ? AND "
            "(SELECT COUNT(*) FROM flight_battery o WHERE o.flight_id = fb.flight_id) = 1",
            (pack_id,),
        ).fetchall()
    }
    out: list[Fit] = []
    for f in upto:
        if f.id in flown and f.run_id is not None:
            got = fits(store, f.run_id)
            if len(got) == 1:
                out.extend(got.values())
    return out


def _finding(flight_id: str, code: str, key: str, severity: str, params: dict[str, Any],
             evidence: list[str], changes: list[dict[str, Any]]) -> dict[str, Any]:  # fmt: skip
    return {
        "id": f"performance:{key}:{flight_id}", "analyzer": "performance", "code": code,
        "severity": severity, "confidence": "low", "params": params, "evidence": evidence,
        "changes": changes, "stage": "operations", "origin": "measured",
    }  # fmt: skip


def _capacity_failsafe(params: Mapping[str, float], prefix: str) -> bool:
    """Whether the monitor's capacity failsafe acts: a reserve with an action."""
    return any(
        float(params.get(f"{prefix}_{level}_MAH", 0.0) or 0.0) > 0
        and float(params.get(f"{prefix}_FS_{action}_ACT", 0.0) or 0.0) > 0
        for level, action in (("LOW", "LOW"), ("CRT", "CRT"))
    )


Found = list[dict[str, Any]]


def capacity_findings(store: Store, reg: Registry, flight_id: str, params: Mapping[str, float],
                      analysis: Mapping[str, Any]) -> tuple[Found, Found]:  # fmt: skip
    """`BATTn_CAPACITY` against what its pack measured, and which of the parameter audit's
    findings the measurement supersedes (as held-back entries for the plan)."""
    spec: Mapping[str, Any] = reg.rules["performance"]["packs"]
    share, step = float(spec["capacity_share"]), float(spec["round_mah"])
    present = {e["id"] for e in analysis.get("evidence", ())}
    found: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    for prefix, m in sorted(measured(store, reg, flight_id).items()):
        param = f"{prefix}_CAPACITY"
        current = params.get(param)
        if current is None or current <= 0:
            continue
        common = {"param": param, "current_mah": round(current),
                  "measured_mah": round(m.capacity_mah), "flights": len(m.flights)}  # fmt: skip
        if current > m.capacity_mah * (1 + share):
            proposed = math.floor(m.capacity_mah / step) * step
            safety = _capacity_failsafe(params, prefix)
            found.append(_finding(
                flight_id, CAPACITY, f"capacity:{prefix}", "safety" if safety else "caution",
                common | {"proposed_mah": round(proposed),
                          "over_mah": round(current - m.capacity_mah),
                          "low_mah": round(float(params.get(f"{prefix}_LOW_MAH", 0.0) or 0.0))},
                [e for e in (EVIDENCE + prefix,) if e in present],
                [{"param": param, "proposed": proposed, "stage": "operations"}],
            ))  # fmt: skip
        elif abs(current - m.capacity_mah) <= share * m.capacity_mah:
            held.append({"code": AUDIT, "flight": m.flights[-1], "reason": "measured_capacity",
                         "share_pct": round(100 * share)} | common)  # fmt: skip
    return found, held


def aging(got: Sequence[Fit], rated: float | None, spec: Mapping[str, Any]
          ) -> list[tuple[str, dict[str, Any]]]:  # fmt: skip
    """A pack's history against the end-of-life conventions: each finding code with its
    params. `got` is in flight order; `rated` its rated capacity, if known."""
    k, least = int(spec["compare"]), int(spec["min_flights"])
    caps = [x.capacity_mah for x in got if x.capacity_mah is not None]
    latest = caps[-int(spec["flights"]) :]
    capacity = float(np.median(latest)) if len(latest) >= least else None
    ohms = [x.resistance_mohm for x in got]
    n = min(k, len(ohms) // 2)
    first = float(np.median(ohms[:n])) if n >= least else None
    last = float(np.median(ohms[-n:])) if n >= least else None
    end_share, end_ratio = float(spec["aged_capacity"]), float(spec["aged_resistance"])
    out: list[tuple[str, dict[str, Any]]] = []
    if capacity is not None and rated and capacity < end_share * rated:
        out.append((WORN, {
            "capacity_mah": round(capacity), "rated_mah": round(rated),
            "capacity_pct": round(100 * capacity / rated), "end_pct": round(100 * end_share),
            "measured": len(latest),
        }))  # fmt: skip
    if first is not None and last is not None and last >= end_ratio * first:
        out.append((WORN_RESISTANCE, {
            "resistance_mohm": round(last, 1), "first_mohm": round(first, 1),
            "ratio": round(last / first, 2), "end_ratio": end_ratio, "compared": n,
        }))  # fmt: skip
    return out


def aged_findings(store: Store, reg: Registry, flight_id: str,
                  analysis: Mapping[str, Any]) -> list[dict[str, Any]]:  # fmt: skip
    """The flight's packs that have aged past the end-of-life conventions."""
    spec: Mapping[str, Any] = reg.rules["performance"]["packs"]
    upto = flown_upto(store, reg, flight_id)
    present = {e["id"] for e in analysis.get("evidence", ())}
    out: list[dict[str, Any]] = []
    packs = store.conn.execute(
        "SELECT bp.id, bp.pack_code, bp.capacity_mah FROM flight_battery fb "
        "JOIN battery_pack bp ON bp.id = fb.battery_pack_id WHERE fb.flight_id = ? "
        "ORDER BY bp.pack_code",
        (flight_id,),
    ).fetchall()
    for pack_id, code, rated in packs:
        got = history(store, reg, str(pack_id), upto)
        if not got:
            continue
        evidence = [e for e in (EVIDENCE + got[-1].monitor,) if e in present]
        common = {"pack": str(code), "flights": len(got)}
        for finding, params in aging(got, float(rated) if rated else got[-1].label_mah, spec):
            key = f"{finding.rsplit('_', 1)[-1]}:{pack_id}"
            out.append(_finding(flight_id, finding, key, "caution", common | params, evidence,
                                []))  # fmt: skip
    return out
